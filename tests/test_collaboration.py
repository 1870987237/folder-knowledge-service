import hashlib
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from folder_kb.server import create_app, Settings


ADMIN = 'collaboration-test-administrator-token'
PASSWORD = 'test-password-long-enough'


def headers(token):
    return {'Authorization': 'Bearer ' + token}


@pytest.fixture
def env(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    app = create_app(Settings(str(root), admin_token=ADMIN, state_dir=str(tmp_path / 'state'), allowed_hosts=('testserver',)))
    with TestClient(app) as client:
        yield client, app.state.registry


def account(c, name):
    value = {'username': name, 'password': PASSWORD}
    assert c.post('/portal/api/register', json=value).status_code == 201
    accounts = c.get('/admin/api/accounts', headers=headers(ADMIN)).json()
    uid = next(u['id'] for u in accounts if u['username'] == name)
    assert c.put('/admin/api/accounts/' + uid, headers=headers(ADMIN), json={'status': 'active'}).status_code == 200
    response = c.post('/portal/api/login', json=value)
    assert response.status_code == 200, response.text
    return uid, response.json()['token']


def library(c, token, organization_id=None):
    r = c.post('/portal/api/libraries', headers=headers(token), json={'name': '工作知识', 'organization_id': organization_id})
    assert r.status_code == 201, r.text
    return r.json()['id']


def draft(c, token, lid, content='# 修改后\n', path='README.md'):
    r = c.get(f'/portal/api/libraries/{lid}/document', headers=headers(token), params={'path': path})
    version = r.json()['version'] if r.status_code == 200 else hashlib.sha256(b'').hexdigest()
    r = c.post(f'/portal/api/libraries/{lid}/drafts', headers=headers(token), json={'path': path, 'content': content, 'expected_version': version})
    assert r.status_code == 201, r.text
    return r.json()['id']


def action(c, token, did, action, revision=1, **kwargs):
    return c.post('/portal/api/drafts/' + did, headers=headers(token), json={'action': action, 'revision': revision, **kwargs})


def organization(c, user, name='测试组织'):
    oid = c.post('/portal/api/organizations', headers=headers(ADMIN), json={'name': name}).json()['id']
    assert c.put(f'/portal/api/organizations/{oid}/members', headers=headers(ADMIN), json={'username': user, 'role': 'admin'}).status_code == 200
    return oid


def test_pending_account_disabled_logout_and_admin_boundary(env):
    c, _ = env
    value = {'username': 'pending', 'password': PASSWORD}
    assert c.post('/portal/api/register', json=value).status_code == 201
    assert c.post('/portal/api/login', json=value).status_code == 401
    uid, token = account(c, 'active')
    assert c.get('/portal/api/me', headers=headers(token)).status_code == 200
    assert c.get('/admin/api/accounts', headers=headers(token)).status_code == 401
    assert c.post('/portal/api/logout', headers=headers(token)).status_code == 200
    assert c.get('/portal/api/me', headers=headers(token)).status_code == 401
    token = c.post('/portal/api/login', json={'username': 'active', 'password': PASSWORD}).json()['token']
    c.put('/admin/api/accounts/' + uid, headers=headers(ADMIN), json={'status': 'disabled'})
    assert c.get('/portal/api/me', headers=headers(token)).status_code == 401


def test_private_libraries_are_not_visible_to_organization_admin(env):
    c, _ = env
    _, alice = account(c, 'alice')
    _, boss = account(c, 'boss')
    oid = organization(c, 'boss')
    c.put(f'/portal/api/organizations/{oid}/members', headers=headers(boss), json={'username': 'alice', 'role': 'member'})
    lid = library(c, alice)
    assert c.get('/portal/api/libraries', headers=headers(boss)).json() == []
    assert c.get(f'/portal/api/libraries/{lid}/document?path=README.md', headers=headers(boss)).status_code == 403
    assert c.get(f'/portal/api/libraries/{lid}/search?query=secret', headers=headers(boss)).status_code == 403


def test_draft_review_publish_and_agent_read(env):
    c, registry = env
    uid, token = account(c, 'alice')
    lid = library(c, token)
    r = c.post('/portal/api/keys', headers=headers(token), json={'name': 'Agent', 'library_ids': [lid], 'submit_library_ids': [lid]})
    assert r.status_code == 201, r.text
    key = r.json()['token']
    old_root = next(l['path'] for l in registry.libraries() if l['id'] == lid)
    did = draft(c, token, lid)
    listed = c.get(f'/portal/api/libraries/{lid}/drafts', headers=headers(token))
    assert listed.status_code == 200 and listed.json()[0]['id'] == did
    assert '修改后' not in registry.run(key, lid, 'read_file', {'path': 'README.md'}, 'rest')['lines'][0]['text']
    assert action(c, token, did, 'publish').status_code == 409
    assert action(c, token, did, 'submit').status_code == 200
    assert action(c, token, did, 'save', 2, content='偷改').status_code == 409
    assert action(c, token, did, 'publish', 2).status_code == 200
    assert '修改后' in registry.run(key, lid, 'read_file', {'path': 'README.md'}, 'mcp')['lines'][0]['text']
    assert (registry.pool.root / old_root / 'README.md').exists()
    assert action(c, token, did, 'publish', 3).status_code == 409
    c.put('/admin/api/accounts/' + uid, headers=headers(ADMIN), json={'status': 'disabled'})
    assert c.get('/api/v1/libraries', headers=headers(key)).status_code == 401


def test_org_roles_scope_and_key_follow_membership(env):
    c, registry = env
    _, boss = account(c, 'boss')
    eid, editor = account(c, 'editor')
    _, outsider = account(c, 'outsider')
    oid = organization(c, 'boss')
    c.put(f'/portal/api/organizations/{oid}/members', headers=headers(boss), json={'username': 'editor', 'role': 'member'})
    lid = library(c, boss, oid)
    assert c.get('/portal/api/libraries', headers=headers(editor)).json() == []
    assert c.put(f'/portal/api/libraries/{lid}/members', headers=headers(boss), json={'user_id': eid, 'role': 'editor'}).status_code == 200
    did = draft(c, editor, lid)
    assert action(c, editor, did, 'submit').status_code == 200
    assert action(c, editor, did, 'publish', 2).status_code == 403
    assert c.get('/portal/api/drafts/' + did, headers=headers(outsider)).status_code == 403
    key = c.post('/portal/api/keys', headers=headers(editor), json={'name': 'Agent', 'library_ids': [lid]}).json()['token']
    assert registry.visible_libraries(key)[0]['id'] == lid
    assert c.post('/portal/api/keys', headers=headers(outsider), json={'name': 'Agent', 'library_ids': [lid]}).status_code == 403
    c.put(f'/portal/api/organizations/{oid}/members', headers=headers(boss), json={'username': 'editor', 'role': 'remove'})
    assert registry.visible_libraries(key) == []
    assert action(c, boss, did, 'publish', 2).status_code == 409
    assert c.get(f'/portal/api/libraries/{lid}/document?path=README.md', headers=headers(editor)).status_code == 403


def test_conflicting_drafts_external_changes_and_revision(env):
    c, registry = env
    _, token = account(c, 'alice')
    lid = library(c, token)
    a, b = draft(c, token, lid, '# A'), draft(c, token, lid, '# B')
    assert action(c, token, a, 'save', content='# A changed').status_code == 200
    assert action(c, token, a, 'submit', 1).status_code == 409
    assert action(c, token, a, 'submit', 2).status_code == 200
    assert action(c, token, b, 'submit').status_code == 200
    assert action(c, token, a, 'publish', 3).status_code == 200
    assert action(c, token, b, 'publish', 2).status_code == 409
    d = draft(c, token, lid)
    action(c, token, d, 'submit')
    current = next(l for l in registry.libraries() if l['id'] == lid)
    (registry.pool.root / current['path'] / 'README.md').write_text('external', encoding='utf-8')
    assert action(c, token, d, 'publish', 2).status_code == 409


def test_new_file_and_unsafe_paths(env):
    c, _ = env
    _, token = account(c, 'alice')
    lid = library(c, token)
    did = draft(c, token, lid, '# 新文件', path='new.md')
    assert action(c, token, did, 'submit').status_code == 200
    assert action(c, token, did, 'publish', 2).status_code == 200
    for path in ('../outside.md', '/outside.md', '.hidden.md', 'C:/outside.md', 'a\\b.md', 'bad.py'):
        r = c.post(f'/portal/api/libraries/{lid}/drafts', headers=headers(token), json={'path': path, 'content': 'bad', 'expected_version': hashlib.sha256(b'').hexdigest()})
        assert 400 <= r.status_code < 500, (path, r.text)


def test_adopted_library_readonly_and_cross_org_denied(env):
    c, registry = env
    _, token = account(c, 'alice')
    _, other = account(c, 'bob')
    oid = organization(c, 'alice')
    other_oid = organization(c, 'bob', '另一个组织')
    path = registry.pool.root / 'external'
    path.mkdir()
    (path / 'README.md').write_text('# 外部文档', encoding='utf-8')
    registry.save_library('external', '外部知识库', 'external', True, create=True)
    assert c.put('/admin/api/libraries/external/ownership', headers=headers(ADMIN), json={'organization_id': oid}).status_code == 200
    assert c.get('/portal/api/libraries/external/document?path=README.md', headers=headers(token)).status_code == 200
    assert c.get('/portal/api/libraries', headers=headers(other)).json() == []
    version = hashlib.sha256('# 外部文档'.encode()).hexdigest()
    assert c.post('/portal/api/libraries/external/drafts', headers=headers(token), json={'path': 'README.md', 'content': 'no', 'expected_version': version}).status_code == 409
    assert c.put(f'/portal/api/organizations/{other_oid}/members', headers=headers(token), json={'username': 'alice', 'role': 'admin'}).status_code == 403
    assert c.put('/admin/api/libraries/external/ownership', headers=headers(ADMIN), json={'organization_id': other_oid}).status_code == 409


def test_origin_and_login_rate_limits(env):
    c, _ = env
    assert c.post('/portal/api/register', headers={'Origin': 'https://evil.example'}, json={'username': 'abc', 'password': PASSWORD}).status_code == 403
    for _ in range(20):
        assert c.post('/portal/api/login', json={'username': 'unknown', 'password': PASSWORD}).status_code == 401
    assert c.post('/portal/api/login', json={'username': 'unknown', 'password': PASSWORD}).status_code == 429


def test_revoked_submission_blocks_inbox_and_queued_work(env):
    from folder_kb.updates import Updates
    from folder_kb.errors import KnowledgeError
    from folder_kb.inbox import Inbox, Material
    c, registry = env
    _, boss = account(c, 'boss')
    uid, editor = account(c, 'editor')
    oid = organization(c, 'boss')
    c.put(f'/portal/api/organizations/{oid}/members', headers=headers(boss), json={'username': 'editor', 'role': 'member'})
    lid = library(c, boss, oid)
    c.put(f'/portal/api/libraries/{lid}/members', headers=headers(boss), json={'user_id': uid, 'role': 'editor'})
    key = c.post('/portal/api/keys', headers=headers(editor), json={'name': 'Agent', 'library_ids': [lid], 'submit_library_ids': [lid]}).json()
    inbox = Inbox(registry)
    item = inbox.submit(key['token'], Material(library_id=lid, submission_id='one', content={'text': 'test'}))
    c.put(f'/portal/api/libraries/{lid}/members', headers=headers(boss), json={'user_id': uid, 'role': 'reader'})
    with pytest.raises(KnowledgeError, match='not granted'):
        inbox.get(item['id'], key['token'])
    with registry.store.transaction() as db:
        assert not Updates.permitted(db, {'key_id': key['id'], 'library_id': lid})
    assert registry.visible_libraries(key['token'])


def test_reserved_mounts_and_native_binding_protected(env):
    from folder_kb.templates import TemplateCatalog, TemplateBinding
    from folder_kb.errors import KnowledgeError
    c, registry = env
    _, token = account(c, 'alice')
    oid = organization(c, 'alice')
    lid = library(c, token)
    registry.save_library('broad', 'Broad', '.', True, create=True)
    assert c.put('/admin/api/libraries/broad/ownership', headers=headers(ADMIN), json={'organization_id': oid}).status_code == 409
    with pytest.raises(KnowledgeError):
        registry.save_library(lid, 'changed', '.', True)
    with pytest.raises(KnowledgeError, match='普通文档库'):
        TemplateCatalog(registry).bind(lid, TemplateBinding(template_id='reference-docs', template_version='0.1.0'))
    # The publication unit cannot overlap another broad mount.
    assert not c.get('/portal/api/libraries', headers=headers(token)).json()[0]['editable']


def test_stale_read_content_reject_and_new_file_conflict(env):
    c, _ = env
    _, token = account(c, 'alice')
    lid = library(c, token)
    r = c.post(f'/portal/api/libraries/{lid}/drafts', headers=headers(token), json={'path': 'README.md', 'content': 'new', 'expected_version': '0' * 64})
    assert r.status_code == 409
    did = draft(c, token, lid)
    assert action(c, token, did, 'submit').status_code == 200
    assert action(c, token, did, 'reject', 2).status_code == 200
    assert action(c, token, did, 'publish', 3).status_code == 409
