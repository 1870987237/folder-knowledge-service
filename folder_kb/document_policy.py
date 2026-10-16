"""Document editing policy and atomic publication boundary shared by people and agents."""
import json
from pathlib import PurePosixPath

from .errors import KnowledgeError
from .storage import now
from .workspaces import WorkspaceSpec, resolve_workspace


def policy(db, lid):
    row = db.execute('SELECT t.metadata_json FROM library_templates b JOIN template_versions t '
                     'ON t.id=b.template_id AND t.version=b.template_version WHERE b.library_id=?', (lid,)).fetchone()
    definition = json.loads(row[0]) if row else {}
    features = definition.get('plugin') or {}
    # Unknown old update programs have not opted into reconciling external edits.
    mode = features.get('document_editing') or ('forbidden' if definition.get('capabilities') else 'allowed')
    return {'mode': mode, 'generated_paths': features.get('generated_paths', [])}


def require_path(db, lid, path):
    value = policy(db, lid)
    if value['mode'] == 'forbidden':
        raise KnowledgeError('plugin_editing_forbidden', '该插件仅允许通过更新管线修改文档，请提交材料', 409)
    path = path.casefold()
    if any(path == p.casefold() or path.startswith(p.casefold() + '/') or p.casefold().startswith(path + '/') for p in value['generated_paths']):
        raise KnowledgeError('generated_document', '该路径由插件生成，请编辑知识正文', 409)


def edit_scope(registry, db, row):
    """Return a whole local publication unit, never another library's hidden content."""
    lid = row['id']
    owner = db.execute('SELECT native FROM library_ownership WHERE library_id=?', (lid,)).fetchone()
    if not owner or policy(db, lid)['mode'] != 'allowed':
        raise KnowledgeError('editing_unsupported', '该知识库不允许直接编辑', 409)
    if db.execute('SELECT 1 FROM repository_libraries WHERE library_id=?', (lid,)).fetchone():
        raise KnowledgeError('editing_unsupported', 'Git 知识库使用仓库同步流程', 409)
    binding = db.execute('SELECT * FROM library_templates WHERE library_id=?', (lid,)).fetchone()
    spec = WorkspaceSpec.model_validate_json(binding['workspace_json']) if binding and binding['workspace_json'] else None
    scope = resolve_workspace(registry, db, row, spec)
    if scope['spec'].type != 'local' or scope['spec'].root_path == '.':
        raise KnowledgeError('editing_unsupported', '直接编辑需要独立的本地工作目录', 409)
    if not (binding and binding['workspace_json']) and (not owner[0] or not row['path'].startswith('document-releases/')):
        raise KnowledgeError('editing_unsupported', '挂载目录需先配置支持直接编辑的管理插件', 409)
    root = scope['root']
    for other in db.execute('SELECT id,path FROM libraries WHERE id<>?', (lid,)):
        path = (registry.pool.root / other['path']).resolve()
        if root == path or root.is_relative_to(path) or path.is_relative_to(root):
            raise KnowledgeError('editing_unsupported', '共享工作目录暂不支持直接编辑', 409)
    if binding and binding['workspace_key'] and db.execute('SELECT 1 FROM library_templates WHERE workspace_key=? AND library_id<>?', (binding['workspace_key'], lid)).fetchone():
        raise KnowledgeError('editing_unsupported', '共享工作目录暂不支持直接编辑', 409)
    return dict(scope, managed=bool(binding and binding['workspace_json']))


def publish_pointer(registry, db, row, scope, target):
    root = target.relative_to(registry.pool.root).as_posix()
    path = (PurePosixPath(root) / scope['spec'].knowledge_subpath).as_posix()
    db.execute('UPDATE libraries SET path=?,updated_at=? WHERE id=?', (path, now(), row['id']))
    if scope['managed']:
        db.execute('INSERT INTO workspace_publications VALUES (?,?,?) ON CONFLICT(workspace_key) '
                   'DO UPDATE SET root_path=excluded.root_path,job_id=excluded.job_id', (scope['workspace_key'], root, None))
    return path
