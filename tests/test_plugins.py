"""Plugin contracts, legacy interoperability and native document coexistence."""
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from folder_kb.server import create_app
from folder_kb.plugin_package import package_plugin
from tests.test_template_management import env, archive, auth, definition, install


def declarative(data, **changes):
    return definition(data, capabilities=[], entrypoints={}, runtime={'type': 'declarative'},
                      config_schema={'type': 'object', 'additionalProperties': False},
                      plugin={'instructions': '回答前查找相关记忆，并标明来源。', 'indexing': {
                          'mode': 'hybrid', 'splitter': 'markdown_sections',
                          'include': ['**/*.md'], 'exclude': ['drafts/**'],
                          'metadata_fields': ['topic']}}, **changes)


def install_plugin(client, metadata=None, data=None):
    data = data or archive()
    metadata = metadata or declarative(data)
    response = client.post('/admin/api/plugins', headers=auth(), json=metadata)
    assert response.status_code == 201, response.text
    response = client.put(f"/admin/api/plugins/{metadata['id']}/versions/{metadata['version']}/package",
                          headers=auth(), content=data)
    assert response.status_code == 200, response.text
    return response.json()


def test_lifecycle_legacy_and_reopen(env):
    app, c, _, settings = env
    data = archive()  # Would raise if executed.
    value = declarative(data)
    r = c.post('/admin/api/plugins', headers=auth(), json=value)
    assert r.json()['status'] == 'registered'
    url = '/admin/api/libraries/alpha/plugin'
    binding = {'plugin_id': 'sample', 'plugin_version': '0.1.0', 'config': {}}
    assert c.put(url, headers=auth(), json=binding).json()['error'] == 'template_not_installed'
    plugin = install_plugin(c, value, data)
    assert plugin['capabilities'] == {'indexing': {'integration': 'definition_only'},
                                     'instructions': {'integration': 'definition_only'}}
    r = c.put(url, headers=auth(), json=binding)
    assert r.status_code == 200, r.text
    assert r.json()['plugin']['libraries'] == [{'id': 'alpha', 'name': 'alpha'}]
    assert c.get('/admin/api/libraries/alpha/template', headers=auth()).json()['binding']['template_id'] == 'sample'
    assert c.post('/admin/api/plugins', headers=auth(), json=value).json()['duplicate']
    assert c.post('/admin/api/plugins', headers=auth(), json=dict(value, name='changed')).status_code == 409
    assert c.put(url, headers=auth(), json=dict(binding, config={'unexpected': True})).status_code == 422
    assert c.put('/admin/api/libraries/alpha/update-policy', headers=auth(), json={
        'enabled': True, 'approved_package_sha256': plugin['package_sha256']}).json()['error'] == 'operation_unsupported'
    with TestClient(create_app(settings)) as restarted:
        assert restarted.get(url, headers=auth()).json()['binding']['plugin_id'] == 'sample'
    assert c.delete(url, headers=auth()).json()['binding'] is None
    assert c.get('/admin/api/plugins', headers=auth()).json()['plugins'][0]['libraries'] == []
    # Old clients remain usable and old manifest equality is preserved.
    old = definition(data, id='legacy')
    install(c, data, old)
    assert c.post('/admin/api/plugins', headers=auth(), json=old).json()['duplicate']
    assert c.get('/admin/api/plugins/legacy/versions/0.1.0', headers=auth()).json()['legacy_template']
    assert (Path(settings.root) / 'INDEX.md').read_text() == 'Original content'


@pytest.mark.parametrize('method,path', [
    ('get', '/plugins'), ('post', '/plugins'), ('get', '/plugins/schema'),
    ('get', '/plugins/sample/versions/0.1.0'), ('put', '/plugins/sample/versions/0.1.0/package'),
    ('get', '/libraries/alpha/plugin'), ('put', '/libraries/alpha/plugin'), ('delete', '/libraries/alpha/plugin'),
])
def test_admin_only(env, method, path):
    _, c, key, _ = env
    for headers in ({}, auth(key['token'])):
        assert getattr(c, method)('/admin/api' + path, headers=headers).status_code == 401


