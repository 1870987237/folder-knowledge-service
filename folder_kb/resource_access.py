"""Platform resource grants; library ownership is the resource-use boundary."""
import json

from fastapi import Request
from pydantic import Field, field_validator

from .errors import KnowledgeError
from .inbox import StrictModel
from .updates import UpdatePolicy


RESOURCE_SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS resource_access (kind TEXT NOT NULL, resource_id TEXT NOT NULL,
       definition TEXT NOT NULL, PRIMARY KEY(kind,resource_id))''',
    '''CREATE TABLE IF NOT EXISTS delegated_libraries (library_id TEXT PRIMARY KEY REFERENCES libraries(id))''',
)


class ResourceGrant(StrictModel):
    organization_ids: list[str] = Field(default_factory=list, max_length=500)
    user_ids: list[str] = Field(default_factory=list, max_length=500)
    approved: bool = False
    model_environment: dict[str, str] = Field(default_factory=lambda: UpdatePolicy().model_environment)

    @field_validator('model_environment')
    @classmethod
    def safe_mapping(cls, value):
        return UpdatePolicy(model_environment=value).model_environment


def plugin_key(plugin_id, version):
    return plugin_id + '@' + version


def scope(db, library_id):
    row = db.execute('SELECT owner_id,organization_id FROM library_ownership WHERE library_id=?', (library_id,)).fetchone()
    if not row or not (row['owner_id'] or row['organization_id']):
        raise KnowledgeError('ownership_required', '请先为知识库指定组织或个人归属', 409)
    return dict(row)


def grant(db, kind, resource_id):
    row = db.execute('SELECT definition FROM resource_access WHERE kind=? AND resource_id=?', (kind, resource_id)).fetchone()
    return json.loads(row[0]) if row else ResourceGrant().model_dump()


def allowed(value, owner):
    return bool(value.get('approved') and (owner['organization_id'] in value['organization_ids']
                if owner['organization_id'] else owner['owner_id'] in value['user_ids']))


def require_access(db, kind, resource_id, owner):
    value = grant(db, kind, resource_id)
    if not allowed(value, owner):
        raise KnowledgeError('resource_forbidden', '该资源未授权给知识库所属组织或个人，请联系平台管理员', 403)
    return value


def check_binding_access(db, library_id, binding, model_id=None):
    """Legacy platform bindings retain their explicit approval; delegated bindings recheck grants."""
    if not db.execute('SELECT 1 FROM delegated_libraries WHERE library_id=?', (library_id,)).fetchone():
        return
    owner = scope(db, library_id)
    access = require_access(db, 'plugin', plugin_key(binding['template_id'], binding['template_version']), owner)
    row = db.execute('SELECT package_sha256 FROM template_versions WHERE id=? AND version=?',
                     (binding['template_id'], binding['template_version'])).fetchone()
    if not row or access.get('package_sha256') != row[0]:
        raise KnowledgeError('resource_approval_stale', '插件程序包需平台重新批准', 409)
    if model_id:
        require_access(db, 'model', model_id, owner)
    return access


def check_workspace_owner(db, registry, library_id, root):
    if not db.execute('SELECT 1 FROM delegated_libraries WHERE library_id=?', (library_id,)).fetchone():
        return
    owner = scope(db, library_id)
    for lib in db.execute('SELECT id,path FROM libraries'):
        path = registry.pool.root / lib['path']
        if path.is_relative_to(root) or root.is_relative_to(path):
            if scope(db, lib['id']) != owner:
                raise KnowledgeError('workspace_ownership_conflict', '更新工作目录包含其他归属的知识库，请平台管理员分离目录', 409)


def add_resource_routes(app, registry):
    from .templates import TemplateCatalog
    from .collaboration import Collaboration
    catalog, collaboration = TemplateCatalog(registry), Collaboration(registry)

    @app.get('/admin/api/resource-subjects')
    def subjects():
        with registry.store.transaction() as db:
            return {'organizations': [dict(r) for r in db.execute('SELECT id,name FROM organizations ORDER BY name')],
                    'users': [dict(r) for r in db.execute("SELECT id,username FROM users WHERE status='active' ORDER BY username")]}

    @app.get('/admin/api/resource-access/{kind}/{resource_id}')
    def get_access(kind: str, resource_id: str):
        with registry.store.transaction() as db:
            return grant(db, kind, resource_id)

    @app.put('/admin/api/resource-access/{kind}/{resource_id}')
    def save_access(kind: str, resource_id: str, value: ResourceGrant):
        with registry.store.transaction(write=True) as db:
            data = value.model_dump()
            if kind == 'plugin':
                parts = resource_id.rsplit('@', 1)
                if len(parts) != 2:
                    raise KnowledgeError('invalid_resource', '插件资源需包含版本', 400)
                row = catalog.require(db, *parts)
                if value.approved and not row['package_sha256']:
                    raise KnowledgeError('template_not_installed', '请先安装程序包', 409)
                data['package_sha256'] = row['package_sha256']
            elif kind == 'model':
                if not db.execute('SELECT 1 FROM model_connections WHERE id=?', (resource_id,)).fetchone():
                    raise KnowledgeError('unknown_model', '模型不存在', 404)
            else:
                raise KnowledgeError('invalid_resource', '不支持的资源类型', 400)
            for table, ids in [('organizations', value.organization_ids), ('users', value.user_ids)]:
                if any(not db.execute(f'SELECT 1 FROM {table} WHERE id=?', (i,)).fetchone() for i in ids):
                    raise KnowledgeError('invalid_resource_subject', '授权对象不存在', 400)
            db.execute('INSERT INTO resource_access VALUES (?,?,?) ON CONFLICT(kind,resource_id) DO UPDATE SET definition=excluded.definition',
                       (kind, resource_id, json.dumps(data)))
            collaboration.event(db, {'id': 'platform'}, 'resource_access_save', kind + ':' + resource_id)
            return data

    @app.get('/portal/api/resources')
    def resources(request: Request, organization_id: str | None = None, library_id: str | None = None):
        actor = request.state.human
        with registry.store.transaction() as db:
            if library_id:
                collaboration.check_configuration(db, actor, library_id)
                owner = scope(db, library_id)
            else:
                if organization_id:
                    collaboration.org_check(db, actor, organization_id)
                owner = {'owner_id': actor['id'], 'organization_id': organization_id}
            plugins = []
            for row in db.execute('SELECT * FROM template_versions WHERE package_sha256 IS NOT NULL ORDER BY id,version'):
                access = grant(db, 'plugin', plugin_key(row['id'], row['version']))
                if allowed(access, owner) and access.get('package_sha256') == row['package_sha256']:
                    definition = json.loads(row['metadata_json'])
                    plugins.append({k: definition[k] for k in ('id', 'version', 'name', 'description', 'capabilities', 'config_schema', 'required_secrets')})
            models = []
            for row in db.execute('SELECT * FROM model_connections ORDER BY id'):
                model = json.loads(row['definition'])
                if model['enabled'] and allowed(grant(db, 'model', row['id']), owner):
                    models.append({k: model[k] for k in ('id', 'name', 'model')})
            return {'plugins': plugins, 'models': models}
