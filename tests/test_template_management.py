from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import stat
import zipfile

from fastapi.testclient import TestClient
import pytest

from folder_kb.errors import KnowledgeError
from folder_kb.server import Settings, create_app
from folder_kb.templates import TemplateRegistration

ADMIN = 'template-admin-test-only-token-long-enough'


def auth(token=ADMIN):
    return {'Authorization': 'Bearer ' + token}


def archive(files=None):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w', compression=zipfile.ZIP_DEFLATED) as zip:
        for name, content in (files or {'program.py': 'raise RuntimeError("never execute on install")'}).items():
            info = zipfile.ZipInfo('placeholder')
            info.filename = name  # Preserve deliberately unsafe paths on Windows too.
            info.compress_type = zipfile.ZIP_DEFLATED
            zip.writestr(info, content)
    return data.getvalue()


def definition(data, **changes):
    value = {'id': 'sample', 'version': '0.1.0', 'name': 'Example', 'maintainer': 'developer',
             'source': {'archive_sha256': hashlib.sha256(data).hexdigest()},
             'capabilities': ['update'], 'entrypoints': {'update': ['python', 'program.py', 'update']},
             'runtime': {'type': 'trusted-python-demo'},
             'config_schema': {'type': 'object', 'properties': {'language': {'enum': ['zh', 'en']}},
                               'required': ['language'], 'additionalProperties': False}}
    return dict(value, **changes)


@pytest.fixture
def env(tmp_path):
    root = tmp_path / 'knowledge'
    root.mkdir()
    (root / 'INDEX.md').write_text('Original content', encoding='utf-8')
    settings = Settings(str(root), admin_token=ADMIN, state_dir=str(tmp_path / 'state'),
                        allowed_hosts=('testserver',))
    app = create_app(settings)
    app.state.registry.save_library('alpha', 'Alpha', '.', True, create=True)
    app.state.registry.save_library('beta', 'Beta', '.', True, create=True)
    key = app.state.registry.create_key('agent', ['alpha'], None)
    with TestClient(app) as client:
        yield app, client, key, settings


def install(client, data=None, metadata=None):
    data = data or archive()
    value = metadata or definition(data)
    registered = client.post('/admin/api/templates', headers=auth(), json=value)
    assert registered.status_code == 201, registered.text
    result = client.put('/admin/api/templates/' + value['id'] + '/versions/' + value['version'] + '/package',
                        headers=auth(), content=data)
    assert result.status_code == 200, result.text
    return result.json()


def test_admin_lifecycle(env):
    app, client, key, settings = env
    data = archive()
    assert client.post('/admin/api/templates', json=definition(data)).status_code == 401
    assert client.post('/admin/api/templates', headers=auth(key['token']), json=definition(data)).status_code == 401
    result = client.post('/admin/api/templates', headers=auth(), json=definition(data))
    assert result.json()['status'] == 'registered'
    binding = {'template_id': 'sample', 'template_version': '0.1.0', 'config': {'language': 'zh'}}
    url = '/admin/api/libraries/alpha/template'
    assert client.put(url, headers=auth(), json=binding).json()['error'] == 'template_not_installed'
    assert client.post('/admin/api/templates', headers=auth(), json=definition(data)).json()['duplicate']
    assert client.post('/admin/api/templates', headers=auth(), json=definition(data, name='changed')).status_code == 409
    installed = install(client, data)
    assert installed['status'] == 'installed' and len(installed['package_sha256']) == 64
    assert client.put(url, headers=auth(key['token']), json=binding).status_code == 401
    assert client.put(url, headers=auth(), json=dict(binding, config={'language': 'invalid'})).status_code == 422
    assert client.put(url, headers=auth(), json=binding).status_code == 200
    binding['config']['language'] = 'en'
    assert client.put('/admin/api/libraries/beta/template', headers=auth(), json=binding).status_code == 200
    reopened = create_app(settings)
    assert reopened.state.templates.binding('alpha')['binding']['config'] == {'language': 'zh'}
    assert reopened.state.templates.binding('beta')['binding']['config'] == {'language': 'en'}
    assert (Path(settings.root) / 'INDEX.md').read_text() == 'Original content'
    # No program is executed by registration, installation or binding.
    assert client.delete(url, headers=auth()).json()['binding'] is None
    assert client.get(url, headers=auth()).json()['binding'] is None
    assert client.get('/admin/api/libraries/missing/template', headers=auth()).status_code == 404
    logs = json.dumps(app.state.registry.recent_events())
    assert 'language' not in logs and key['token'] not in logs


def test_digest_immutable_install_and_tamper(env):
    app, client, _, _ = env
    data = archive()
    client.post('/admin/api/templates', headers=auth(), json=definition(data))
    endpoint = '/admin/api/templates/sample/versions/0.1.0/package'
    assert client.put(endpoint, headers=auth(), content=b'wrong').json()['error'] == 'template_digest_mismatch'
    install(client, data)
    assert client.put(endpoint, headers=auth(), content=data).status_code == 200
    path = app.state.registry.directory / 'templates/sample/0.1.0/package/program.py'
    path.write_text('tampered')
    assert client.put(endpoint, headers=auth(), content=data).status_code == 409
    binding = {'template_id': 'sample', 'template_version': '0.1.0', 'config': {'language': 'zh'}}
    assert client.put('/admin/api/libraries/alpha/template', headers=auth(), json=binding).json()['error'] == 'template_package_changed'
    assert path.read_text() == 'tampered'


