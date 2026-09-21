"""Repository catalog and durable jobs. Only the separate worker invokes Git."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import PurePosixPath
import re
import time
from urllib.parse import urlsplit
import uuid

from .errors import KnowledgeError


REPOSITORY_SCHEMA = (
    '''CREATE TABLE repositories (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL, branch TEXT NOT NULL,
        import_path TEXT, credential TEXT NOT NULL, interval_seconds INTEGER NOT NULL,
        enabled INTEGER NOT NULL, current_commit TEXT, release_path TEXT,
        last_sync TEXT, next_sync REAL, last_error TEXT, created_at TEXT NOT NULL)''',
    '''CREATE TABLE repository_jobs (
        id TEXT PRIMARY KEY, repository_id TEXT NOT NULL REFERENCES repositories(id),
        trigger TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
        started_at TEXT, finished_at TEXT, before_commit TEXT, after_commit TEXT, error TEXT)''',
    "CREATE UNIQUE INDEX one_repository_job ON repository_jobs(repository_id) WHERE status IN ('queued','running')",
    '''CREATE TABLE repository_libraries (
        library_id TEXT PRIMARY KEY REFERENCES libraries(id),
        repository_id TEXT NOT NULL REFERENCES repositories(id), subpath TEXT NOT NULL)''',
)


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def identifier(value):
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', value):
        raise KnowledgeError('invalid_id', 'ID 只允许小写字母、数字、短横线和下划线')
    return value


def relative_path(value):
    if value == '.':
        return value
    parts = value.split('/')
    if (not value or len(value) > 1024 or '\\' in value or ':' in value or
            any(not p or p.startswith('.') for p in parts)):
        raise KnowledgeError('invalid_path', '请填写不含隐藏目录或上级跳转的相对路径')
    return PurePosixPath(value).as_posix()


def remote_url(value):
    if not value or len(value) > 2048 or any(c.isspace() or ord(c) < 32 for c in value):
        raise KnowledgeError('invalid_remote', '请填写 HTTPS 或 SSH 仓库地址')
    # Normalize scp-like SSH syntax, including private Git servers.
    match = re.fullmatch(r'([A-Za-z0-9_.-]+)@([A-Za-z0-9.-]+):([^:]+)', value)
    if match:
        value = 'ssh://' + match[1] + '@' + match[2] + '/' + match[3]
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (parsed.scheme not in ('https', 'ssh') or not parsed.hostname or
                not parsed.path or parsed.path == '/' or parsed.query or parsed.fragment or
                parsed.password or (parsed.scheme == 'https' and parsed.username) or
                not re.fullmatch(r'[A-Za-z0-9.:-]+', parsed.hostname) or
                (parsed.username and not re.fullmatch(r'[A-Za-z0-9_.-]+', parsed.username))):
            raise ValueError()
    except ValueError:
        raise KnowledgeError('invalid_remote', '仅支持 HTTPS / SSH，地址中不能包含密码、Token 或查询参数') from None
    return value


def branch_name(value):
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9/_.-]{0,199}', value) or
            '..' in value or any(not p or p.startswith('.') or p.endswith(('.', '.lock')) for p in value.split('/'))):
        raise KnowledgeError('invalid_branch', '分支名无效，请使用明确的分支名称')
    return value


class RepositoryCatalog:
    def __init__(self, registry):
        self.registry = registry
        self.store = registry.store

    def get(self, db, rid):
        row = db.execute('SELECT * FROM repositories WHERE id=?', (rid,)).fetchone()
        if row is None:
            raise KnowledgeError('unknown_repository', '仓库不存在', 404)
        return dict(row)

    def list(self):
        with self.store.transaction() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM repositories ORDER BY created_at,id')]
            for row in rows:
                row['enabled'] = bool(row['enabled'])
                row['libraries'] = [dict(x) for x in db.execute(
                    'SELECT b.*,l.name FROM repository_libraries b JOIN libraries l ON l.id=b.library_id WHERE repository_id=?', (row['id'],))]
                job = db.execute('SELECT * FROM repository_jobs WHERE repository_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1', (row['id'],)).fetchone()
                row['job'] = dict(job) if job else None
            heartbeat = db.execute("SELECT value FROM metadata WHERE name='git_worker_heartbeat'").fetchone()
        return {'repositories': rows, 'worker_online': bool(heartbeat and time.time()-float(heartbeat[0]) < 30)}

    def _queue(self, db, rid, trigger):
        active = db.execute("SELECT * FROM repository_jobs WHERE repository_id=? AND status IN ('queued','running')", (rid,)).fetchone()
        if active:
            return dict(active)
        row = {'id': uuid.uuid4().hex, 'repository_id': rid, 'trigger': trigger,
               'status': 'queued', 'created_at': timestamp()}
        db.execute('INSERT INTO repository_jobs(id,repository_id,trigger,status,created_at) VALUES (:id,:repository_id,:trigger,:status,:created_at)', row)
        self.store.event(db, {'kind': 'admin', 'action': 'repository_sync_queued', 'path': rid, 'status': 'ok'})
        return row

    def create(self, name, url='', branch='', import_path=None, credential='', interval_seconds=0):
        name = name.strip()
        if not 1 <= len(name) <= 80:
            raise KnowledgeError('invalid_name', '仓库名称需为 1–80 个字符')
        self._settings(credential, interval_seconds)
        if import_path:
            import_path = relative_path(import_path)
            if import_path == '.' or import_path.split('/')[0] == 'git-releases':
                raise KnowledgeError('invalid_import', '请选择根目录下已有的独立 Git 仓库')
            self.registry.folder_for_path(import_path)
            if url:
                raise KnowledgeError('invalid_import', '接管仓库时无需填写远端地址')
            branch = branch_name(branch) if branch else ''
        else:
            url, branch = remote_url(url), branch_name(branch)
        rid = uuid.uuid4().hex[:16]
        with self.store.transaction(write=True) as db:
            if import_path and db.execute('SELECT 1 FROM repositories WHERE import_path=?', (import_path,)).fetchone():
                raise KnowledgeError('duplicate_repository', '此目录已登记为 Git 仓库', 409)
            db.execute('INSERT INTO repositories(id,name,url,branch,import_path,credential,interval_seconds,enabled,created_at) VALUES (?,?,?,?,?,?,?,1,?)',
                       (rid, name, url, branch, import_path, credential, interval_seconds, timestamp()))
            self._queue(db, rid, 'initial')
            return self.get(db, rid)

    @staticmethod
    def _settings(credential, interval):
        if credential:
            identifier(credential)
        if interval != 0 and not 60 <= interval <= 2592000:
            raise KnowledgeError('invalid_interval', '更新间隔为 0（手动）或 60–2592000 秒')

    def update(self, rid, name, credential, interval_seconds, enabled):
        self._settings(credential, interval_seconds)
        if not 1 <= len(name.strip()) <= 80:
            raise KnowledgeError('invalid_name', '仓库名称需为 1–80 个字符')
        with self.store.transaction(write=True) as db:
            self.get(db, rid)
            db.execute('UPDATE repositories SET name=?,credential=?,interval_seconds=?,enabled=?,next_sync=? WHERE id=?',
                       (name.strip(), credential, interval_seconds, enabled,
                        time.time()+interval_seconds if enabled and interval_seconds else None, rid))
            self.store.event(db, {'kind':'admin','action':'repository_settings_update','path':rid,'status':'ok'})
            return self.get(db, rid)

    def enqueue(self, rid):
        with self.store.transaction(write=True) as db:
            self.get(db, rid)
            return self._queue(db, rid, 'manual')

    def jobs(self, rid):
        with self.store.transaction() as db:
            self.get(db, rid)
            return {'jobs': [dict(x) for x in db.execute('SELECT * FROM repository_jobs WHERE repository_id=? ORDER BY created_at DESC,rowid DESC LIMIT 100', (rid,))]}

    def bind(self, rid, library_id, name, subpath='.', existing=False):
        identifier(library_id)
        subpath = relative_path(subpath)
        if not 1 <= len(name.strip()) <= 80:
            raise KnowledgeError('invalid_name', '知识库名称需为 1–80 个字符')
        with self.store.transaction(write=True) as db:
            repo = self.get(db, rid)
            if not repo['release_path']:
                raise KnowledgeError('repository_not_ready', '请先成功同步仓库', 409)
            row = db.execute('SELECT * FROM libraries WHERE id=?', (library_id,)).fetchone()
            if bool(row) != existing:
                raise KnowledgeError('library_conflict', '请选择已有知识库，或使用未占用的新 ID', 409)
            binding = db.execute('SELECT * FROM repository_libraries WHERE library_id=?', (library_id,)).fetchone()
            if binding and binding['repository_id'] != rid:
                raise KnowledgeError('library_conflict', '知识库已关联其他仓库，请先解除关联', 409)
            path = repo['release_path'] + ('/' + subpath if subpath != '.' else '')
            self.registry.folder_for_path(path)
            if row:
                db.execute('UPDATE libraries SET name=?,path=?,updated_at=? WHERE id=?', (name.strip(), path, timestamp(), library_id))
            else:
                db.execute('INSERT INTO libraries VALUES (?,?,?,1,?,?)', (library_id, name.strip(), path, timestamp(), timestamp()))
            db.execute('INSERT INTO repository_libraries VALUES (?,?,?) ON CONFLICT(library_id) DO UPDATE SET subpath=excluded.subpath', (library_id,rid,subpath))
            self.store.event(db, {'kind':'admin','action':'repository_library_bind','library_id':library_id,'path':rid,'status':'ok'})
            return {'library_id': library_id, 'repository_id': rid, 'subpath': subpath}

    def unbind(self, rid, library_id):
        with self.store.transaction(write=True) as db:
            self.get(db, rid)
            db.execute('DELETE FROM repository_libraries WHERE repository_id=? AND library_id=?', (rid, library_id))
            self.store.event(db, {'kind':'admin','action':'repository_library_unbind','library_id':library_id,'path':rid,'status':'ok'})
        return {'status': 'ok'}
