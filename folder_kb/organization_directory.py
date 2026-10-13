"""Organization-owned departments, groups and live library grants."""
import uuid
from typing import Literal

from fastapi import Request
from pydantic import Field

from .collaboration import Collaboration, Input, ROLES, fail

DIRECTORY_SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS organization_units (
       id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
       kind TEXT NOT NULL CHECK(kind IN ('department','group')), name TEXT NOT NULL,
       parent_id TEXT REFERENCES organization_units(id), revision INTEGER NOT NULL DEFAULT 1,
       UNIQUE(id,organization_id), CHECK(kind='department' OR parent_id IS NULL))''',
    '''CREATE INDEX IF NOT EXISTS units_by_org ON organization_units(organization_id,parent_id)''',
    '''CREATE TABLE IF NOT EXISTS unit_members (
       unit_id TEXT NOT NULL, organization_id TEXT NOT NULL, user_id TEXT NOT NULL,
       PRIMARY KEY(unit_id,user_id),
       FOREIGN KEY(unit_id,organization_id) REFERENCES organization_units(id,organization_id) ON DELETE CASCADE,
       FOREIGN KEY(organization_id,user_id) REFERENCES organization_members(organization_id,user_id) ON DELETE CASCADE)''',
    '''CREATE INDEX IF NOT EXISTS units_by_member ON unit_members(user_id,organization_id)''',
    '''CREATE TABLE IF NOT EXISTS library_unit_grants (
       library_id TEXT NOT NULL REFERENCES libraries(id),
       unit_id TEXT NOT NULL REFERENCES organization_units(id) ON DELETE CASCADE,
       role TEXT NOT NULL CHECK(role IN ('reader','editor','publisher','manager')),
       include_descendants INTEGER NOT NULL CHECK(include_descendants IN (0,1)),
       PRIMARY KEY(library_id,unit_id))''',
)


def inherited_role(db, uid, lid, oid):
    # Walk upwards from actual memberships, never across the organization boundary.
    rows = db.execute('''WITH RECURSIVE subjects(id,indirect) AS (
        SELECT unit_id,0 FROM unit_members WHERE organization_id=? AND user_id=?
        UNION
        SELECT u.parent_id,1 FROM organization_units u JOIN subjects s ON s.id=u.id
          WHERE u.organization_id=? AND u.parent_id IS NOT NULL
        ) SELECT g.role FROM library_unit_grants g JOIN subjects s ON s.id=g.unit_id
          JOIN organization_units u ON u.id=g.unit_id
          WHERE g.library_id=? AND u.organization_id=? AND (s.indirect=0 OR g.include_descendants=1)''',
        (oid, uid, oid, lid, oid))
    return max((ROLES[r[0]] for r in rows), default=0)


def remove_member(db, oid, uid):
    # Make any open membership editor stale before the organization FK cascades.
    db.execute('UPDATE organization_units SET revision=revision+1 WHERE id IN '
               '(SELECT unit_id FROM unit_members WHERE organization_id=? AND user_id=?)', (oid, uid))


class Unit(Input):
    name: str = Field(min_length=1, max_length=80, pattern=r'\S')
    parent_id: str | None = Field(default=None, min_length=1, max_length=64)
    member_ids: list[str] = Field(default_factory=list, max_length=1000)


class NewUnit(Unit):
    kind: Literal['department', 'group']


class EditUnit(Unit):
    expected_revision: int = Field(ge=1)


class UnitGrant(Input):
    role: Literal['reader', 'editor', 'publisher', 'manager', 'remove']
    include_descendants: bool = False
    expected_revision: int = Field(ge=0)


class MemberProfile(Input):
    role: Literal['member', 'admin']
    unit_ids: list[str] = Field(default_factory=list, max_length=2000)
    expected_role: Literal['member', 'admin']
    expected_unit_ids: list[str] = Field(default_factory=list, max_length=2000)


class Directory:
    def __init__(self, registry):
        self.collab = Collaboration(registry)
        self.store = registry.store

    def units(self, db, oid):
        return [dict(r) for r in db.execute('SELECT * FROM organization_units WHERE organization_id=? ORDER BY name,id', (oid,))]

    def list(self, actor, oid):
        with self.store.transaction() as db:
            self.collab.org_check(db, actor, oid)
            units = self.units(db, oid)
            members = {}
            for r in db.execute('SELECT unit_id,user_id FROM unit_members WHERE organization_id=?', (oid,)):
                members.setdefault(r[0], []).append(r[1])
            return [dict(u, member_ids=members.get(u['id'], [])) for u in units]

    def member(self, actor, oid, uid, value=None):
        with self.store.transaction(write=value is not None) as db:
            self.collab.org_check(db, actor, oid)
            row = db.execute('SELECT u.id,u.username,u.status,m.role FROM organization_members m '
                             'JOIN users u ON u.id=m.user_id WHERE m.organization_id=? AND m.user_id=?',
                             (oid, uid)).fetchone()
            if not row:
                fail('unknown_member', '成员已移出组织，请刷新列表', 404)
            current = {r[0] for r in db.execute('SELECT unit_id FROM unit_members WHERE organization_id=? AND user_id=?', (oid, uid))}
            if value:
                if row['role'] != value.expected_role or current != set(value.expected_unit_ids):
                    fail('member_changed', '成员资料已变化，请重新加载后修改', 409)
                wanted = set(value.unit_ids)
                available = {u['id'] for u in self.units(db, oid)}
                if not wanted <= available:
                    fail('invalid_unit', '只能选择本组织现有的部门或用户组', 400)
                for unit_id in wanted - current:
                    if db.execute('SELECT count(*) FROM unit_members WHERE unit_id=?', (unit_id,)).fetchone()[0] >= 1000:
                        fail('unit_member_limit', '每个部门或用户组最多 1000 名成员', 409)
                if row['status'] != 'active':
                    fail('inactive_member', '账号尚未启用，不能调整成员资料', 409)
                if row['role'] == 'admin' and value.role != 'admin' and db.execute(
                        "SELECT count(*) FROM organization_members m JOIN users u ON u.id=m.user_id "
                        "WHERE organization_id=? AND role='admin' AND u.status='active'", (oid,)).fetchone()[0] <= 1:
                    fail('last_admin', '请先设置另一位组织管理员', 409)
                # Only touch this user's memberships; other members' edits remain intact.
                for unit_id in current - wanted:
                    db.execute('DELETE FROM unit_members WHERE unit_id=? AND user_id=?', (unit_id, uid))
                for unit_id in wanted - current:
                    db.execute('INSERT INTO unit_members VALUES (?,?,?)', (unit_id, oid, uid))
                for unit_id in current ^ wanted:
                    db.execute('UPDATE organization_units SET revision=revision+1 WHERE id=?', (unit_id,))
                db.execute('UPDATE organization_members SET role=? WHERE organization_id=? AND user_id=?', (value.role, oid, uid))
                self.collab.event(db, actor, 'organization_member_profile', oid + ':' + uid)
                return dict(row, role=value.role, unit_ids=sorted(wanted))
            return dict(row, unit_ids=sorted(current))

    def save(self, actor, oid, value, unit_id=None):
        with self.store.transaction(write=True) as db:
            self.collab.org_check(db, actor, oid)
            units = {u['id']: u for u in self.units(db, oid)}
            old = units.get(unit_id)
            if unit_id and not old:
                fail('unknown_unit', '部门或用户组不存在', 404)
            if old and old['revision'] != value.expected_revision:
                fail('unit_changed', '部门或用户组已变化，请重新打开设置', 409)
            if not old and len(units) >= 2000:
                fail('directory_limit', '每个组织最多 2000 个部门和用户组', 409)
            kind = old['kind'] if old else value.kind
            if value.parent_id and (kind != 'department' or value.parent_id not in units or units[value.parent_id]['kind'] != 'department'):
                fail('invalid_parent', '上级必须为本组织的部门', 400)
            uid = unit_id or uuid.uuid4().hex
            name = value.name.strip()
            if any(u['id'] != uid and u['kind'] == kind and u['parent_id'] == value.parent_id and u['name'].casefold() == name.casefold() for u in units.values()):
                fail('duplicate_unit', '同级部门或用户组名称重复', 409)
            units[uid] = dict(id=uid, name=name, kind=kind, parent_id=value.parent_id)
            # Validate the whole resulting tree, including descendants of a moved node.
            for start in units:
                visited, cursor = set(), start
                while cursor:
                    if cursor in visited:
                        fail('department_cycle', '部门不能移动到自身或其下级', 409)
                    visited.add(cursor)
                    if len(visited) > 16:
                        fail('department_depth', '部门层级最多 16 层', 409)
                    cursor = units[cursor]['parent_id']
            members = set(value.member_ids)
            available = {r[0] for r in db.execute('SELECT user_id FROM organization_members WHERE organization_id=?', (oid,))}
            if not members <= available:
                fail('invalid_member', '只能选择本组织成员', 400)
            if old:
                db.execute('UPDATE organization_units SET name=?,parent_id=?,revision=revision+1 WHERE id=?', (name, value.parent_id, uid))
                db.execute('DELETE FROM unit_members WHERE unit_id=?', (uid,))
            else:
                db.execute('INSERT INTO organization_units VALUES (?,?,?,?,?,1)', (uid, oid, kind, name, value.parent_id))
            db.executemany('INSERT INTO unit_members VALUES (?,?,?)', [(uid, oid, member) for member in members])
            self.collab.event(db, actor, 'unit_update' if old else 'unit_create', uid)
            return {'id': uid, 'revision': old['revision'] + 1 if old else 1}

    def delete(self, actor, oid, uid, revision):
        with self.store.transaction(write=True) as db:
            self.collab.org_check(db, actor, oid)
            row = db.execute('SELECT * FROM organization_units WHERE id=? AND organization_id=?', (uid, oid)).fetchone()
            if not row:
                fail('unknown_unit', '部门或用户组不存在', 404)
            if row['revision'] != revision:
                fail('unit_changed', '部门或用户组已变化，请刷新后重试', 409)
            if db.execute('SELECT 1 FROM organization_units WHERE parent_id=?', (uid,)).fetchone():
                fail('department_not_empty', '请先移动或删除下级部门', 409)
            db.execute('DELETE FROM organization_units WHERE id=?', (uid,))
            self.collab.event(db, actor, 'unit_delete', uid)
            return {'ok': True}

    def grants(self, actor, lid, uid=None, value=None):
        from .governance import metadata
        with self.store.transaction(write=value is not None) as db:
            self.collab.check_configuration(db, actor, lid)
            scope = db.execute('SELECT organization_id FROM library_ownership WHERE library_id=?', (lid,)).fetchone()
            if not scope or not scope[0]:
                fail('organization_library_required', '仅组织库支持部门和用户组授权', 409)
            units = self.units(db, scope[0])
            meta = metadata(db, lid)
            if value:
                if meta['revision'] != value.expected_revision:
                    fail('governance_changed', '授权或归属已变化，请刷新后重试', 409)
                unit = next((u for u in units if u['id'] == uid), None)
                if not unit:
                    fail('invalid_subject', '只能授权本组织的部门或用户组', 400)
                if unit['kind'] == 'group' and value.include_descendants:
                    fail('invalid_scope', '用户组不包含下级部门', 400)
                if value.role == 'remove':
                    db.execute('DELETE FROM library_unit_grants WHERE library_id=? AND unit_id=?', (lid, uid))
                else:
                    db.execute('INSERT INTO library_unit_grants VALUES (?,?,?,?) ON CONFLICT(library_id,unit_id) '
                               'DO UPDATE SET role=excluded.role,include_descendants=excluded.include_descendants',
                               (lid, uid, value.role, value.include_descendants))
                db.execute('INSERT INTO library_governance VALUES (?,?,?,?) ON CONFLICT(library_id) '
                           'DO UPDATE SET revision=excluded.revision',
                           (lid, meta['organization_access'], meta['custody_native'], meta['revision'] + 1))
                self.collab.event(db, actor, 'library_unit_' + value.role, lid + ':' + uid)
            grants = [dict(r) for r in db.execute('SELECT * FROM library_unit_grants WHERE library_id=?', (lid,))]
            return {'units': units, 'grants': grants, 'revision': metadata(db, lid)['revision']}


def add_directory_routes(app, registry):
    service = Directory(registry)

    @app.get('/portal/api/organizations/{oid}/members/{uid}/profile')
    def member_profile(oid: str, uid: str, request: Request):
        return service.member(request.state.human, oid, uid)

    @app.put('/portal/api/organizations/{oid}/members/{uid}/profile')
    def edit_member_profile(oid: str, uid: str, value: MemberProfile, request: Request):
        return service.member(request.state.human, oid, uid, value)

    @app.get('/portal/api/organizations/{oid}/units')
    def units(oid: str, request: Request):
        return service.list(request.state.human, oid)

    @app.post('/portal/api/organizations/{oid}/units', status_code=201)
    def create(oid: str, value: NewUnit, request: Request):
        return service.save(request.state.human, oid, value)

    @app.put('/portal/api/organizations/{oid}/units/{uid}')
    def edit(oid: str, uid: str, value: EditUnit, request: Request):
        return service.save(request.state.human, oid, value, uid)

    @app.delete('/portal/api/organizations/{oid}/units/{uid}')
    def delete(oid: str, uid: str, expected_revision: int, request: Request):
        return service.delete(request.state.human, oid, uid, expected_revision)

    @app.get('/portal/api/libraries/{lid}/unit-grants')
    def grants(lid: str, request: Request):
        return service.grants(request.state.human, lid)

    @app.put('/portal/api/libraries/{lid}/unit-grants/{uid}')
    def grant(lid: str, uid: str, value: UnitGrant, request: Request):
        return service.grants(request.state.human, lid, uid, value)
