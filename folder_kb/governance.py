"""Library custody and organization-wide sharing, independent of file storage."""
import json
from typing import Literal

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field

from .errors import KnowledgeError
from .storage import now

GOVERNANCE_SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS library_governance (
       library_id TEXT PRIMARY KEY REFERENCES libraries(id),
       organization_access TEXT NOT NULL DEFAULT 'restricted',
       custody_native INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0)''',
    '''CREATE TABLE IF NOT EXISTS library_governance_events (
       id INTEGER PRIMARY KEY, library_id TEXT NOT NULL REFERENCES libraries(id),
       actor_id TEXT NOT NULL, previous_organization_id TEXT, organization_id TEXT,
       revoked_keys INTEGER NOT NULL, created_at TEXT NOT NULL)''',
)


class Transfer(BaseModel):
    model_config = ConfigDict(extra='forbid')
    organization_id: str | None = Field(min_length=1, max_length=64)
    expected_revision: int = Field(ge=0)


class Sharing(BaseModel):
    model_config = ConfigDict(extra='forbid')
    organization_access: Literal['restricted', 'org_read']
    expected_revision: int = Field(ge=0)


def metadata(db, lid):
    row = db.execute('SELECT * FROM library_governance WHERE library_id=?', (lid,)).fetchone()
    return dict(row) if row else {'organization_access': 'restricted', 'custody_native': 0, 'revision': 0}


class Governance:
    def __init__(self, registry):
        self.registry, self.store = registry, registry.store

    def state(self, db, lid):
        row = db.execute('SELECT l.*,o.owner_id,o.organization_id,o.native FROM libraries l '
                         'LEFT JOIN library_ownership o ON o.library_id=l.id WHERE l.id=?', (lid,)).fetchone()
        if not row:
            raise KnowledgeError('unknown_library', '知识库不存在', 404)
        meta = metadata(db, lid)
        return dict(row, organization_access=meta['organization_access'], revision=meta['revision'],
                    native=bool(row['native'] or meta['custody_native']))

    def get(self, lid):
        with self.store.transaction() as db:
            return self.state(db, lid)

    def transfer(self, lid, value: Transfer, actor_id='platform', initial_only=False):
        from .capabilities import bump
        with self.store.transaction(write=True) as db:
            before = self.state(db, lid)
            if initial_only and (before['organization_id'] or before['owner_id']):
                raise KnowledgeError('already_owned', '此库已有归属，请使用知识库归属管理', 409)
            if before['owner_id']:
                raise KnowledgeError('private_transfer_unsupported', '个人库转移需所有者交接流程，当前不支持', 409)
            if before['revision'] != value.expected_revision:
                raise KnowledgeError('governance_changed', '归属或共享策略已变化，请刷新后重试', 409)
            target_org = value.organization_id
            if target_org and not db.execute('SELECT 1 FROM organizations WHERE id=?', (target_org,)).fetchone():
                raise KnowledgeError('unknown_organization', '目标组织不存在', 404)
            if target_org == before['organization_id']:
                return before
            if db.execute("SELECT 1 FROM document_drafts WHERE library_id=? AND status IN ('draft','submitted')", (lid,)).fetchone():
                raise KnowledgeError('transfer_busy', '存在未完成草稿，请先发布或退回并完成处理', 409)
            if db.execute("SELECT 1 FROM inbox_items WHERE library_id=? AND status<>'completed'", (lid,)).fetchone():
                raise KnowledgeError('transfer_busy', '存在未处理材料，请先完成收件箱处理', 409)
            if db.execute("SELECT 1 FROM update_jobs WHERE library_id=? AND status IN ('queued','running','candidate_ready','publish_queued','publishing')", (lid,)).fetchone():
                raise KnowledgeError('transfer_busy', '存在未完成更新任务，请先完成或拒绝候选', 409)
            binding = db.execute('SELECT workspace_key FROM library_templates WHERE library_id=?', (lid,)).fetchone()
            if binding and binding[0] and db.execute('SELECT 1 FROM library_templates WHERE workspace_key=? AND library_id<>?', (binding[0], lid)).fetchone():
                raise KnowledgeError('shared_workspace_transfer', '多个知识库共用更新工作目录，需先解除关联后再转移', 409)
            if target_org:
                target = self.registry.checked_folder(before).root
                documents = self.registry.pool.root / 'document-releases'
                if target == self.registry.pool.root or (not before['native'] and (target.is_relative_to(documents) or documents.is_relative_to(target))):
                    raise KnowledgeError('reserved_document_scope', '不能把挂载根或平台发布目录作为组织库分配', 409)
                for other in db.execute('SELECT * FROM libraries WHERE id<>?', (lid,)):
                    # Paths are normalized at registration; include unassigned aliases too.
                    p, q = before['path'].rstrip('/'), other['path'].rstrip('/')
                    if p == q or p.startswith(q + '/') or q.startswith(p + '/') or q == '.':
                        raise KnowledgeError('overlapping_scope', '目录与其他知识库重叠，请先解除重叠挂载', 409)
            keys = [r[0] for r in db.execute('SELECT key_id FROM key_libraries WHERE library_id=?', (lid,))]
            for kid in keys:
                bump(db, kid)
            db.execute('DELETE FROM key_libraries WHERE library_id=?', (lid,))
            db.execute('DELETE FROM library_members WHERE library_id=?', (lid,))
            db.execute('DELETE FROM library_unit_grants WHERE library_id=?', (lid,))
            db.execute('UPDATE library_agent_settings SET direct_enabled=0 WHERE library_id=?', (lid,))
            policy = db.execute('SELECT definition FROM update_policies WHERE library_id=?', (lid,)).fetchone()
            if policy:
                definition = json.loads(policy[0]); definition['auto_trigger'] = False
                db.execute('UPDATE update_policies SET definition=? WHERE library_id=?', (json.dumps(definition), lid))
            db.execute('DELETE FROM library_ownership WHERE library_id=?', (lid,))
            if target_org:
                db.execute('INSERT INTO library_ownership VALUES (?,NULL,?,?)', (lid, target_org, before['native']))
            db.execute("INSERT INTO library_governance VALUES (?,'restricted',?,?) ON CONFLICT(library_id) "
                       "DO UPDATE SET organization_access='restricted',custody_native=excluded.custody_native,revision=excluded.revision",
                       (lid, before['native'], before['revision'] + 1))
            db.execute('INSERT INTO library_governance_events VALUES (NULL,?,?,?,?,?,?)',
                       (lid, actor_id, before['organization_id'], target_org, len(keys), now()))
            self.store.event(db, {'kind': 'admin', 'action': 'library_transfer', 'library_id': lid, 'status': 'ok'})
            return dict(self.state(db, lid), revoked_keys=len(keys))

    def sharing(self, lid, actor, value=None):
        from .collaboration import Collaboration
        with self.store.transaction(write=value is not None) as db:
            Collaboration(self.registry).check(db, actor, lid, 4)
            state = self.state(db, lid)
            if not state['organization_id']:
                raise KnowledgeError('organization_library_required', '仅组织库支持组织内共享', 409)
            if value:
                if value.expected_revision != state['revision']:
                    raise KnowledgeError('governance_changed', '归属或共享策略已变化，请刷新后重试', 409)
                db.execute('INSERT INTO library_governance VALUES (?,?,?,?) ON CONFLICT(library_id) '
                           'DO UPDATE SET organization_access=excluded.organization_access,revision=excluded.revision',
                           (lid, value.organization_access, state['native'], state['revision'] + 1))
                Collaboration(self.registry).event(db, actor, 'library_sharing_' + value.organization_access, lid)
            current = self.state(db, lid)
            return {k: current[k] for k in ('organization_access', 'revision')}


def add_governance_routes(app, registry):
    service = Governance(registry)

    @app.get('/admin/api/library-governance')
    def listing():
        with registry.store.transaction() as db:
            ids = [r[0] for r in db.execute('SELECT id FROM libraries ORDER BY name,id')]
            return [service.state(db, lid) for lid in ids]

    @app.get('/admin/api/libraries/{lid}/governance')
    def get(lid: str):
        return service.get(lid)

    @app.put('/admin/api/libraries/{lid}/governance')
    def transfer(lid: str, value: Transfer):
        return service.transfer(lid, value)

    @app.get('/portal/api/libraries/{lid}/sharing')
    def sharing(lid: str, request: Request):
        return service.sharing(lid, request.state.human)

    @app.put('/portal/api/libraries/{lid}/sharing')
    def save(lid: str, value: Sharing, request: Request):
        return service.sharing(lid, request.state.human, value)