def test_bounds_schema_and_bad_checksum(env, monkeypatch):
    _, c, _, _ = env
    schema = c.get('/admin/api/plugins/schema', headers=auth()).json()
    assert 'plugin' in schema['properties']
    assert c.post('/admin/api/plugins', headers=auth(), content=b'x' * 65537).status_code == 413
    assert c.put('/admin/api/libraries/alpha/plugin', headers=auth(), content=b'x' * 65537).status_code == 413
    data = archive()
    c.post('/admin/api/plugins', headers=auth(), json=declarative(data))
    url = '/admin/api/plugins/sample/versions/0.1.0/package'
    assert c.put(url, headers=auth(), content=b'wrong').status_code == 409
    import folder_kb.templates as templates
    monkeypatch.setattr(templates, 'MAX_PACKAGE_BYTES', 1024)
    assert c.put(url, headers=auth(), content=iter([b'x' * 800, b'x' * 800])).status_code == 413


@pytest.mark.parametrize('index', [
    {'include': ['../secret/**']}, {'include': ['/etc/**']}, {'include': ['C:/data/*']},
    {'splitter': 'custom'}, {'splitter': 'custom', 'entrypoint': []},
    {'splitter': 'custom', 'entrypoint': ['sh', 'run.py']},
    {'splitter': 'custom', 'entrypoint': ['python', '../run.py']},
    {'splitter': 'document', 'entrypoint': ['python', 'run.py']},
])
def test_invalid_index_contract(env, index):
    _, c, _, _ = env
    value = declarative(archive())
    value['plugin']['indexing'].update(index)
    assert c.post('/admin/api/plugins', headers=auth(), json=value).status_code == 422


def test_custom_index_entrypoint_is_checked_not_executed(env):
    _, c, _, _ = env
    value = declarative(archive())
    value['runtime'] = {'type': 'trusted-python-demo'}
    value['plugin']['indexing'].update(splitter='custom', entrypoint=['python', 'missing.py'])
    assert c.post('/admin/api/plugins', headers=auth(), json=value).status_code == 201
    r = c.put('/admin/api/plugins/sample/versions/0.1.0/package', headers=auth(), content=archive())
    assert r.json()['error'] == 'missing_template_entrypoint'
    value['version'] = '0.2.0'
    value['plugin']['indexing']['entrypoint'] = ['python', 'program.py']
    assert install_plugin(c, value)['capabilities']['indexing']['integration'] == 'definition_only'


def test_native_can_publish_with_declarative_plugin(env):
    _, c, _, _ = env
    # Use this fixture's admin token; do not reuse another fixture's credentials.
    credentials = {'username': 'native-reader', 'password': 'plugin-test-password-long-enough'}
    assert c.post('/portal/api/register', json=credentials).status_code == 201
    uid = c.get('/admin/api/accounts', headers=auth()).json()[0]['id']
    c.put('/admin/api/accounts/' + uid, headers=auth(), json={'status': 'active'})
    token = c.post('/portal/api/login', json=credentials).json()['token']
    lid = c.post('/portal/api/libraries', headers=auth(token), json={'name': 'Personal'}).json()['id']
    install_plugin(c)
    url = f'/admin/api/libraries/{lid}/plugin'
    binding = {'plugin_id': 'sample', 'plugin_version': '0.1.0'}
    assert c.put(url, headers=auth(), json=binding).status_code == 200
    assert c.get(url, headers=auth()).json()['binding']['workspace'] is None
    from tests.test_collaboration import draft, action
    did = draft(c, token, lid)
    assert action(c, token, did, 'submit').status_code == 200
    assert action(c, token, did, 'publish', revision=2).status_code == 200
    assert c.put(url, headers=auth(), json=binding).status_code == 200
    assert c.get('/portal/api/libraries', headers=auth(token)).json()[0]['editable']
    install(c, metadata=definition(archive(), id='writer'))
    assert c.put(url, headers=auth(), json={'plugin_id': 'writer', 'plugin_version': '0.1.0',
                                           'config': {'language': 'zh'}}).json()['error'] == 'native_library_managed'


def test_packaged_example(env, tmp_path):
    _, c, _, _ = env
    base = Path(__file__).resolve().parents[1]
    files = package_plugin(base / 'examples/plugins/personal-memory/package',
                           base / 'examples/plugins/personal-memory/definition.json', tmp_path / 'dist')
    value = json.loads(Path(files['manifest']).read_text(encoding='utf-8'))
    result = install_plugin(c, value, Path(files['package']).read_bytes())
    assert result['definition']['plugin']['indexing']['mode'] == 'hybrid'
