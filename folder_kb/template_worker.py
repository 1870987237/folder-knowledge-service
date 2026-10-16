"""Reviewed Python template worker. Separate process; not a sandbox for untrusted code."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

from .core import is_link
from .errors import KnowledgeError
from .worker_runtime import worker_lock, worker_heartbeat
from .management import Registry
from .storage import now
from .template_protocol import JobRequest, Record, prepare_job, tree_digest, validate_result
from .updates import Updates, encode
from .workspaces import excluded, workspace_digest, workspace_files


class TemplateWorker:
    def __init__(self, registry):
        self.registry, self.store = registry, registry.store
        self.updates = Updates(registry)
        self.jobs_root = self.updates.jobs_root
        self.jobs_root.mkdir(exist_ok=True)
        if is_link(self.jobs_root.lstat()):
            raise ValueError('Unsafe job directory')

    def heartbeat(self):
        with self.store.transaction(write=True) as db:
            db.execute("INSERT INTO metadata VALUES ('template_worker_heartbeat',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (now(),))

    def fail(self, job_id, code):
        with self.store.transaction(write=True) as db:
            db.execute("UPDATE update_jobs SET status='failed',error_code=?,updated_at=? WHERE id=? AND status!='published'", (code, now(), job_id))
            db.execute("UPDATE inbox_items SET status='pending',claim_digest=NULL,lease_until=NULL,updated_at=? WHERE id IN (SELECT item_id FROM update_job_items WHERE job_id=?) AND status='processing'", (now(), job_id))
            self.updates.event(db, 'update_failed', job_id)

    def recover(self):
        with self.store.transaction() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM update_jobs WHERE status IN ('running','publishing')")]
        for job_id in ids:
            self.fail(job_id, 'worker_interrupted')

    def tick(self):
        self.heartbeat()
        with self.store.transaction(write=True) as db:
            job = db.execute("SELECT * FROM update_jobs WHERE status IN ('publish_queued','queued') ORDER BY CASE status WHEN 'publish_queued' THEN 0 ELSE 1 END,created_at LIMIT 1").fetchone()
            if job:
                db.execute('UPDATE update_jobs SET status=?,updated_at=? WHERE id=?', ('publishing' if job['status']=='publish_queued' else 'running', now(), job['id']))
        if job:
            try:
                self.publish(job) if job['status']=='publish_queued' else self.execute(job)
            except KnowledgeError as exc:
                self.fail(job['id'], exc.code)
            except subprocess.TimeoutExpired:
                self.fail(job['id'], 'template_timeout')
            except subprocess.CalledProcessError:
                self.fail(job['id'], 'template_process_failed')
            except (ValueError, OSError, KeyError):
                # Never persist exception text: programs/HTTP providers can echo credentials.
                self.fail(job['id'], 'template_validation_failed')
            return True
        with self.store.transaction() as db:
            policies = [(r[0], json.loads(r[1])) for r in db.execute('SELECT library_id,definition FROM update_policies')]
        for library_id, policy in policies:
            if policy['enabled'] and policy['auto_trigger']:
                try:
                    self.updates.enqueue(library_id)
                    return True
                except KnowledgeError:
                    continue
        return False

    def current(self, db, job):
        if job['approved_by']:
            from .library_updates import LibraryUpdates
            LibraryUpdates(self.registry).workspace_access(db, {'id': job['approved_by']}, job['library_id'], job=job)
        context = self.updates.context(db, job['library_id'])
        if encode(context) != job['context_json']:
            raise KnowledgeError('stale_update_context', 'Workspace configuration changed', 409)
        from .update_lifecycle import check_bootstrap
        check_bootstrap(db, self.registry, job, context)
        items = db.execute('SELECT i.* FROM inbox_items i JOIN update_job_items j ON j.item_id=i.id WHERE j.job_id=? ORDER BY i.sequence', (job['id'],)).fetchall()
        if any(i['status'] != 'processing' or not self.updates.permitted(db, i) for i in items):
            raise KnowledgeError('submission_unavailable', 'Submission permission changed', 409)
        return context, [Record(id=i['id'], material=json.loads(i['payload'])) for i in items]

    def environment(self, context):
        definition = context['template']['definition']
        if os.getenv('KB_TEMPLATE_TRUSTED_PYTHON') != '1' or definition['runtime']['type'] != 'trusted-python-demo':
            raise KnowledgeError('template_runtime_disabled', 'Enable reviewed Python execution on the worker', 409)
        env = {k: os.environ[k] for k in ('SYSTEMROOT','WINDIR','TEMP','TMP','PATH') if k in os.environ}
        env['PYTHONIOENCODING'] = 'utf-8'
        injected = {}
        if model := context['model']:
            key = ''
            if name := model['api_key_env']:
                allowed = {n.strip() for n in os.getenv('KB_MODEL_KEY_ALLOWLIST','').split(',') if n.strip()}
                if name not in allowed or not os.getenv(name):
                    raise KnowledgeError('model_credential_unavailable', 'Credential is absent or not in the worker allowlist', 409)
                key = os.environ[name]
            values = {'base_url': model['base_url'], 'model': model['model'], 'api_key': key}
            injected = {name: values[field] for name, field in context['policy']['model_environment'].items()}
        if any(not injected.get(name) for name in definition['required_secrets']):
            raise KnowledgeError('template_secrets_unavailable', 'Required template credentials are not mapped', 409)
        env.update(injected)
        return env

    def run(self, command, cwd, env, timeout):
        options = {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name=='nt' else {'start_new_session': True}
        process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False, **options)
        deadline = time.monotonic()+timeout
        try:
            while process.poll() is None:
                self.heartbeat()
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    process.wait(timeout=min(2, remaining))
                except subprocess.TimeoutExpired:
                    pass
            if process.returncode:
                raise subprocess.CalledProcessError(process.returncode, command)
        finally:
            if process.poll() is None:
                if os.name=='nt':
                    subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()

    def execute(self, job):
        with self.store.transaction() as db:
            context, records = self.current(db, job)
        definition, spec = context['template']['definition'], context['spec']
        env = self.environment(context)
        source = Path(context['root'])
        request = JobRequest(job_id=job['id'], operation=job['operation'], library_id=job['library_id'],
            base_version=workspace_digest(source, spec['exclude_paths']),
            template={'id': definition['id'], 'version': definition['version'], 'package_sha256': context['template']['package_sha256']},
            config=json.loads(context['binding']['config_json']), knowledge_subpath=spec['knowledge_subpath'],
            workspace_key=context['workspace_key'], exclude_paths=spec['exclude_paths'])
        package = self.updates.catalog.version_path(definition['id'], definition['version'])/'package'
        if tree_digest(package) != request.template.package_sha256:
            raise KnowledgeError('template_package_changed', 'Installed package was modified', 409)
        directory = self.jobs_root/job['id']
        prepare_job(directory, request, records, source)
        before = tree_digest(directory/'input')
        argv = definition['entrypoints'][job['operation']]
        command = [sys.executable, '-I', '-B', str(package/argv[1]), *argv[2:],
                   '--input', str(directory/'input'), '--work', str(directory/'work'), '--output', str(directory/'output')]
        self.run(command, directory/'work', env, definition['runtime']['timeout_seconds'])
        if before != tree_digest(directory/'input') or tree_digest(package) != request.template.package_sha256:
            raise KnowledgeError('template_input_changed', 'Program changed its input or installed package', 409)
        result = validate_result(directory, request, records)
        candidate = directory/'output/workspace'
        for required in definition['output']['required_files']:
            if not (candidate/spec['knowledge_subpath']/required).is_file():
                raise ValueError('Missing required output')
        digest = workspace_digest(candidate)
        with self.store.transaction(write=True) as db:
            self.current(db, job)
            db.execute("UPDATE update_jobs SET status='candidate_ready',base_version=?,candidate_digest=?,result_json=?,updated_at=? WHERE id=?",
                       (request.base_version, digest, result.model_dump_json(), now(), job['id']))
            self.updates.event(db, 'update_candidate_ready', job['id'], job['library_id'])
            from .update_lifecycle import bootstrap_job
            if bootstrap_job(db, job['id']):
                # Creation authorizes only the untouched seed structure; all publication
                # integrity, permission and source-version checks still run in publish().
                db.execute("UPDATE update_jobs SET status='publish_queued' WHERE id=?", (job['id'],))

    def publish(self, job):
        with self.store.transaction() as db:
            context, records = self.current(db, job)
        directory = self.jobs_root/job['id']
        request = JobRequest.model_validate_json((directory/'input/request.json').read_text(encoding='utf-8'))
        result = validate_result(directory, request, records)
        candidate, source = directory/'output/workspace', Path(context['root'])
        if encode(result.model_dump()) != encode(json.loads(job['result_json'])):
            raise KnowledgeError('candidate_changed', 'Reviewed result was modified', 409)
        if workspace_digest(candidate) != job['candidate_digest'] or request.base_version != job['base_version']:
            raise KnowledgeError('candidate_changed', 'Reviewed candidate changed', 409)
        if workspace_digest(source, request.exclude_paths) != job['base_version']:
            raise KnowledgeError('workspace_changed', 'Published workspace changed; regenerate the candidate', 409)
        releases = self.registry.pool.root/'template-releases'
        releases.mkdir(exist_ok=True)
        if is_link(releases.lstat()):
            raise ValueError('Unsafe release directory')
        target = releases/job['id']
        target.mkdir(exist_ok=False)
        # A fresh complete release keeps excluded files private and intact. No in-place writes.
        tree_digest(source)
        entries = list(source.rglob('*'))
        if len(entries)>2000:
            raise ValueError('Workspace too large')
        for path in entries:
            relative = path.relative_to(source).as_posix()
            if excluded(relative, request.exclude_paths):
                if any(relative==d or relative.startswith(d+'/') for d in result.deleted_paths):
                    raise ValueError('Deletion would remove excluded data')
                output = target/relative
                if path.is_dir():
                    output.mkdir(parents=True, exist_ok=True)
                else:
                    output.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, output)
        for path in workspace_files(candidate, include_dirs=True):
            output = target/path.relative_to(candidate)
            if path.is_dir():
                output.mkdir(parents=True, exist_ok=True)
            else:
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, output)
        if workspace_digest(target, request.exclude_paths) != job['candidate_digest']:
            raise ValueError('Release integrity mismatch')
        relative_root = target.relative_to(self.registry.pool.root).as_posix()
        with self.store.transaction(write=True) as db:
            self.current(db, job)
            if workspace_digest(source, request.exclude_paths) != job['base_version'] or workspace_digest(candidate) != job['candidate_digest']:
                raise KnowledgeError('workspace_changed', 'Source or candidate changed during publication', 409)
            for view in context['views']:
                scope = json.loads(view['workspace_json'])
                path = (Path(relative_root)/scope['knowledge_subpath']).as_posix()
                self.registry.folder_for_path(path)
                db.execute('UPDATE libraries SET path=?,updated_at=? WHERE id=?', (path, now(), view['library_id']))
            db.execute('INSERT INTO workspace_publications VALUES (?,?,?) ON CONFLICT(workspace_key) DO UPDATE SET root_path=excluded.root_path,job_id=excluded.job_id', (context['workspace_key'], relative_root, job['id']))
            for item in result.materials:
                receipt = item.model_dump(exclude={'record_id'}) | {'job_id': job['id'], 'version': job['candidate_digest'], 'published': True}
                db.execute("UPDATE inbox_items SET status='completed',result=?,claim_digest=NULL,lease_until=NULL,updated_at=? WHERE id=?", (encode(receipt), now(), item.record_id))
            db.execute("UPDATE update_jobs SET status='published',updated_at=? WHERE id=?", (now(), job['id']))
            self.updates.event(db, 'update_published', job['id'], job['library_id'])


def main():
    registry = Registry(os.environ['KB_ROOT'], os.environ['KB_STATE_DIR'])
    worker = TemplateWorker(registry)
    def stop(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    with worker_lock(registry.directory, 'template-worker.lock'), worker_heartbeat(registry.store, 'updates'):
        worker.recover()
        while True:
            if not worker.tick():
                time.sleep(2)



if __name__ == '__main__':
    main()
