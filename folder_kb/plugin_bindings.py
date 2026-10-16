"""Independent retrieval slot; absence preserves the legacy combined binding."""
import json

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .errors import KnowledgeError
from .template_protocol import tree_digest


SCHEMA = '''CREATE TABLE IF NOT EXISTS library_search_bindings (
    library_id TEXT PRIMARY KEY REFERENCES libraries(id),
    template_id TEXT, template_version TEXT, config_json TEXT NOT NULL, delegated INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(template_id,template_version) REFERENCES template_versions(id,version))'''


def search_binding(db, lid):
    row = db.execute('SELECT * FROM library_search_bindings WHERE library_id=?', (lid,)).fetchone()
    if row is not None:
        return dict(row) if row['template_id'] else None
    row = db.execute('SELECT * FROM library_templates WHERE library_id=?', (lid,)).fetchone()
    return dict(row) if row else None


def search_definition(db, lid):
    binding = search_binding(db, lid)
    if not binding:
        return {}
    row = db.execute('SELECT metadata_json FROM template_versions WHERE id=? AND version=?',
                     (binding['template_id'], binding['template_version'])).fetchone()
    return (json.loads(row[0]).get('plugin') or {}) if row else {}


def get_search(catalog, lid):
    with catalog.store.transaction() as db:
        catalog.binding(lid, db)  # existence check
        binding = search_binding(db, lid)
        inherited = not db.execute('SELECT 1 FROM library_search_bindings WHERE library_id=?', (lid,)).fetchone()
        return {'inherited': inherited, 'binding': None if not binding else {
            'plugin_id': binding['template_id'], 'plugin_version': binding['template_version'],
            'config': json.loads(binding['config_json'])}}


def set_search(catalog, lid, value, actor=None):
    from .collaboration import Collaboration
    from .resource_access import require_access, scope, plugin_key
    from .retrieval import supported
    with catalog.store.transaction(write=True) as db:
        Collaboration(catalog.registry).check_configuration(db, actor or {'platform': True}, lid)
        if value is not None:
            if value.workspace is not None:
                raise KnowledgeError('index_workspace_unsupported', '检索插件只读取知识库可见目录', 422)
            row = catalog.require(db, value.plugin_id, value.plugin_version)
            definition = json.loads(row['metadata_json'])
            if not supported((definition.get('plugin') or {}).get('indexing')):
                raise KnowledgeError('index_unsupported', '所选插件不提供可运行的检索能力', 409)
            if not row['package_sha256']:
                raise KnowledgeError('template_not_installed', '请先安装插件', 409)
            if actor and not actor.get('platform') or db.execute('SELECT 1 FROM delegated_libraries WHERE library_id=?', (lid,)).fetchone():
                access = require_access(db, 'plugin', plugin_key(value.plugin_id, value.plugin_version), scope(db, lid))
                if access.get('package_sha256') != row['package_sha256']:
                    raise KnowledgeError('resource_approval_stale', '插件程序包需平台重新批准', 409)
            try:
                Draft202012Validator(definition['config_schema']).validate(value.config)
            except ValidationError:
                raise KnowledgeError('invalid_template_config', '配置不符合插件规范', 422) from None
            if tree_digest(catalog.version_path(value.plugin_id, value.plugin_version) / 'package') != row['package_sha256']:
                raise KnowledgeError('template_package_changed', '插件程序包发生变化', 409)
        db.execute('INSERT INTO library_search_bindings VALUES (?,?,?,?,?) ON CONFLICT(library_id) '
                   'DO UPDATE SET template_id=excluded.template_id,template_version=excluded.template_version,config_json=excluded.config_json,delegated=excluded.delegated',
                   (lid, value.plugin_id if value else None, value.plugin_version if value else None,
                    json.dumps(value.config if value else {}), bool(actor and not actor.get('platform'))))
        catalog.store.event(db, {'kind': 'admin', 'action': 'search_plugin_bind' if value else 'search_plugin_unbind', 'library_id': lid, 'status': 'ok'})
    return get_search(catalog, lid)