@pytest.mark.parametrize('name', ['../escape.py', '/absolute.py', 'C:/evil.py', 'a\\evil.py', 'CON', 'a/../bad', 'a./b'])
def test_bad_zip_paths(env, name):
    app, client, _, _ = env
    data = archive({'program.py': 'pass', name: 'bad'})
    client.post('/admin/api/templates', headers=auth(), json=definition(data))
    result = client.put('/admin/api/templates/sample/versions/0.1.0/package', headers=auth(), content=data)
    assert result.status_code == 400
    assert not (app.state.registry.directory / 'templates/sample/0.1.0').exists()
    assert not list((app.state.registry.directory / 'templates/.staging').iterdir())


def test_link_duplicate_missing_entry_and_expansion(env, monkeypatch):
    app, client, _, _ = env
    import folder_kb.templates as module
    cases = []
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as zip:
        info = zipfile.ZipInfo('program.py')
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zip.writestr(info, '/etc/passwd')
    cases.append(data.getvalue())
    cases.append(archive({'program.py': 'pass', 'PROGRAM.PY': 'collision'}))
    cases.append(archive({'readme.md': 'missing script'}))
    cases.append(archive({'program.py': 'x' * 2000}))
    monkeypatch.setattr(module, 'MAX_PACKAGE_BYTES', 1024)
    for i, data in enumerate(cases):
        version = '1.' + str(i)
        client.post('/admin/api/templates', headers=auth(), json=definition(data, version=version))
        result = client.put('/admin/api/templates/sample/versions/' + version + '/package', headers=auth(), content=data)
        assert result.status_code == 400, result.text


def test_stream_and_metadata_limits(env, monkeypatch):
    _, client, _, _ = env
    import folder_kb.templates as module
    client.post('/admin/api/templates', headers=auth(), json=definition(archive()))
    monkeypatch.setattr(module, 'MAX_PACKAGE_BYTES', 1024)
    assert client.put('/admin/api/templates/sample/versions/0.1.0/package', headers=auth(),
                      content=iter([b'x' * 800, b'x' * 800])).status_code == 413
    assert client.post('/admin/api/templates', headers=auth(), content=b'x' * 65537).status_code == 413
    assert client.put('/admin/api/libraries/alpha/template', headers=auth(), content=b'x' * 65537).status_code == 413


@pytest.mark.parametrize('change', [
    {'entrypoints': {'update': ['bash', '-c', 'anything']}},
    {'entrypoints': {'update': ['python', '../outside.py']}},
    {'capabilities': ['initialize']},
    {'runtime': {'type': 'container', 'image': 'python:latest'}},
    {'config_schema': {'type': 'object', '$ref': 'https://example.com/schema'}},
    {'config_schema': {'type': 'object', 'properties': {'x': {'type': 'invalid'}}}},
])
def test_invalid_metadata(env, change):
    _, client, _, _ = env
    assert client.post('/admin/api/templates', headers=auth(), json=definition(archive(), **change)).status_code == 422


def test_schema_local_ref_and_secret_gate(env):
    _, client, _, _ = env
    schema = {'type': 'object', '$defs': {'lang': {'enum': ['zh']}},
              'properties': {'language': {'$ref': '#/$defs/lang'}}, 'required': ['language']}
    install(client, metadata=definition(archive(), config_schema=schema))
    value = {'template_id': 'sample', 'template_version': '0.1.0', 'config': {'language': 'zh'}}
    assert client.put('/admin/api/libraries/alpha/template', headers=auth(), json=value).status_code == 200
    install(client, metadata=definition(archive(), version='0.2.0', required_secrets=['MODEL_API_KEY']))
    value['template_version'] = '0.2.0'
    assert client.put('/admin/api/libraries/alpha/template', headers=auth(), json=value).json()['error'] == 'template_secrets_unavailable'


def test_concurrent_install_and_orphan_recovery(env):
    app, client, _, _ = env
    data = archive()
    app.state.templates.register(TemplateRegistration(**definition(data)))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: app.state.templates.install('sample', '0.1.0', data), range(4)))
    assert {r['status'] for r in results} == {'installed'}
    assert not list((app.state.registry.directory / 'templates/.staging').iterdir())
    # Models a rename completed just before the DB transaction was rolled back.
    with app.state.registry.store.transaction(write=True) as db:
        db.execute("UPDATE template_versions SET package_sha256=''")
    assert app.state.templates.install('sample', '0.1.0', data)['status'] == 'installed'
    install(client, metadata=definition(data, version='0.2.0'))
    page = client.get('/admin/api/templates?limit=1', headers=auth()).json()
    next_page = client.get('/admin/api/templates', headers=auth(), params={'offset': page['next_offset']}).json()
    assert len(next_page['templates']) == 1
