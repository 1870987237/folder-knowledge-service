"""Human identities, scoped knowledge access and reviewed document publication."""
from __future__ import annotations

import difflib
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Literal

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field

from .core import KnowledgeError, KnowledgeFolder, is_link
from .storage import now


COLLAB_SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
       password_hash TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS user_sessions (digest TEXT PRIMARY KEY, user_id TEXT NOT NULL
       REFERENCES users(id), expires REAL NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS login_limits (bucket TEXT PRIMARY KEY, attempts INTEGER NOT NULL, expires REAL NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS organizations (id TEXT PRIMARY KEY, name TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS organization_members (organization_id TEXT REFERENCES organizations(id),
       user_id TEXT REFERENCES users(id), role TEXT NOT NULL, PRIMARY KEY(organization_id,user_id))''',
    '''CREATE TABLE IF NOT EXISTS library_ownership (library_id TEXT PRIMARY KEY REFERENCES libraries(id),
       owner_id TEXT REFERENCES users(id), organization_id TEXT REFERENCES organizations(id), native INTEGER NOT NULL DEFAULT 0,
       CHECK ((owner_id IS NULL) != (organization_id IS NULL)))''',
    '''CREATE TABLE IF NOT EXISTS library_members (library_id TEXT REFERENCES libraries(id),
       user_id TEXT REFERENCES users(id), role TEXT NOT NULL, PRIMARY KEY(library_id,user_id))''',
    '''CREATE TABLE IF NOT EXISTS key_owners (key_id TEXT PRIMARY KEY REFERENCES agent_keys(id), user_id TEXT NOT NULL REFERENCES users(id))''',
    '''CREATE TABLE IF NOT EXISTS document_drafts (id TEXT PRIMARY KEY, library_id TEXT NOT NULL REFERENCES libraries(id),
       author_id TEXT NOT NULL REFERENCES users(id), path TEXT NOT NULL, base_root TEXT NOT NULL, base_version TEXT NOT NULL,
       original TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL,
       reviewer_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, published_root TEXT)''',
    '''CREATE TABLE IF NOT EXISTS collaboration_events (id INTEGER PRIMARY KEY, actor_id TEXT NOT NULL,
       action TEXT NOT NULL, resource TEXT NOT NULL, created_at TEXT NOT NULL)''',
)
ROLES = {'reader': 1, 'editor': 2, 'publisher': 3, 'manager': 4}
TEXT_BYTES = 256 * 1024


def fail(code, message, status=403):
    raise KnowledgeError(code, message, status)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def snapshot(root):
    """Only native document trees; bounded reads verify opened handles and hard links."""
    folder = KnowledgeFolder(root)
    files, directories, total, count = {}, [], 0, 0
    for current, dirs, names in os.walk(root, followlinks=False):
        for name in dirs + names:
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            count += 1
            if count > 2000:
                fail('workspace_too_large', '文档库最多支持 2000 个文件和目录', 413)
            folder._path(relative)
            if path.is_dir():
                directories.append(relative)
            else:
                if path.suffix.lower() not in ('.md', '.txt'):
                    fail('native_content_unsupported', '普通文档库仅支持 Markdown / txt；其他内容请使用模板库', 409)
                with folder._open_text(relative) as (stream, st, _):
                    if total + st.st_size > 32 * 1024 * 1024:
                        fail('workspace_too_large', '文档库快照最大 32 MiB', 413)
                    raw = stream.read(32 * 1024 * 1024 - total + 1)
                    total += len(raw)
                    if total > 32 * 1024 * 1024:
                        fail('workspace_too_large', '文档库过大', 413)
                    files[relative] = raw
    version = digest(json.dumps([sorted(directories), sorted((k, hashlib.sha256(v).hexdigest()) for k, v in files.items())]))
    return version, files, directories


def workspace_digest(root):
    return snapshot(root)[0]


def copy_workspace(source, target):
    _, files, directories = snapshot(source)
    target.mkdir(exist_ok=False)
    for directory in directories:
        (target / directory).mkdir(parents=True, exist_ok=True)
    for path, raw in files.items():
        (target / path).write_bytes(raw)


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    return salt + ':' + hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


def user_role(db, user_id, library_id):
    if not db.execute("SELECT 1 FROM users WHERE id=? AND status='active'", (user_id,)).fetchone():
        return 0
    scope = db.execute('SELECT * FROM library_ownership WHERE library_id=?', (library_id,)).fetchone()
    if not scope:
        return 0
    if scope['owner_id']:
        if scope['owner_id'] == user_id:
            return 4
        # Personal libraries are private in v1; sharing requires an explicit future policy.
        return 0
    member = db.execute('SELECT role FROM organization_members WHERE organization_id=? AND user_id=?',
                        (scope['organization_id'], user_id)).fetchone()
    if not member:
        return 0
    if member['role'] == 'admin':
        return 4
    grant = db.execute('SELECT role FROM library_members WHERE library_id=? AND user_id=?', (library_id, user_id)).fetchone()
    from .governance import metadata
    default = 1 if metadata(db, library_id)['organization_access'] == 'org_read' else 0
    return max(ROLES.get(grant[0], 0) if grant else 0, default)


def owned_key_allowed(db, key_id, library_id, submit=False):
    owner = db.execute('SELECT user_id FROM key_owners WHERE key_id=?', (key_id,)).fetchone()
    return not owner or user_role(db, owner[0], library_id) >= (2 if submit else 1)


class Collaboration:
    def __init__(self, registry):
        self.registry, self.store = registry, registry.store

    def event(self, db, actor, action, resource):
        db.execute('INSERT INTO collaboration_events(actor_id,action,resource,created_at) VALUES (?,?,?,?)',
                   (actor['id'], action, resource, now()))

    def register(self, username, password, address):
        username = username.strip().lower()
        if not username or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789._-@' for c in username):
            fail('invalid_username', '账号使用英文字母、数字、点、横线、下划线或 @', 400)
        self.rate_limit('register:' + address, 10)
        hashed = password_hash(password)
        with self.store.transaction(write=True) as db:
            if db.execute('SELECT 1 FROM users WHERE username=?', (username,)).fetchone():
                fail('account_exists', '账号已存在', 409)
            uid = uuid.uuid4().hex
            db.execute('INSERT INTO users VALUES (?,?,?,?,?)', (uid, username, hashed, 'pending', now()))
            self.event(db, {'id': uid}, 'register', uid)
        return {'status': 'pending', 'message': '注册成功，等待平台管理员启用'}

    def rate_limit(self, bucket, limit=20):
        stamp = time.time()
        with self.store.transaction(write=True) as db:
            db.execute('DELETE FROM login_limits WHERE expires<?', (stamp,))
            row = db.execute('SELECT attempts FROM login_limits WHERE bucket=?', (bucket,)).fetchone()
            if row and row[0] >= limit:
                fail('rate_limited', '操作过于频繁，请一分钟后重试', 429)
            db.execute('INSERT INTO login_limits VALUES (?,1,?) ON CONFLICT(bucket) DO UPDATE SET attempts=attempts+1',
                       (bucket, stamp + 60))

    def login(self, username, password, address):
        self.rate_limit('login:' + address)
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM users WHERE username=?', (username.strip().lower(),)).fetchone()
        stored = row['password_hash'] if row else '00' * 16 + ':' + '00' * 64
        valid = hmac.compare_digest(password_hash(password, stored.split(':')[0]), stored)
        if not row or not valid or row['status'] != 'active':
            fail('login_failed', '账号、密码无效或账号尚未启用', 401)
        token = 'fku_' + secrets.token_urlsafe(32)
        with self.store.transaction(write=True) as db:
            # Recheck status after the slow password derivation.
            if db.execute('SELECT status FROM users WHERE id=?', (row['id'],)).fetchone()[0] != 'active':
                fail('login_failed', '账号不可用', 401)
            db.execute('DELETE FROM user_sessions WHERE expires<?', (time.time(),))
            db.execute('INSERT INTO user_sessions VALUES (?,?,?)', (digest(token), row['id'], time.time() + 8 * 3600))
        return {'token': token}

    def authenticate(self, token, db=None):
        if db is None:
            with self.store.transaction() as conn:
                return self.authenticate(token, conn)
        row = db.execute('SELECT u.id,u.username FROM user_sessions s JOIN users u ON u.id=s.user_id '
                         "WHERE s.digest=? AND s.expires>? AND u.status='active'", (digest(token), time.time())).fetchone()
        if not row:
            fail('session_expired', '会话已过期，请重新登录', 401)
        return dict(row)

    def check(self, db, actor, library_id, level=1):
        row = db.execute('SELECT * FROM libraries WHERE id=? AND enabled=1', (library_id,)).fetchone()
        role = 4 if actor.get('platform') else user_role(db, actor['id'], library_id)
        if not row or role < level:
            fail('library_forbidden', '没有该知识库的操作权限')
        return dict(row), role

    def org_check(self, db, actor, oid):
        if not db.execute('SELECT 1 FROM organizations WHERE id=?', (oid,)).fetchone():
            fail('organization_forbidden', '组织不存在或不可管理')
        if not actor.get('platform') and not db.execute(
                "SELECT 1 FROM organization_members WHERE organization_id=? AND user_id=? AND role='admin'", (oid, actor['id'])).fetchone():
            fail('organization_forbidden', '无组织管理权限')

    def me(self, actor):
        with self.store.transaction() as db:
            if actor.get('platform'):
                orgs = [dict(r, role='admin') for r in db.execute('SELECT * FROM organizations')]
            else:
                orgs = [dict(r) for r in db.execute('SELECT o.*,m.role FROM organizations o JOIN organization_members m '
                                                  'ON m.organization_id=o.id WHERE m.user_id=?', (actor['id'],))]
        return dict(actor, organizations=orgs)

    def libraries(self, actor):
        with self.store.transaction() as db:
            rows = db.execute('SELECT l.*,s.owner_id,s.organization_id,s.native FROM libraries l '
                              'LEFT JOIN library_ownership s ON s.library_id=l.id WHERE l.enabled=1').fetchall()
            result = []
            for row in rows:
                role = 4 if actor.get('platform') else user_role(db, actor['id'], row['id'])
                if role:
                    editable = self.editable(db, row['id'], row['path'])
                    result.append({'id': row['id'], 'name': row['name'], 'owner_id': row['owner_id'],
                                   'organization_id': row['organization_id'], 'role': role, 'editable': editable, 'updated_at': row['updated_at']})
        return result

    def editable(self, db, lid, root):
        scope = db.execute('SELECT native FROM library_ownership WHERE library_id=?', (lid,)).fetchone()
        if not scope or not scope[0] or not root.startswith('document-releases/'):
            return False
        binding = db.execute('SELECT t.metadata_json FROM library_templates b JOIN template_versions t '
                             'ON t.id=b.template_id AND t.version=b.template_version WHERE b.library_id=?', (lid,)).fetchone()
        if binding:
            definition = json.loads(binding['metadata_json'])
            if definition.get('capabilities') or not definition.get('plugin'):
                return False
        if db.execute('SELECT 1 FROM repository_libraries WHERE library_id=?', (lid,)).fetchone():
            return False
        # A native library must remain an independent publication unit.
        for other in db.execute('SELECT id,path FROM libraries WHERE id<>?', (lid,)):
            if root == other['path'] or root.startswith(other['path'].rstrip('/') + '/') or other['path'].startswith(root + '/') or other['path'] == '.':
                return False
        return True

    def create_library(self, actor, name, oid):
        lid = uuid.uuid4().hex
        with self.store.transaction(write=True) as db:
            if oid:
                self.org_check(db, actor, oid)
            elif actor.get('platform'):
                fail('owner_required', '平台管理员请为组织创建库，个人库由用户创建', 400)
            root = self.registry.pool.root / 'document-releases'
            try:
                root.mkdir(exist_ok=True)
            except OSError:
                fail('document_storage_unavailable', '请为 document-releases 配置专用可写挂载', 503)
            if is_link(root.lstat()):
                fail('unsafe_path', '发布目录不安全')
            folder = root / lid
            try:
                folder.mkdir()
                (folder / 'README.md').write_text('# ' + name.strip() + '\n', encoding='utf-8')
            except OSError:
                fail('document_storage_unavailable', '普通文档存储不可写，请检查专用挂载', 503)
            stamp = now()
            db.execute('INSERT INTO libraries VALUES (?,?,?,?,?,?)', (lid, name.strip(), 'document-releases/' + lid, 1, stamp, stamp))
            db.execute('INSERT INTO library_ownership VALUES (?,?,?,1)', (lid, None if oid else actor['id'], oid))
            self.event(db, actor, 'library_create', lid)
        return {'id': lid}

    @staticmethod
    def text(folder, path, missing=False):
        pure = PurePosixPath(path)
        if not path or pure.as_posix() != path or pure.suffix.lower() not in ('.md', '.txt'):
            fail('unsupported_document', '编辑仅支持 Markdown / txt 文档', 400)
        try:
            with folder._open_text(path) as (stream, st, _):
                if st.st_size > TEXT_BYTES:
                    fail('document_too_large', '编辑上限为 256 KiB，请拆分文档', 413)
                value = stream.read(TEXT_BYTES + 1)
                if len(value) > TEXT_BYTES:
                    fail('document_too_large', '文档过大', 413)
                return value.decode('utf-8-sig').replace('\r\n', '\n')
        except UnicodeError:
            fail('invalid_encoding', '文档必须为 UTF-8', 415)
        except KnowledgeError as exc:
            if exc.code == 'not_found' and missing:
                # Parent must already exist and obey the same path restrictions.
                folder._path(str(pure.parent))
                if any(p.startswith('.') or ':' in p or '\\' in p for p in pure.parts) or pure.is_absolute() or '..' in pure.parts:
                    fail('invalid_path', '无效文档路径', 400)
                return ''
            raise

    def read(self, actor, lid, path):
        with self.store.transaction() as db:
            row, _ = self.check(db, actor, lid)
            folder = self.registry.checked_folder(row)
            text = self.text(folder, path)
            return {'path': path, 'content': text, 'version': digest(text)}

    def create_draft(self, actor, lid, path, content, expected_version):
        if len(content.encode()) > TEXT_BYTES:
            fail('document_too_large', '文档过大', 413)
        with self.store.transaction(write=True) as db:
            row, _ = self.check(db, actor, lid, 2)
            if actor.get('platform') or not self.editable(db, lid, row['path']):
                fail('editing_unsupported', '请通过用户账号编辑平台文档库；挂载库使用其模板或 Git 更新流程', 409)
            folder = self.registry.checked_folder(row)
            base = workspace_digest(folder.root)
            original = self.text(folder, path, missing=True)
            if digest(original) != expected_version:
                fail('document_changed', '文档已更新，请重新读取后编辑', 409)
            if workspace_digest(folder.root) != base:
                fail('document_changed', '知识库正在变化，请重试', 409)
            did, stamp = uuid.uuid4().hex, now()
            db.execute('INSERT INTO document_drafts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (did, lid, actor['id'], path, row['path'], base, original, content, 'draft', 1, None, stamp, stamp, None))
            self.event(db, actor, 'draft_create', did)
        return {'id': did}

    def drafts(self, actor, lid):
        with self.store.transaction() as db:
            _, role = self.check(db, actor, lid)
            rows = db.execute('SELECT d.id,d.path,d.author_id,u.username AS author,d.status,d.revision,d.created_at,d.updated_at '
                              'FROM document_drafts d JOIN users u ON u.id=d.author_id WHERE library_id=? ORDER BY d.created_at DESC,d.id DESC LIMIT 200', (lid,))
            return [dict(r) for r in rows if role >= 3 or r['author_id'] == actor['id'] or r['status'] == 'published']

    def draft(self, actor, did):
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM document_drafts WHERE id=?', (did,)).fetchone()
            if not row:
                fail('draft_unavailable', '草稿不可用', 404)
            _, role = self.check(db, actor, row['library_id'])
            if role < 3 and row['author_id'] != actor['id'] and row['status'] != 'published':
                fail('draft_forbidden', '不能查看他人草稿')
            result = {k: row[k] for k in ('id', 'path', 'content', 'original', 'status', 'revision', 'author_id', 'library_id')}
            result['diff'] = ''.join(difflib.unified_diff(row['original'].splitlines(True), row['content'].splitlines(True), fromfile='发布前', tofile='候选'))
            return result

    def change_draft(self, actor, did, action, revision, content=None):
        with self.store.transaction(write=True) as db:
            raw = db.execute('SELECT * FROM document_drafts WHERE id=?', (did,)).fetchone()
            if not raw:
                fail('draft_unavailable', '草稿不可用', 404)
            d = dict(raw)
            row, _ = self.check(db, actor, d['library_id'], 3 if action in ('publish', 'reject') else 2)
            if d['revision'] != revision:
                fail('draft_changed', '草稿状态已变化，请刷新', 409)
            if action in ('save', 'submit') and d['author_id'] != actor['id']:
                fail('draft_forbidden', '只有作者可以修改或送审草稿')
            required = 'submitted' if action in ('publish', 'reject') else 'draft'
            if d['status'] != required:
                fail('draft_state', '当前状态不支持此操作', 409)
            status = {'save': 'draft', 'submit': 'submitted', 'reject': 'rejected', 'publish': 'published'}[action]
            target = None
            if action == 'save':
                if content is None or len(content.encode()) > TEXT_BYTES:
                    fail('document_too_large', '请输入有效文档内容，最大 256 KiB', 413)
                db.execute('UPDATE document_drafts SET content=? WHERE id=?', (content, did))
            if action in ('submit', 'publish'):
                if not self.editable(db, row['id'], row['path']) or user_role(db, d['author_id'], row['id']) < 2:
                    fail('editing_unsupported', '编辑能力或作者权限已变化', 409)
                source = self.registry.checked_folder(row)
                if row['path'] != d['base_root'] or workspace_digest(source.root) != d['base_version']:
                    fail('document_changed', '已有更新发布或文件发生变化，请重新整理草稿', 409)
            if action == 'publish':
                release = self.registry.pool.root / 'document-releases'
                if is_link(release.lstat()):
                    fail('unsafe_path', '发布目录不安全')
                target = release / uuid.uuid4().hex
                try:
                    copy_workspace(source.root, target)
                    if workspace_digest(target) != d['base_version']:
                        fail('document_changed', '复制期间原文档发生变化', 409)
                    output = target / d['path']
                    output.write_text(d['content'], encoding='utf-8', newline='\n')
                except OSError:
                    fail('document_storage_unavailable', '普通文档发布目录不可写', 503)
                if workspace_digest(source.root) != d['base_version']:
                    fail('document_changed', '发布期间原文件变化，请重试', 409)
                # DB readers see either the old complete tree or the new one.
                relative = target.relative_to(self.registry.pool.root).as_posix()
                db.execute('UPDATE libraries SET path=?,updated_at=? WHERE id=?', (relative, now(), row['id']))
                target = relative
            db.execute('UPDATE document_drafts SET status=?,revision=revision+1,updated_at=?,reviewer_id=?,published_root=? WHERE id=?',
                       (status, now(), actor['id'] if action in ('publish', 'reject') else None, target, did))
            self.event(db, actor, 'draft_' + action, did)
        return {'id': did, 'status': status}


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Credentials(Input):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=12, max_length=256)


class AccountState(Input):
    status: Literal['active', 'disabled']


class Name(Input):
    name: str = Field(min_length=1, max_length=80, pattern=r'\S')


class Membership(Input):
    username: str = Field(min_length=1, max_length=80)
    role: Literal['admin', 'member', 'remove']


class Scope(Input):
    organization_id: str


class NewLibrary(Name):
    organization_id: str | None = None


class Grant(Input):
    user_id: str
    role: Literal['reader', 'editor', 'publisher', 'manager', 'remove']


class NewDraft(Input):
    path: str = Field(min_length=1, max_length=1024)
    content: str = Field(max_length=TEXT_BYTES)
    expected_version: str = Field(pattern=r'^[0-9a-f]{64}$')


class DraftAction(Input):
    action: Literal['save', 'submit', 'publish', 'reject']
    revision: int = Field(ge=1)
    content: str | None = Field(default=None, max_length=TEXT_BYTES)


class UserKey(Input):
    name: str = Field(min_length=1, max_length=80)
    library_ids: list[str] = Field(min_length=1, max_length=64)
    submit_library_ids: list[str] = Field(default_factory=list, max_length=64)


def add_collaboration_routes(app, registry):
    service = Collaboration(registry)
    app.state.collaboration = service
    def actor(request):
        return request.state.human

    @app.post('/portal/api/register', status_code=201)
    def register(value: Credentials, request: Request):
        return service.register(value.username, value.password, request.client.host)

    @app.post('/portal/api/login')
    def login(value: Credentials, request: Request):
        return service.login(value.username, value.password, request.client.host)

    @app.post('/portal/api/logout')
    def logout(request: Request):
        with service.store.transaction(write=True) as db:
            db.execute('DELETE FROM user_sessions WHERE digest=?', (digest(request.headers.get('authorization', '')[7:]),))
        return {'ok': True}

    @app.get('/portal/api/me')
    def me(request: Request):
        return service.me(actor(request))

    @app.get('/admin/api/accounts')
    def accounts():
        with service.store.transaction() as db:
            return [dict(r) for r in db.execute('SELECT id,username,status,created_at FROM users ORDER BY created_at DESC')]

    @app.put('/admin/api/accounts/{uid}')
    def account(uid: str, value: AccountState):
        with service.store.transaction(write=True) as db:
            if not db.execute('SELECT 1 FROM users WHERE id=?', (uid,)).fetchone():
                fail('unknown_user', '账号不存在', 404)
            db.execute('UPDATE users SET status=? WHERE id=?', (value.status, uid))
            if value.status == 'disabled':
                db.execute('DELETE FROM user_sessions WHERE user_id=?', (uid,))
            service.event(db, {'id': 'platform'}, 'account_' + value.status, uid)
        return {'ok': True}

    @app.post('/portal/api/organizations', status_code=201)
    def create_org(value: Name, request: Request):
        a = actor(request)
        if not a.get('platform'):
            fail('admin_required', '仅平台管理员可以创建组织')
        oid = uuid.uuid4().hex
        with service.store.transaction(write=True) as db:
            db.execute('INSERT INTO organizations VALUES (?,?)', (oid, value.name.strip()))
            service.event(db, a, 'organization_create', oid)
        return {'id': oid}

    @app.get('/portal/api/organizations/{oid}/members')
    def members(oid: str, request: Request):
        with service.store.transaction() as db:
            service.org_check(db, actor(request), oid)
            return [dict(r) for r in db.execute('SELECT u.id,u.username,m.role FROM organization_members m JOIN users u ON u.id=m.user_id WHERE organization_id=?', (oid,))]

    @app.put('/portal/api/organizations/{oid}/members')
    def member(oid: str, value: Membership, request: Request):
        a = actor(request)
        with service.store.transaction(write=True) as db:
            service.org_check(db, a, oid)
            user = db.execute("SELECT id FROM users WHERE username=? AND status='active'", (value.username.strip().lower(),)).fetchone()
            if not user:
                fail('unknown_user', '请输入已启用的账号', 404)
            current = db.execute('SELECT role FROM organization_members WHERE organization_id=? AND user_id=?', (oid, user[0])).fetchone()
            if current and current[0] == 'admin' and value.role != 'admin' and db.execute(
                    "SELECT count(*) FROM organization_members m JOIN users u ON u.id=m.user_id WHERE organization_id=? AND role='admin' AND u.status='active'", (oid,)).fetchone()[0] <= 1:
                fail('last_admin', '请先设置另一位组织管理员', 409)
            if value.role == 'remove':
                db.execute('DELETE FROM organization_members WHERE organization_id=? AND user_id=?', (oid, user[0]))
                db.execute('DELETE FROM library_members WHERE user_id=? AND library_id IN (SELECT library_id FROM library_ownership WHERE organization_id=?)', (user[0], oid))
            else:
                db.execute('INSERT INTO organization_members VALUES (?,?,?) ON CONFLICT(organization_id,user_id) DO UPDATE SET role=excluded.role', (oid, user[0], value.role))
            service.event(db, a, 'organization_member_' + value.role, oid + ':' + user[0])
        return {'ok': True}

    @app.put('/admin/api/libraries/{lid}/ownership')
    def ownership(lid: str, value: Scope):
        from .governance import Governance, Transfer
        governance = Governance(registry)
        state = governance.get(lid)
        governance.transfer(lid, Transfer(organization_id=value.organization_id, expected_revision=state['revision']), initial_only=True)
        return {'ok': True}

    @app.get('/portal/api/libraries')
    def libraries(request: Request):
        return service.libraries(actor(request))

    @app.post('/portal/api/libraries', status_code=201)
    def create_library(value: NewLibrary, request: Request):
        return service.create_library(actor(request), value.name, value.organization_id)

    @app.get('/portal/api/libraries/{lid}/members')
    def grants(lid: str, request: Request):
        with service.store.transaction() as db:
            service.check(db, actor(request), lid, 4)
            return [dict(r) for r in db.execute('SELECT u.id,u.username,g.role FROM library_members g JOIN users u ON u.id=g.user_id WHERE library_id=?', (lid,))]

    @app.put('/portal/api/libraries/{lid}/members')
    def grant(lid: str, value: Grant, request: Request):
        with service.store.transaction(write=True) as db:
            a = actor(request)
            service.check(db, a, lid, 4)
            scope = db.execute('SELECT organization_id FROM library_ownership WHERE library_id=?', (lid,)).fetchone()
            if not scope or not scope[0] or not db.execute('SELECT 1 FROM organization_members WHERE organization_id=? AND user_id=?', (scope[0], value.user_id)).fetchone():
                fail('invalid_member', '仅能授权同组织成员；个人库暂不共享', 400)
            if value.role == 'remove':
                db.execute('DELETE FROM library_members WHERE library_id=? AND user_id=?', (lid, value.user_id))
            else:
                db.execute('INSERT INTO library_members VALUES (?,?,?) ON CONFLICT(library_id,user_id) DO UPDATE SET role=excluded.role', (lid, value.user_id, value.role))
            service.event(db, a, 'library_member_' + value.role, lid + ':' + value.user_id)
        return {'ok': True}

    @app.get('/portal/api/libraries/{lid}/candidates')
    def candidates(lid: str, request: Request):
        with service.store.transaction() as db:
            service.check(db, actor(request), lid, 4)
            return [dict(r) for r in db.execute('SELECT u.id,u.username FROM users u JOIN organization_members m ON u.id=m.user_id '
                "JOIN library_ownership s ON s.organization_id=m.organization_id WHERE s.library_id=? AND u.status='active'", (lid,))]

    @app.get('/portal/api/libraries/{lid}/browse')
    def browse(lid: str, request: Request, path: str = '.', offset: int = 0):
        with service.store.transaction() as db:
            row, _ = service.check(db, actor(request), lid)
            return registry.access_library(row, 'list_directory', dict(path=path, offset=offset))

    @app.get('/portal/api/libraries/{lid}/search')
    def search(lid: str, request: Request, query: str, cursor: str | None = None):
        with service.store.transaction() as db:
            row, _ = service.check(db, actor(request), lid)
            return registry.access_library(row, 'grep', dict(query=query, cursor=cursor))

    @app.get('/portal/api/libraries/{lid}/document')
    def document(lid: str, request: Request, path: str):
        return service.read(actor(request), lid, path)

    @app.get('/portal/api/libraries/{lid}/drafts')
    def drafts(lid: str, request: Request):
        return service.drafts(actor(request), lid)

    @app.post('/portal/api/libraries/{lid}/drafts', status_code=201)
    def create_draft(lid: str, value: NewDraft, request: Request):
        return service.create_draft(actor(request), lid, **value.model_dump())

    @app.get('/portal/api/drafts/{did}')
    def draft(did: str, request: Request):
        return service.draft(actor(request), did)

    @app.post('/portal/api/drafts/{did}')
    def change(did: str, value: DraftAction, request: Request):
        return service.change_draft(actor(request), did, **value.model_dump())

    @app.get('/portal/api/keys')
    def keys(request: Request):
        with service.store.transaction() as db:
            return [registry.public_key(registry.store.key(db, r[0])) for r in db.execute('SELECT key_id FROM key_owners WHERE user_id=?', (actor(request)['id'],))]

    @app.post('/portal/api/keys', status_code=201)
    def create_key(value: UserKey, request: Request):
        a = actor(request)
        if a.get('platform'):
            fail('user_required', '用户 Key 请用用户账号创建', 400)
        if not set(value.submit_library_ids).issubset(value.library_ids):
            fail('invalid_grants', '提交范围必须在阅读范围内', 400)
        # Generate key and ownership atomically; never expose an unowned intermediate key.
        with service.store.transaction(write=True) as db:
            for lid in value.library_ids:
                service.check(db, a, lid, 2 if lid in value.submit_library_ids else 1)
            kid = uuid.uuid4().hex[:16]
            token = 'fkb_' + kid + '_' + secrets.token_urlsafe(32)
            db.execute('INSERT INTO agent_keys VALUES (?,?,?,?,NULL,NULL)', (kid, value.name, digest(token), now()))
            db.execute('INSERT INTO key_owners VALUES (?,?)', (kid, a['id']))
            db.executemany('INSERT INTO key_libraries VALUES (?,?)', [(kid, l) for l in set(value.library_ids)])
            db.executemany('INSERT INTO inbox_grants VALUES (?,?)', [(kid, l) for l in set(value.submit_library_ids)])
            service.event(db, a, 'user_key_create', kid)
        return {'id': kid, 'token': token}

    @app.post('/portal/api/keys/{kid}/revoke')
    def revoke(kid: str, request: Request):
        with service.store.transaction(write=True) as db:
            if not db.execute('SELECT 1 FROM key_owners WHERE key_id=? AND user_id=?', (kid, actor(request)['id'])).fetchone():
                fail('key_forbidden', '无此 Key 管理权限')
            db.execute('UPDATE agent_keys SET revoked_at=? WHERE id=?', (now(), kid))
            service.event(db, actor(request), 'user_key_revoke', kid)
        return {'ok': True}
