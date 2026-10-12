"""Single-process Git publisher; run separately from the read-only HTTP service."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

from .core import is_link
from .errors import KnowledgeError
from .management import Registry
from .repositories import RepositoryCatalog, branch_name, identifier, remote_url, timestamp


class SyncError(Exception):
    pass


@contextmanager
def worker_lock(directory, filename='git-worker.lock'):
    """OS lock dies with the process; no second worker can recover live jobs."""
    with (directory / filename).open('a+b') as handle:
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b'0'); handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise SyncError('已有 Git 同步进程正在运行') from None
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise SyncError('已有 Git 同步进程正在运行') from None
        try:
            yield
        finally:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


class GitWorker:
    def __init__(self, registry, credentials_dir=None, timeout=300, max_bytes=256*1024*1024):
        self.registry = registry
        self.catalog = RepositoryCatalog(registry)
        self.store = registry.store
        self.private = registry.directory / 'git-cache'
        self.private.mkdir(mode=0o700, exist_ok=True)
        self.credentials = Path(credentials_dir).resolve() if credentials_dir else None
        if self.credentials and (self.credentials.is_relative_to(registry.pool.root) or registry.pool.root.is_relative_to(self.credentials)):
            raise ValueError('Git credentials must be outside the knowledge root')
        self.timeout, self.max_bytes = timeout, max_bytes
        self.last_heartbeat = 0.0

    def heartbeat(self):
        if time.monotonic()-self.last_heartbeat < 5:
            return
        with self.store.transaction(write=True) as db:
            db.execute("INSERT INTO metadata VALUES ('git_worker_heartbeat',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (str(time.time()),))
        self.last_heartbeat = time.monotonic()

    def recover(self):
        with self.store.transaction(write=True) as db:
            for row in db.execute("SELECT * FROM repository_jobs WHERE status='running'").fetchall():
                db.execute("UPDATE repository_jobs SET status='failed',error=?,finished_at=? WHERE id=?", ('worker_interrupted: 同步进程中断，旧版本保持可用；可重新同步', timestamp(), row['id']))
                db.execute('UPDATE repositories SET last_error=?,next_sync=? WHERE id=?', ('worker_interrupted', time.time()+60, row['repository_id']))

    def claim(self):
        with self.store.transaction(write=True) as db:
            for row in db.execute('SELECT id FROM repositories WHERE enabled=1 AND interval_seconds>0 AND next_sync IS NOT NULL AND next_sync<=?', (time.time(),)).fetchall():
                self.catalog._queue(db, row['id'], 'scheduled')
            job = db.execute("SELECT * FROM repository_jobs WHERE status='queued' ORDER BY created_at,rowid LIMIT 1").fetchone()
            if not job:
                return None
            repo = self.catalog.get(db, job['repository_id'])
            db.execute("UPDATE repository_jobs SET status='running',started_at=?,before_commit=? WHERE id=?", (timestamp(), repo['current_commit'], job['id']))
            return dict(job), repo

    def tick(self):
        self.heartbeat()
        task = self.claim()
        if task is None:
            return False
        job, repo = task
        try:
            self.sync(job, repo)
        except Exception as exc:
            # No raw Git stderr, URL credentials, absolute paths or traceback in the DB/UI.
            error = str(exc) if isinstance(exc, SyncError) else 'sync_failed: 检查文件权限、磁盘空间和同步进程配置'
            with self.store.transaction(write=True) as db:
                latest = self.catalog.get(db, repo['id'])
                db.execute("UPDATE repository_jobs SET status='failed',finished_at=?,error=? WHERE id=?", (timestamp(), error, job['id']))
                db.execute('UPDATE repositories SET last_error=?,next_sync=? WHERE id=?',
                           (error, self.next_sync(latest), repo['id']))
                self.store.event(db, {'kind':'admin','action':'repository_sync_failed','path':repo['id'],'status':'error','error':error})
        return True

    @staticmethod
    def next_sync(repo):
        return time.time()+repo['interval_seconds'] if repo['enabled'] and repo['interval_seconds'] else None

    def environment(self):
        env = {k:v for k,v in os.environ.items() if not k.startswith(('GIT_', 'SSH_'))}
        env.update(GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_ATTR_NOSYSTEM='1', GIT_LFS_SKIP_SMUDGE='1', GIT_OPTIONAL_LOCKS='0')
        return env

    def git(self, args, *, env=None, destination=None, allowed=(0,)):
        with tempfile.TemporaryFile() as stderr, tempfile.TemporaryFile() as stdout:
            target = destination or stdout
            cmd = ['git', '-c', 'core.hooksPath='+os.devnull, '-c', 'core.fsmonitor=false',
                   '-c', 'protocol.file.allow=never', '-c', 'protocol.ext.allow=never',
                   '-c', 'protocol.allow=never', '-c', 'protocol.https.allow=always',
                   '-c', 'protocol.ssh.allow=always', '-c', 'credential.helper=',
                   '-c', 'http.followRedirects=false', *args]
            try:
                proc = subprocess.Popen(cmd, cwd=self.private, env=env or self.environment(), stdin=subprocess.DEVNULL,
                                        stdout=target, stderr=stderr, start_new_session=os.name != 'nt')
            except FileNotFoundError:
                raise SyncError('git_missing: 同步镜像未安装 Git') from None
            started = time.monotonic()
            try:
                while proc.poll() is None:
                    self.heartbeat()
                    if time.monotonic()-started > self.timeout:
                        raise SyncError('git_timeout: Git 操作超时，旧版本保持可用')
                    if os.fstat(target.fileno()).st_size > (self.max_bytes if destination else 8*1024*1024) or os.fstat(stderr.fileno()).st_size > 2*1024*1024:
                        raise SyncError('git_output_limit: 仓库或命令输出超过配置限制')
                    time.sleep(.05)
            finally:
                if proc.poll() is None:
                    if os.name != 'nt':
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                    proc.wait()
            if proc.returncode not in allowed:
                stderr.seek(0)
                detail = stderr.read(8192).lower()
                if any(x in detail for x in (b'authentication failed', b'permission denied', b'could not read username')):
                    raise SyncError('git_auth_failed: 仓库认证失败，请检查凭据及仓库读取权限')
                if b'host key verification failed' in detail:
                    raise SyncError('ssh_host_unverified: 请配置并核对 SSH known_hosts')
                if b"couldn't find remote ref" in detail:
                    raise SyncError('branch_not_found: 远端分支不存在')
                raise SyncError('git_failed: Git 操作失败，请检查远端地址、分支、网络和凭据')
            if destination:
                if os.fstat(target.fileno()).st_size > self.max_bytes:
                    raise SyncError('snapshot_too_large: 发布快照超过大小限制')
                return proc.returncode, b''
            stdout.seek(0)
            data = stdout.read(8*1024*1024+1)
            if len(data)>8*1024*1024:
                raise SyncError('git_output_limit: Git 输出超过限制')
            return proc.returncode, data

    def credential_file(self, name):
        if not self.credentials or not name or Path(name).name != name or '/' in name or '\\' in name:
            raise SyncError('credential_missing: 请在同步进程配置凭据文件')
        path = self.credentials / name
        if not path.is_file() or is_link(path.lstat()) or not path.resolve().is_relative_to(self.credentials):
            raise SyncError('credential_missing: 凭据文件不存在或不可用')
        return path

    @contextmanager
    def authenticated_env(self, repo):
        env = self.environment()
        # Never use inherited ssh-agent or SSH config as a hidden credential fallback.
        env['GIT_SSH_COMMAND'] = 'ssh -F /dev/null -o BatchMode=yes -o StrictHostKeyChecking=yes -o IdentityAgent=none -o IdentitiesOnly=yes'
        if not repo['credential']:
            yield env
            return
        try:
            profile = json.loads(self.credential_file(identifier(repo['credential'])+'.json').read_text(encoding='utf-8'))
            if repo['url'].startswith('https://') and profile['type'] == 'https_token':
                token = self.credential_file(profile['token_file']).read_text(encoding='utf-8').strip()
                username = profile.get('username', 'oauth2')
                if not token or '\n' in token or '\r' in token or not re.fullmatch(r'[A-Za-z0-9_.@-]+', username):
                    raise ValueError()
                # Git gets credentials through a one-command environment, never argv or config.
                with tempfile.TemporaryDirectory(dir=self.private) as directory:
                    askpass = Path(directory)/'askpass.py'
                    askpass.write_text("#!/usr/bin/env python3\nimport os,sys\nprint(os.environ['FKB_GIT_USERNAME'] if 'username' in sys.argv[1].lower() else os.environ['FKB_GIT_TOKEN'])\n", encoding='utf-8')
                    askpass.chmod(0o700)
                    if os.name == 'nt':
                        launcher = Path(directory)/'askpass.cmd'
                        launcher.write_text('@"'+sys.executable+'" "'+str(askpass)+'" %*\n',encoding='utf-8')
                        env['GIT_ASKPASS'] = str(launcher)
                    else:
                        env['GIT_ASKPASS'] = str(askpass)
                    env.update(FKB_GIT_USERNAME=username, FKB_GIT_TOKEN=token)
                    yield env
            elif repo['url'].startswith('ssh://') and profile['type'] == 'ssh_key':
                key = self.credential_file(profile['key_file'])
                hosts = self.credential_file(profile['known_hosts_file'])
                env['GIT_SSH_COMMAND'] += ' -i '+shlex.quote(str(key))+' -o UserKnownHostsFile='+shlex.quote(str(hosts))
                yield env
            else:
                raise ValueError()
        except (KeyError, ValueError, OSError):
            raise SyncError('credential_invalid: 凭据配置无效或与仓库协议不匹配') from None

    def inspect_import(self, repo):
        folder = self.registry.folder_for_path(repo['import_path']).root
        gitdir = folder / '.git'
        if not gitdir.is_dir() or is_link(gitdir.lstat()):
            raise SyncError('import_not_repository: 只支持具有独立 .git 目录的已有仓库')
        # Includes/filters can execute code or load paths outside the adopted folder.
        config = gitdir/'config'
        if not config.is_file() or is_link(config.lstat()):
            raise SyncError('import_config: 仓库配置不可用')
        _, raw = self.git(['config','--file',str(config),'--no-includes','--list'])
        keys = [line.split('=',1)[0].lower() for line in raw.decode('utf-8',errors='replace').splitlines()]
        if any(k.startswith(('include.', 'includeif.', 'filter.', 'diff.', 'credential.')) or k in ('core.worktree','core.fsmonitor','core.sshcommand') for k in keys):
            raise SyncError('import_config: 已有仓库包含外部配置或执行扩展，请用远端地址重新登记')
        # A root-owned checkout is common in Linux deployments. Trust only this
        # explicitly registered, boundary-checked path, never safe.directory=*.
        checkout_args = ['-c','safe.directory='+str(folder),'-C',str(folder)]
        _, status = self.git([*checkout_args,'status','--porcelain=v1','--untracked-files=all'])
        if status.strip():
            raise SyncError('local_changes: 已有仓库存在本地修改或未跟踪文件，请先处理再同步')
        _, raw_url = self.git(['config','--file',str(config),'--no-includes','--get','remote.origin.url'])
        _, raw_branch = self.git([*checkout_args,'symbolic-ref','--short','HEAD'])
        try:
            url = remote_url(raw_url.decode().strip())
            branch = branch_name(repo['branch'] or raw_branch.decode().strip())
        except KnowledgeError:
            raise SyncError('import_remote: origin 地址或分支不受支持；请去掉 URL 中的凭据') from None
        _, raw_commit = self.git([*checkout_args,'rev-parse','HEAD'])
        return url, branch, raw_commit.decode().strip()

    def make_release(self, rid, commit, archive):
        root = self.registry.pool.root
        relative = f'git-releases/{rid}/{commit[:12]}-{uuid.uuid4().hex[:12]}'
        current = root
        for part in relative.split('/'):
            current = current/part
            if current.exists() and is_link(current.lstat()):
                raise SyncError('unsafe_release_path: 发布目录不能是链接')
            current.mkdir(exist_ok=True)
            if not current.resolve().is_relative_to(root):
                raise SyncError('unsafe_release_path: 发布目录超出知识根目录')
        total = count = 0
        with tarfile.open(fileobj=archive, mode='r:') as tar:
            for entry in tar:
                count += 1
                parts = PurePosixPath(entry.name).parts
                if (not parts or entry.name.startswith('/') or '\\' in entry.name or ':' in entry.name or
                        '..' in parts or '.git' in parts or len(parts)>64 or not (entry.isfile() or entry.isdir())):
                    raise SyncError('unsupported_entry: 仓库含链接或不安全路径，无法发布')
                total += entry.size
                if count>50000 or total>self.max_bytes:
                    raise SyncError('snapshot_too_large: 快照超过 50000 项或大小限制')
                target = current.joinpath(*parts)
                if entry.isdir():
                    target.mkdir(parents=True,exist_ok=True)
                else:
                    target.parent.mkdir(parents=True,exist_ok=True)
                    with tar.extractfile(entry) as src, target.open('xb') as out:
                        first = True
                        while chunk := src.read(65536):
                            if first and chunk.splitlines()[0] == b'version https://git-lfs.github.com/spec/v1':
                                raise SyncError('lfs_unsupported: 暂不支持 Git LFS 文件，请使用普通文档仓库')
                            out.write(chunk)
                            first = False
                self.heartbeat()
        return relative

    def sync(self, job, repo):
        imported_commit = None
        if repo['import_path']:
            url, branch, imported_commit = self.inspect_import(repo)
            if repo['url'] and (repo['url'] != url or repo['branch'] != branch):
                raise SyncError('import_changed: 原目录的远端或分支已变更，请重新登记')
            repo.update(url=url,branch=branch)
            with self.store.transaction(write=True) as db:
                db.execute('UPDATE repositories SET url=?,branch=? WHERE id=?',(url,branch,repo['id']))
        cache = self.private/repo['id']
        if not cache.exists():
            self.git(['init','--bare',str(cache)])
        if is_link(cache.lstat()):
            raise SyncError('unsafe_cache: Git 缓存目录不可用')
        with self.authenticated_env(repo) as env:
            self.git(['--git-dir='+str(cache),'fetch','--no-tags','--',repo['url'],f'+refs/heads/{repo["branch"]}:refs/heads/incoming'],env=env)
        _, raw = self.git(['--git-dir='+str(cache),'rev-parse','refs/heads/incoming^{commit}'])
        commit = raw.decode().strip()
        if not re.fullmatch('[0-9a-f]{40,64}',commit):
            raise SyncError('invalid_commit: 无法识别远端提交')
        # A deliberate branch switch may select an unrelated history. Ordinary
        # updates on the published branch must still reject force-pushes.
        previous_commit = repo['current_commit'] if repo['published_branch'] == repo['branch'] else None
        for previous in {previous_commit, imported_commit}-{None}:
            code,_ = self.git(['--git-dir='+str(cache),'merge-base','--is-ancestor',previous,commit],allowed=(0,1,128))
            if code:
                raise SyncError('history_diverged: 本地提交未合入远端或分支历史已重写，保留旧版本')
        unchanged = commit == repo['current_commit'] and repo['release_path']
        release = repo['release_path']
        if not unchanged:
            _, tree = self.git(['--git-dir='+str(cache),'ls-tree','-r',commit])
            if any(line.startswith(b'160000 ') for line in tree.splitlines()):
                raise SyncError('submodule_unsupported: 暂不支持包含 Git 子模块的知识仓库')
            with tempfile.TemporaryFile(dir=self.private) as archive:
                self.git(['--git-dir='+str(cache),'archive','--format=tar',commit],destination=archive)
                archive.seek(0)
                release = self.make_release(repo['id'],commit,archive)
        try:
            self.registry.folder_for_path(release)
        except KnowledgeError:
            raise SyncError('release_missing: 已发布目录丢失，请恢复目录后重试') from None
        # One SQLite transaction switches every binding. An in-flight request retains
        # its already resolved old directory; old releases are deliberately retained.
        with self.store.transaction(write=True) as db:
            latest = self.catalog.get(db,repo['id'])
            bindings = db.execute('SELECT * FROM repository_libraries WHERE repository_id=?',(repo['id'],)).fetchall()
            for binding in bindings:
                path = release+('/'+binding['subpath'] if binding['subpath']!='.' else '')
                try:
                    self.registry.folder_for_path(path)
                except KnowledgeError:
                    raise SyncError('library_path_missing: 新版本缺少已关联的知识目录，保留旧版本') from None
                db.execute('UPDATE libraries SET path=?,updated_at=? WHERE id=?',(path,timestamp(),binding['library_id']))
            db.execute('UPDATE repositories SET current_commit=?,release_path=?,published_branch=?,last_sync=?,next_sync=?,last_error=NULL WHERE id=?',
                       (commit,release,repo['branch'],timestamp(),self.next_sync(latest),repo['id']))
            db.execute("UPDATE repository_jobs SET status=?,after_commit=?,finished_at=? WHERE id=?",('unchanged' if unchanged else 'succeeded',commit,timestamp(),job['id']))
            self.store.event(db,{'kind':'admin','action':'repository_sync_published','path':repo['id'],'status':'ok'})


def main():
    registry = Registry(os.environ['KB_ROOT'],os.environ['KB_STATE_DIR'])
    worker = GitWorker(registry,os.getenv('KB_GIT_CREDENTIALS_DIR'),
                       timeout=int(os.getenv('KB_GIT_TIMEOUT','300')),
                       max_bytes=int(os.getenv('KB_GIT_MAX_BYTES',str(256*1024*1024))))
    def stop(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM,stop)
    with worker_lock(registry.directory):
        worker.recover()
        try:
            while True:
                if not worker.tick():
                    time.sleep(2)
        finally:
            with registry.store.transaction(write=True) as db:
                db.execute("DELETE FROM metadata WHERE name='git_worker_heartbeat'")


if __name__ == '__main__':
    main()
