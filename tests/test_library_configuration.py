import pytest

from tests.test_collaboration import ADMIN, account, env, headers, library, organization


def test_configuration_follows_ownership_not_content_grants(env):
    c, registry = env
    uid, member = account(c, 'member')
    _, boss = account(c, 'boss')
    _, outsider = account(c, 'outsider')
    oid = organization(c, 'boss')
    organization(c, 'outsider', '另一组织')
    c.put(f'/portal/api/organizations/{oid}/members', headers=headers(boss),
          json={'username': 'member', 'role': 'member'})
    lid = library(c, boss, oid)
    personal = library(c, member)
    base = f'/portal/api/libraries/{lid}'
    assert c.put(base + '/members', headers=headers(boss), json={'user_id': uid, 'role': 'manager'}).status_code == 200
    listed = c.get('/portal/api/libraries', headers=headers(member)).json()
    assert next(r for r in listed if r['id'] == lid)['role'] == 4
    assert next(r for r in listed if r['id'] == lid)['can_configure'] is False
    assert next(r for r in listed if r['id'] == personal)['can_configure'] is True
    for token in (member, outsider):
        for suffix in ('members', 'candidates', 'agent-settings', 'sharing', 'unit-grants', 'index', 'index/search?query=test'):
            assert c.get(base + '/' + suffix, headers=headers(token)).status_code == 403
        assert c.post(base + '/index/build', headers=headers(token)).status_code == 403
        assert c.put(base + '/settings', headers=headers(token), json={'name': 'forbidden'}).status_code == 403
        assert c.put(base + '/members', headers=headers(token), json={'user_id': uid, 'role': 'remove'}).status_code == 403
    assert c.get(f'/portal/api/libraries/{personal}/index', headers=headers(boss)).status_code == 403
    for token, target in ((boss, lid), (member, personal), (ADMIN, lid)):
        url = f'/portal/api/libraries/{target}'
        assert c.put(url + '/settings', headers=headers(token), json={'name': '  新名称  '}).json()['name'] == '新名称'
        assert c.get(url + '/index', headers=headers(token)).json()['status'] == 'unconfigured'
        assert c.get(url + '/agent-settings', headers=headers(token)).status_code == 200
        for body in ({'name': 'bad', 'path': 'other'}, {'name': 'bad', 'organization_id': oid}, {'name': '   '}):
            assert c.put(url + '/settings', headers=headers(token), json=body).status_code == 422
    # Demotion is effective on the next request, even if the former admin has a manager grant.
    boss_id = next(u['id'] for u in c.get('/admin/api/accounts', headers=headers(ADMIN)).json() if u['username'] == 'boss')
    c.put(base + '/members', headers=headers(boss), json={'user_id': boss_id, 'role': 'manager'})
    assert c.put(f'/portal/api/organizations/{oid}/members', headers=headers(ADMIN), json={'username': 'member', 'role': 'admin'}).status_code == 200
    assert c.put(f'/portal/api/organizations/{oid}/members', headers=headers(ADMIN), json={'username': 'boss', 'role': 'member'}).status_code == 200
    assert c.put(base + '/settings', headers=headers(boss), json={'name': 'denied'}).status_code == 403
    assert c.get('/admin/api/libraries', headers=headers(member)).status_code == 401
    with registry.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM collaboration_events WHERE action='library_settings'").fetchone()[0] == 3


def test_portal_index_uses_existing_plugin_and_grep_stays_available(env, tmp_path):
    from pathlib import Path
    import json
    from folder_kb.plugin_package import package_plugin
    c, registry = env
    _, token = account(c, 'owner')
    lid = library(c, token)
    source = Path('tests/fixtures/legacy-fulltext')
    package = package_plugin(source / 'package', source / 'definition.json', tmp_path / 'dist')
    manifest = json.loads(Path(package['manifest']).read_text(encoding='utf-8'))
    assert c.post('/admin/api/plugins', headers=headers(ADMIN), json=manifest).status_code == 201
    assert c.put('/admin/api/plugins/test-legacy-fulltext/versions/1.0.0/package', headers=headers(ADMIN),
                 content=Path(package['package']).read_bytes()).status_code == 200
    assert c.put(f'/admin/api/libraries/{lid}/plugin', headers=headers(ADMIN),
                 json={'plugin_id': 'test-legacy-fulltext', 'plugin_version': '1.0.0'}).status_code == 200
    base = f'/portal/api/libraries/{lid}'
    assert c.get(base + '/index', headers=headers(token)).json()['status'] == 'missing'
    assert c.post(base + '/index/build', headers=headers(token)).status_code == 200
    assert c.get(base + '/index', headers=headers(token)).json()['status'] == 'ready'
    result = c.get(base + '/index/search?query=工作知识', headers=headers(token))
    assert result.status_code == 200, result.text
    assert result.json()['results'][0]['path'] == 'README.md'
    assert c.get(base + '/search?query=工作知识', headers=headers(token)).json()['matches']
    assert c.post(f'/admin/api/libraries/{lid}/index/build', headers=headers(token)).status_code == 401
