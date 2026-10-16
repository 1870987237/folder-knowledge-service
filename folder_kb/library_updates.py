"""Human workspace facade over the existing plugin, inbox and update services."""
import hashlib
import json
import uuid

from fastapi import Query, Request
from pydantic import Field

from .errors import KnowledgeError
from .inbox import Inbox, Material, StrictModel
from .resource_access import check_binding_access, plugin_key, require_access, scope
from .storage import now
from .templates import TemplateBinding
from .updates import RunRequest, UpdatePolicy, Updates
from .workspaces import WorkspaceSpec, resolve_workspace, workspace_digest
from .update_lifecycle import initialized


class LibraryPluginSettings(StrictModel):
    plugin_id: str = Field(min_length=1, max_length=100)
    plugin_version: str = Field(min_length=1, max_length=100)
    config: dict = Field(default_factory=dict)
    enabled: bool = False
    auto_trigger: bool = False
    model_connection_id: str | None = None
    max_batch_size: int = Field(default=10, ge=1, le=100)


class LibraryUpdates:
    def __init__(self, registry):
        from .collaboration import Collaboration
        self.registry, self.store = registry, registry.store
        self.collaboration = Collaboration(registry)
        self.updates, self.inbox = Updates(registry), Inbox(registry)
        self.catalog = self.updates.catalog

    def workspace_access(self, db, actor, lid, configuration=False, job=None):
        """Whole-workspace candidates must never cross another library's permission boundary."""
        check = self.collaboration.check_configuration if configuration else lambda db, a, l: self.collaboration.check(db, a, l, 3)
        lib, _ = check(db, actor, lid)
        binding = self.catalog.binding(lid, db)['binding']
        if binding:
            spec = WorkspaceSpec(**binding['workspace']) if binding['workspace'] else None
            workspace = resolve_workspace(self.registry, db, lib, spec)
            if workspace['spec'].type != 'local' or workspace['spec'].root_path == '.':
                raise KnowledgeError('workspace_platform_managed', '此工作目录需由平台管理员管理', 409)
            root = workspace['root']
            for other in db.execute('SELECT * FROM libraries'):
                path = self.registry.pool.root / other['path']
                if path.is_relative_to(root) or root.is_relative_to(path):
                    check(db, actor, other['id'])
        if job:
            context = json.loads(job['context_json'])
            for mount in context['mounts']:
                check(db, actor, mount['id'])
        return lib, binding

    def configure(self, db, actor, lid, value):
        lib, old = self.workspace_access(db, actor, lid, configuration=True)
        if db.execute("SELECT 1 FROM update_jobs WHERE (library_id=? OR workspace_key=?) AND status IN ('queued','running','candidate_ready','publish_queued','publishing')",
                      (lid, old['workspace_key'] if old else '')).fetchone():
            raise KnowledgeError('update_in_progress', '请先完成或拒绝当前更新候选，再修改设置', 409)
        owner = scope(db, lid)
        access = require_access(db, 'plugin', plugin_key(value.plugin_id, value.plugin_version), owner)
        template = self.catalog.require(db, value.plugin_id, value.plugin_version)
        if not template['package_sha256'] or access.get('package_sha256') != template['package_sha256']:
            raise KnowledgeError('resource_approval_stale', '插件程序包需平台重新批准', 409)
        definition = json.loads(template['metadata_json'])
        update_capable = 'update' in definition['capabilities']
        if value.enabled and not update_capable:
            raise KnowledgeError('operation_unsupported', '所选插件没有知识更新能力', 409)
        if value.model_connection_id:
            require_access(db, 'model', value.model_connection_id, owner)
            model = db.execute('SELECT definition FROM model_connections WHERE id=?', (value.model_connection_id,)).fetchone()
            if not model or not json.loads(model[0])['enabled']:
                raise KnowledgeError('model_disabled', '模型连接不可用', 409)
        if value.enabled and definition['required_secrets'] and not value.model_connection_id:
            raise KnowledgeError('model_required', '该插件需要选择已授权模型连接', 409)
        if value.enabled and any(name not in access['model_environment'] for name in definition['required_secrets']):
            raise KnowledgeError('model_mapping_required', '请平台管理员完善插件的模型变量映射', 409)
        native = db.execute('SELECT native FROM library_ownership WHERE library_id=?', (lid,)).fetchone()[0]
        workspace = WorkspaceSpec(**old['workspace']) if old and old['workspace'] else None
        if native and update_capable:
            if not old and (definition.get('plugin') or {}).get('default_knowledge_subpath', '.') != '.':
                raise KnowledgeError('workspace_setup_required', '该模板需要独立知识子目录；请新建模板库，或由平台管理员配置完整工作范围', 409)
            if not self.collaboration.editable(db, lid, lib['path']):
                raise KnowledgeError('workspace_not_independent', '模板更新需要独立知识库工作目录', 409)
            if db.execute("SELECT 1 FROM document_drafts WHERE library_id=? AND status IN ('draft','submitted')", (lid,)).fetchone():
                raise KnowledgeError('drafts_pending', '请先处理未完成的文档草稿', 409)
            # Preserve the logical workspace after publications; editing policy belongs to the plugin.
            workspace = workspace or WorkspaceSpec(type='local', root_path=lib['path'])
            if (definition.get('plugin') or {}).get('document_editing') != 'allowed':
                db.execute('UPDATE library_ownership SET native=0 WHERE library_id=?', (lid,))
        self.catalog.bind(lid, TemplateBinding(template_id=value.plugin_id, template_version=value.plugin_version,
                                             config=value.config, workspace=workspace), db)
        db.execute('INSERT OR IGNORE INTO delegated_libraries VALUES (?)', (lid,))
        self.workspace_access(db, actor, lid, configuration=True)
        policy = UpdatePolicy(enabled=value.enabled, auto_trigger=value.auto_trigger,
                              model_connection_id=value.model_connection_id, max_batch_size=value.max_batch_size,
                              model_environment=access['model_environment'], approved_package_sha256=template['package_sha256'])
        self.updates.save_policy(lid, policy, db)
        if value.enabled:
            self.updates.context(db, lid)
            self.start_bootstrap(db, lid)
        self.collaboration.event(db, actor, 'library_plugin_configure', lid)

    def start_bootstrap(self, db, lid):
        record = db.execute('SELECT * FROM library_bootstrap WHERE library_id=?', (lid,)).fetchone()
        if record and record['job_id'] is None:
            context = self.updates.context(db, lid)
            if record['template_id'] == context['template']['definition']['id']:
                job = self.updates.enqueue(lid, 'initialize', db=db)
                db.execute('UPDATE library_bootstrap SET job_id=? WHERE library_id=?', (job['id'], lid))

    def create(self, actor, value):
        # One transaction creates ownership, binding and policy; invalid options leave no library.
        result = None
        try:
            with self.store.transaction(write=True) as db:
                result = self.collaboration.create_library(actor, value.name, value.organization_id, db)
                definition = json.loads(self.catalog.require(db, value.template.plugin_id, value.template.plugin_version)['metadata_json'])
                sub = (definition.get('plugin') or {}).get('default_knowledge_subpath', '.')
                if sub != '.':
                    root = 'document-releases/' + result['id']
                    (self.registry.pool.root / root / sub).mkdir(parents=True)
                    db.execute('UPDATE libraries SET path=? WHERE id=?', (root + '/' + sub, result['id']))
                    # Binding must retain the complete root while exposing only knowledge/.
                    db.execute('UPDATE library_ownership SET native=0 WHERE library_id=?', (result['id'],))
                    self.catalog.bind(result['id'], TemplateBinding(template_id=value.template.plugin_id,
                        template_version=value.template.plugin_version, config=value.template.config,
                        workspace=WorkspaceSpec(type='local', root_path=root, knowledge_subpath=sub)), db)
                self.configure(db, actor, result['id'], value.template)
                if 'initialize' in definition['capabilities']:
                    binding = self.catalog.binding(result['id'], db)['binding']
                    lib = db.execute('SELECT * FROM libraries WHERE id=?', (result['id'],)).fetchone()
                    workspace = resolve_workspace(self.registry, db, lib, WorkspaceSpec(**binding['workspace']) if binding['workspace'] else None)
                    db.execute('INSERT INTO library_bootstrap VALUES (?,?,?,?,?,NULL)',
                               (result['id'], definition['id'], workspace['workspace_key'],
                                workspace_digest(workspace['root'], workspace['spec'].exclude_paths), actor['id']))
                    if value.template.enabled:
                        self.start_bootstrap(db, result['id'])
                return result
        except Exception:
            if result:
                # Only this call's new UUID directory and seed file, never an existing library.
                parent = (self.registry.pool.root / 'document-releases').resolve()
                folder = (parent / result['id']).resolve()
                if folder.parent == parent:
                    import shutil
                    if folder.is_dir():
                        shutil.rmtree(folder)
            raise

    def settings(self, db, actor, lid):
        self.collaboration.check_configuration(db, actor, lid)
        binding = self.catalog.binding(lid, db)['binding']
        row = db.execute('SELECT definition FROM update_policies WHERE library_id=?', (lid,)).fetchone()
        policy = json.loads(row[0]) if row else UpdatePolicy().model_dump()
        availability = None
        if binding:
            try:
                check_binding_access(db, lid, binding, policy['model_connection_id'])
            except KnowledgeError as exc:
                availability = str(exc)
        return {'binding': None if not binding else {'plugin_id': binding['template_id'],
                'plugin_version': binding['template_version'], 'config': binding['config']},
                'policy': {k: policy[k] for k in ('enabled', 'auto_trigger', 'model_connection_id', 'max_batch_size')},
                'unavailable': availability}

    def submit(self, db, actor, lid, material):
        self.collaboration.check(db, actor, lid, 2)
        if material.library_id != lid:
            raise KnowledgeError('invalid_library', '材料知识库与当前知识库不一致', 400)
        if actor.get('platform'):
            raise KnowledgeError('user_required', '请使用个人账号提交材料', 403)
        payload = material.model_dump_json()
        digest = hashlib.sha256(payload.encode()).hexdigest()
        row = db.execute('SELECT * FROM inbox_items WHERE user_id=? AND library_id=? AND submission_id=?',
                         (actor['id'], lid, material.submission_id)).fetchone()
        duplicate = row is not None
        if row and row['digest'] != digest:
            raise KnowledgeError('submission_conflict', '此提交编号已用于其他材料', 409)
        if not row:
            item_id, stamp = uuid.uuid4().hex, now()
            db.execute("""INSERT INTO inbox_items(id,library_id,user_id,submission_id,payload,digest,created_at,updated_at,status)
                          VALUES (?,?,?,?,?,?,?,?,'pending')""",
                       (item_id, lid, actor['id'], material.submission_id, payload, digest, stamp, stamp))
            row = self.inbox.require_item(db, item_id)
        self.collaboration.event(db, actor, 'material_submit', lid + ':' + row['id'])
        return dict(self.inbox.record(row, False), duplicate=duplicate, knowledge_updated=False)


