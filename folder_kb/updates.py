"""Durable template jobs. Publishing always requires an explicit admin approval."""
from __future__ import annotations

import difflib
import json
import re
import uuid
from urllib.parse import urlsplit

from fastapi import Query
from pydantic import Field, model_validator

from .errors import KnowledgeError
from .inbox import StrictModel
from .storage import now
from .templates import TemplateCatalog
from .workspaces import WorkspaceSpec, resolve_workspace, workspace_files

UPDATE_SCHEMA = (
    'CREATE TABLE IF NOT EXISTS model_connections (id TEXT PRIMARY KEY, definition TEXT NOT NULL)',
    '''CREATE TABLE IF NOT EXISTS update_policies (library_id TEXT PRIMARY KEY REFERENCES libraries(id),
       definition TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS update_jobs (id TEXT PRIMARY KEY, library_id TEXT NOT NULL REFERENCES libraries(id),
       workspace_key TEXT NOT NULL, operation TEXT NOT NULL, status TEXT NOT NULL,
       created_at TEXT NOT NULL, updated_at TEXT NOT NULL, context_json TEXT NOT NULL,
       base_version TEXT, candidate_digest TEXT, result_json TEXT, error_code TEXT)''',
    '''CREATE UNIQUE INDEX IF NOT EXISTS update_workspace_active ON update_jobs(workspace_key)
       WHERE status IN ('queued','running','candidate_ready','publish_queued','publishing')''',
    '''CREATE TABLE IF NOT EXISTS update_job_items (job_id TEXT NOT NULL REFERENCES update_jobs(id),
       item_id TEXT NOT NULL REFERENCES inbox_items(id), PRIMARY KEY(job_id,item_id))''',
    'CREATE INDEX IF NOT EXISTS update_item_jobs ON update_job_items(item_id)',
    '''CREATE TABLE IF NOT EXISTS workspace_publications (workspace_key TEXT PRIMARY KEY, root_path TEXT NOT NULL,
       job_id TEXT NOT NULL REFERENCES update_jobs(id))''',
)
ACTIVE = ('queued', 'running', 'candidate_ready', 'publish_queued', 'publishing')


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class ModelConnection(StrictModel):
    id: str = Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,63}$')
    name: str = Field(min_length=1, max_length=100)
    base_url: str = Field(max_length=1024)
    model: str = Field(min_length=1, max_length=200)
    api_key_env: str | None = Field(default=None, pattern=r'^[A-Z][A-Z0-9_]{0,63}$')
    enabled: bool = True

    @model_validator(mode='after')
    def safe_url(self):
        parsed = urlsplit(self.base_url)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or
                parsed.password or parsed.query or parsed.fragment or any(c.isspace() for c in self.base_url)):
            raise ValueError('Use an HTTP(S) base URL without credentials, query or fragment')
        self.base_url = self.base_url.rstrip('/')
        return self


class UpdatePolicy(StrictModel):
    enabled: bool = False
    auto_trigger: bool = True
    model_connection_id: str | None = Field(default=None, max_length=64)
    model_environment: dict[str, str] = Field(default_factory=lambda: {
        'FKB_MODEL_BASE_URL': 'base_url', 'FKB_MODEL_MODEL': 'model', 'FKB_MODEL_API_KEY': 'api_key'})
    max_batch_size: int = Field(default=10, ge=1, le=100)
    approved_package_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def safe_environment(self):
        if len(self.model_environment) > 12:
            raise ValueError('Too many environment mappings')
        for name, value in self.model_environment.items():
            if not re.fullmatch(r'[A-Z][A-Z0-9_]{0,48}_(BASE_URL|MODEL|API_KEY)', name):
                raise ValueError('Model variables must end in _BASE_URL, _MODEL or _API_KEY')
            if value not in ('base_url', 'model', 'api_key'):
                raise ValueError('Unknown model field')
        return self


class RunRequest(StrictModel):
    operation: str = Field(default='update', pattern=r'^(initialize|update)$')


