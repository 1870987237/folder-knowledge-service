"""Initialization state and narrowly scoped consent for a new library's seed structure."""
import json
from pathlib import Path

from .errors import KnowledgeError
from .workspaces import workspace_digest


BOOTSTRAP_SCHEMA = '''CREATE TABLE IF NOT EXISTS library_bootstrap (
    library_id TEXT PRIMARY KEY REFERENCES libraries(id), template_id TEXT NOT NULL,
    workspace_key TEXT NOT NULL, seed_digest TEXT NOT NULL, creator_id TEXT NOT NULL,
    job_id TEXT REFERENCES update_jobs(id))'''


def initialized(db, context):
    definition = context['template']['definition']
    if 'initialize' not in definition['capabilities']:
        return True
    for row in db.execute("SELECT context_json FROM update_jobs WHERE workspace_key=? AND status='published'",
                          (context['workspace_key'],)):
        if json.loads(row[0])['template']['definition']['id'] == definition['id']:
            return True
    if db.execute('SELECT 1 FROM library_bootstrap WHERE library_id=?', (context['binding']['library_id'],)).fetchone():
        return False
    # Imported pre-existing workspaces have no platform initialization history.
    required = definition['output']['required_files']
    root = Path(context['root']) / context['spec']['knowledge_subpath']
    return bool(required) and all((root / path).is_file() for path in required)


def bootstrap_job(db, job_id):
    return db.execute('SELECT * FROM library_bootstrap WHERE job_id=?', (job_id,)).fetchone()


def check_bootstrap(db, registry, job, context):
    record = bootstrap_job(db, job['id'])
    if not record:
        return False
    from .library_updates import LibraryUpdates
    actor = {'id': record['creator_id'], 'platform': record['creator_id'] == 'platform'}
    LibraryUpdates(registry).workspace_access(db, actor, job['library_id'], configuration=True, job=job)
    if (job['operation'] != 'initialize' or record['template_id'] != context['template']['definition']['id']
            or record['workspace_key'] != context['workspace_key']
            or record['seed_digest'] != workspace_digest(Path(context['root']), context['spec']['exclude_paths'])):
        raise KnowledgeError('bootstrap_workspace_changed', '初始化期间知识库已被修改，请检查内容后手动初始化并审核', 409)
    return True