def add_library_update_routes(app, registry):
    service = LibraryUpdates(registry)

    @app.get('/portal/api/libraries/{lid}/plugin-settings')
    def settings(lid: str, request: Request):
        with service.store.transaction() as db:
            return service.settings(db, request.state.human, lid)

    @app.put('/portal/api/libraries/{lid}/plugin-settings')
    def configure(lid: str, value: LibraryPluginSettings, request: Request):
        with service.store.transaction(write=True) as db:
            service.configure(db, request.state.human, lid, value)
            return service.settings(db, request.state.human, lid)

    @app.post('/portal/api/libraries/{lid}/inbox', status_code=201)
    def submit(lid: str, value: Material, request: Request):
        with service.store.transaction(write=True) as db:
            return service.submit(db, request.state.human, lid, value)

    @app.get('/portal/api/libraries/{lid}/inbox')
    def inbox(lid: str, request: Request, after: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        with service.store.transaction() as db:
            _, role = service.collaboration.check(db, request.state.human, lid, 2)
            rows = db.execute('SELECT * FROM inbox_items WHERE library_id=? AND sequence>? AND (? OR user_id=?) ORDER BY sequence LIMIT ?',
                              (lid, after, role >= 3, request.state.human['id'], limit + 1)).fetchall()
            return {'items': [dict(service.inbox.record(row, False), update_job=service.inbox.job_status(db, row['id'])) for row in rows[:limit]],
                    'next_after': rows[limit - 1]['sequence'] if len(rows) > limit else None}

    @app.get('/portal/api/libraries/{lid}/inbox/{item_id}')
    def item(lid: str, item_id: str, request: Request):
        with service.store.transaction() as db:
            _, role = service.collaboration.check(db, request.state.human, lid, 2)
            row = service.inbox.require_item(db, item_id)
            if row['library_id'] != lid or (role < 3 and row['user_id'] != request.state.human['id']):
                raise KnowledgeError('inbox_not_found', '材料不存在或无权访问', 404)
            return dict(service.inbox.record(row), update_job=service.inbox.job_status(db, item_id))

    @app.get('/portal/api/libraries/{lid}/update-jobs')
    def jobs(lid: str, request: Request, offset: int = Query(0, ge=0)):
        with service.store.transaction() as db:
            service.workspace_access(db, request.state.human, lid)
            rows = db.execute('SELECT * FROM update_jobs WHERE library_id=? ORDER BY created_at DESC,id DESC LIMIT 51 OFFSET ?', (lid, offset)).fetchall()
            heartbeat = db.execute("SELECT value FROM metadata WHERE name='template_worker_heartbeat'").fetchone()
            unavailable, can_run, can_initialize = None, False, False
            initialization, pending_count, retry_count = 'unavailable', 0, 0
            try:
                context = service.updates.context(db, lid)
                active = db.execute("SELECT operation,status FROM update_jobs WHERE workspace_key=? AND status IN ('queued','running','candidate_ready','publish_queued','publishing')", (context['workspace_key'],)).fetchone()
                ready = initialized(db, context)
                pending_count = len(service.updates.pending(db, lid, True))
                retry_count = pending_count - len(service.updates.pending(db, lid))
                initialization = 'ready' if ready else 'required'
                can_run = ready and not active and pending_count > 0
                can_initialize = not ready and not active and 'initialize' in context['template']['definition']['capabilities']
                if not ready:
                    initialization = active['status'] if active and active['operation'] == 'initialize' else 'required'
                    unavailable = ('初始化候选待审核，请查看任务并审核发布。' if initialization == 'candidate_ready' else
                                   '正在初始化知识库，完成后即可处理材料。' if active else
                                   '知识库尚未初始化，暂不能处理材料。请初始化；若已有失败任务，可查看原因并重试。')
                elif active:
                    unavailable = '当前任务尚未完成，请等待处理或审核候选后再处理下一批材料。'
            except KnowledgeError as exc:
                unavailable = ('请知识库管理员配置模板并启用更新程序' if exc.code == 'updates_disabled' else exc.message)
            return {'jobs': [dict(service.updates.public(r, db), result=None) for r in rows[:50]], 'next_offset': offset + 50 if len(rows) > 50 else None,
                    'worker_heartbeat': heartbeat[0] if heartbeat else None, 'can_run': can_run,
                    'can_initialize': can_initialize, 'unavailable': unavailable,
                    'initialization': initialization, 'pending_count': pending_count, 'retry_count': retry_count}

    @app.post('/portal/api/libraries/{lid}/update-jobs', status_code=201)
    def enqueue(lid: str, value: RunRequest, request: Request):
        with service.store.transaction(write=True) as db:
            service.workspace_access(db, request.state.human, lid)
            job = service.updates.enqueue(lid, value.operation, db=db, include_failed=True)
            service.collaboration.event(db, request.state.human, 'library_update_enqueue', lid + ':' + job['id'])
            return job

    def check_job(db, actor, lid, job_id):
        service.collaboration.check(db, actor, lid, 3)
        job = service.updates.require(db, job_id)
        if job['library_id'] != lid:
            raise KnowledgeError('unknown_update_job', '任务不存在或无权访问', 404)
        service.workspace_access(db, actor, lid, job=job)
        return job

    @app.get('/portal/api/libraries/{lid}/update-jobs/{job_id}')
    def job(lid: str, job_id: str, request: Request):
        with service.store.transaction() as db:
            return service.updates.public(check_job(db, request.state.human, lid, job_id), db)

    @app.get('/portal/api/libraries/{lid}/update-jobs/{job_id}/preview')
    def preview(lid: str, job_id: str, request: Request):
        with service.store.transaction() as db:
            check_job(db, request.state.human, lid, job_id)
            return service.updates.preview(job_id)

    @app.post('/portal/api/libraries/{lid}/update-jobs/{job_id}/{action}')
    def action(lid: str, job_id: str, action: str, request: Request):
        with service.store.transaction(write=True) as db:
            check_job(db, request.state.human, lid, job_id)
            result = service.updates.act(job_id, action, db)
            if action == 'approve' and not request.state.human.get('platform'):
                db.execute('UPDATE update_jobs SET approved_by=? WHERE id=?', (request.state.human['id'], job_id))
            service.collaboration.event(db, request.state.human, 'library_update_' + action, lid + ':' + job_id)
            return result