class Updates:
    def __init__(self, registry):
        self.registry, self.store = registry, registry.store
        self.catalog = TemplateCatalog(registry)
        self.jobs_root = registry.directory / 'update-jobs'

    def event(self, db, action, job_id=None, library_id=None):
        self.store.event(db, {'kind': 'admin', 'action': action, 'request_id': job_id,
                              'library_id': library_id, 'status': 'ok'})

    def models(self):
        with self.store.transaction() as db:
            return {'models': [json.loads(r[0]) for r in db.execute('SELECT definition FROM model_connections ORDER BY id')]}

    def save_model(self, value):
        with self.store.transaction(write=True) as db:
            db.execute('INSERT INTO model_connections VALUES (?,?) ON CONFLICT(id) DO UPDATE SET definition=excluded.definition',
                       (value.id, encode(value.model_dump())))
            self.event(db, 'model_connection_save')
        return value.model_dump()

    def policy(self, library_id):
        self.catalog.binding(library_id)
        with self.store.transaction() as db:
            row = db.execute('SELECT definition FROM update_policies WHERE library_id=?', (library_id,)).fetchone()
            return json.loads(row[0]) if row else UpdatePolicy().model_dump()

    def save_policy(self, library_id, policy):
        binding = self.catalog.binding(library_id)['binding']
        if policy.enabled:
            if not binding:
                raise KnowledgeError('template_required', 'Bind an installed template first', 409)
            template = self.catalog.get(binding['template_id'], binding['template_version'])
            if policy.approved_package_sha256 != template['package_sha256']:
                raise KnowledgeError('package_approval_required', 'Approve the installed package digest before enabling execution', 409)
            if template['definition']['protocol_version'] != '1.1':
                raise KnowledgeError('protocol_unsupported', 'Updates require protocol 1.1', 409)
        with self.store.transaction(write=True) as db:
            if policy.model_connection_id and not db.execute('SELECT 1 FROM model_connections WHERE id=?', (policy.model_connection_id,)).fetchone():
                raise KnowledgeError('unknown_model', 'Model connection does not exist', 404)
            db.execute('INSERT INTO update_policies VALUES (?,?) ON CONFLICT(library_id) DO UPDATE SET definition=excluded.definition',
                       (library_id, encode(policy.model_dump())))
            self.event(db, 'update_policy_save', library_id=library_id)
        return policy.model_dump()

    def context(self, db, library_id):
        lib = db.execute('SELECT * FROM libraries WHERE id=? AND enabled=1', (library_id,)).fetchone()
        binding = db.execute('SELECT * FROM library_templates WHERE library_id=?', (library_id,)).fetchone()
        policy = db.execute('SELECT definition FROM update_policies WHERE library_id=?', (library_id,)).fetchone()
        if not lib or not binding or not policy or not json.loads(policy[0])['enabled']:
            raise KnowledgeError('updates_disabled', 'Enabled library, binding and update policy required', 409)
        policy = json.loads(policy[0])
        template = self.catalog.public(self.catalog.require(db, binding['template_id'], binding['template_version']))
        if template['package_sha256'] != policy['approved_package_sha256']:
            raise KnowledgeError('package_approval_required', 'Template package approval is stale', 409)
        spec = WorkspaceSpec.model_validate_json(binding['workspace_json']) if binding['workspace_json'] else None
        scope = resolve_workspace(self.registry, db, lib, spec)
        # Git releases remain owned by the Git synchronizer; never silently detach them.
        if scope['spec'].type != 'local':
            raise KnowledgeError('git_update_unsupported', 'Template publication currently requires a local workspace', 409)
        if scope['spec'].root_path == '.' or scope['spec'].root_path.split('/')[0].casefold() == 'template-releases':
            raise KnowledgeError('invalid_update_root', 'Use a dedicated workspace below the mount root', 409)
        model = None
        if policy['model_connection_id']:
            row = db.execute('SELECT definition FROM model_connections WHERE id=?', (policy['model_connection_id'],)).fetchone()
            model = json.loads(row[0]) if row else None
            if not model or not model['enabled']:
                raise KnowledgeError('model_disabled', 'Model connection is unavailable', 409)
        views = [dict(r) for r in db.execute('SELECT * FROM library_templates WHERE workspace_key=? ORDER BY library_id', (scope['workspace_key'],))]
        mounts = [dict(r) for r in db.execute('SELECT id,path,enabled FROM libraries WHERE id IN (SELECT library_id FROM library_templates WHERE workspace_key=?) ORDER BY id', (scope['workspace_key'],))]
        return {'binding': dict(binding), 'policy': policy, 'model': model, 'template': template,
                'views': views, 'mounts': mounts, 'root': str(scope['root']),
                'workspace_key': scope['workspace_key'], 'spec': scope['spec'].model_dump()}

    @staticmethod
    def permitted(db, item):
        return db.execute('''SELECT 1 FROM agent_keys k JOIN inbox_grants g ON g.key_id=k.id
            JOIN libraries l ON l.id=g.library_id WHERE k.id=? AND g.library_id=? AND l.enabled=1
            AND k.revoked_at IS NULL AND (k.expires_at IS NULL OR k.expires_at>?)''',
            (item['key_id'], item['library_id'], now())).fetchone() is not None

    def enqueue(self, library_id, operation='update', retry_id=None):
        with self.store.transaction(write=True) as db:
            context = self.context(db, library_id)
            if operation not in context['template']['definition']['capabilities']:
                raise KnowledgeError('operation_unsupported', 'Template does not support this operation', 409)
            if db.execute("SELECT 1 FROM update_jobs WHERE workspace_key=? AND status IN ('queued','running','candidate_ready','publish_queued','publishing')", (context['workspace_key'],)).fetchone():
                raise KnowledgeError('update_in_progress', 'Review or finish the current workspace job first', 409)
            items = []
            if operation == 'update':
                if retry_id:
                    items = db.execute('SELECT i.* FROM inbox_items i JOIN update_job_items j ON j.item_id=i.id WHERE j.job_id=? ORDER BY i.sequence', (retry_id,)).fetchall()
                else:
                    items = db.execute('''SELECT i.* FROM inbox_items i
                        JOIN agent_keys k ON k.id=i.key_id
                        JOIN inbox_grants g ON g.key_id=i.key_id AND g.library_id=i.library_id
                        WHERE i.library_id=? AND i.status='pending' AND k.revoked_at IS NULL
                        AND (k.expires_at IS NULL OR k.expires_at>?)
                        AND NOT EXISTS (SELECT 1 FROM update_job_items j WHERE j.item_id=i.id)
                        ORDER BY i.sequence LIMIT ?''', (library_id, now(), context['policy']['max_batch_size'])).fetchall()
                if not items:
                    raise KnowledgeError('no_pending_material', 'No new pending submissions', 409)
                if any(i['status'] != 'pending' or not self.permitted(db, i) for i in items):
                    raise KnowledgeError('submission_unavailable', 'A submission is claimed or its permission was revoked', 409)
            job_id = uuid.uuid4().hex
            db.execute('''INSERT INTO update_jobs(id,library_id,workspace_key,operation,status,created_at,updated_at,context_json)
                VALUES (?,?,?,?,'queued',?,?,?)''', (job_id, library_id, context['workspace_key'], operation, now(), now(), encode(context)))
            for item in items:
                db.execute('INSERT INTO update_job_items VALUES (?,?)', (job_id, item['id']))
                db.execute("UPDATE inbox_items SET status='processing',claim_digest=NULL,lease_until=NULL,attempts=attempts+1,updated_at=? WHERE id=?", (now(), item['id']))
            self.event(db, 'update_enqueue', job_id, library_id)
        return self.get(job_id)

    @staticmethod
    def require(db, job_id):
        row = db.execute('SELECT * FROM update_jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise KnowledgeError('unknown_update_job', 'Update job does not exist', 404)
        return row

    @staticmethod
    def public(row):
        result = {key: row[key] for key in ('id', 'library_id', 'operation', 'status', 'created_at', 'updated_at', 'base_version', 'error_code')}
        result['result'] = json.loads(row['result_json']) if row['result_json'] else None
        result['publish_mode'] = 'manual_review'
        return result

    def get(self, job_id):
        with self.store.transaction() as db:
            row = self.require(db, job_id)
            return self.public(row)

    def listing(self, offset=0, limit=50):
        with self.store.transaction() as db:
            rows = db.execute('SELECT * FROM update_jobs ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', (limit+1, offset)).fetchall()
            heartbeat = db.execute("SELECT value FROM metadata WHERE name='template_worker_heartbeat'").fetchone()
            return {'jobs': [self.public(r) for r in rows[:limit]], 'next_offset': offset+limit if len(rows)>limit else None,
                    'worker_heartbeat': heartbeat[0] if heartbeat else None}

    def act(self, job_id, action):
        if action == 'retry':
            job = self.get(job_id)
            if job['status'] != 'failed':
                raise KnowledgeError('invalid_job_state', 'Only failed jobs can be retried', 409)
            return self.enqueue(job['library_id'], job['operation'], job_id)
        with self.store.transaction(write=True) as db:
            row = self.require(db, job_id)
            if row['status'] != 'candidate_ready':
                raise KnowledgeError('invalid_job_state', 'A validated candidate is required', 409)
            if action == 'approve':
                if encode(self.context(db, row['library_id'])) != row['context_json']:
                    raise KnowledgeError('stale_update_context', 'Binding, model or policy changed; reject and regenerate', 409)
                status = 'publish_queued'
            elif action == 'reject':
                status = 'rejected'
                receipt = encode({'outcome': 'rejected', 'summary': 'Candidate rejected by administrator', 'job_id': job_id, 'published': False, 'references': []})
                db.execute("UPDATE inbox_items SET status='completed',result=?,updated_at=? WHERE id IN (SELECT item_id FROM update_job_items WHERE job_id=?)", (receipt, now(), job_id))
            else:
                raise KnowledgeError('invalid_action', 'Unknown action', 400)
            db.execute('UPDATE update_jobs SET status=?,updated_at=? WHERE id=?', (status, now(), job_id))
            self.event(db, 'update_'+action, job_id, row['library_id'])
        return self.get(job_id)

    def preview(self, job_id):
        row = self.get(job_id)
        if row['status'] not in ('candidate_ready', 'publish_queued', 'published'):
            raise KnowledgeError('candidate_unavailable', 'Candidate is not ready for review', 409)
        job = self.jobs_root / job_id
        before, after = job/'input/workspace', job/'output/workspace'
        old = {p.relative_to(before).as_posix(): p for p in workspace_files(before)}
        new = {p.relative_to(after).as_posix(): p for p in workspace_files(after)}
        changes, remaining = [], 256*1024
        for path in sorted(old.keys() | new.keys()):
            a = old[path].read_bytes() if path in old else b''
            b = new[path].read_bytes() if path in new else b''
            if a == b and path in old and path in new:
                continue
            diff = ''.join(difflib.unified_diff(a[:32768].decode('utf-8', 'replace').splitlines(True), b[:32768].decode('utf-8', 'replace').splitlines(True), fromfile='before/'+path, tofile='after/'+path))
            clipped = diff[:max(0, remaining)]
            remaining -= len(clipped)
            changes.append({'path': path, 'change': 'added' if path not in old else 'deleted' if path not in new else 'modified',
                            'diff': clipped, 'truncated': len(clipped)<len(diff) or max(len(a),len(b))>32768})
        return {'job_id': job_id, 'changes': changes}


def add_update_routes(app, registry):
    service = Updates(registry)
    app.state.updates = service

    @app.get('/admin/api/models')
    def models():
        return service.models()

    @app.put('/admin/api/models/{model_id}')
    def save_model(model_id: str, value: ModelConnection):
        if model_id != value.id:
            raise KnowledgeError('id_mismatch', 'Model ID mismatch')
        return service.save_model(value)

    @app.get('/admin/api/libraries/{library_id}/update-policy')
    def policy(library_id: str):
        return service.policy(library_id)

    @app.put('/admin/api/libraries/{library_id}/update-policy')
    def save_policy(library_id: str, value: UpdatePolicy):
        return service.save_policy(library_id, value)

    @app.post('/admin/api/libraries/{library_id}/update-jobs', status_code=202)
    def enqueue(library_id: str, value: RunRequest):
        return service.enqueue(library_id, value.operation)

    @app.get('/admin/api/update-jobs')
    def listing(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        return service.listing(offset, limit)

    @app.get('/admin/api/update-jobs/{job_id}')
    def detail(job_id: str):
        return service.get(job_id)

    @app.get('/admin/api/update-jobs/{job_id}/preview')
    def preview(job_id: str):
        return service.preview(job_id)

    @app.post('/admin/api/update-jobs/{job_id}/{action}')
    def action(job_id: str, action: str):
        return service.act(job_id, action)
