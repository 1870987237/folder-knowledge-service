import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import threading
import time

import httpx
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import pytest
import uvicorn

from folder_kb.core import KnowledgeError, KnowledgeFolder
from folder_kb.management import Registry
from folder_kb.server import Settings, create_app

ADMIN = 'test-administrator-token-for-tests-only-abcdef'


@pytest.fixture
def managed(tmp_path):
    root = tmp_path / 'knowledge'
    for name in ('alpha', 'beta', 'replacement'):
        directory = root / name
        directory.mkdir(parents=True)
        (directory / 'README.md').write_text(name + ' private knowledge\n', encoding='utf-8')
    settings = Settings(str(root), admin_token=ADMIN, state_dir=str(tmp_path / 'state'),
                        allowed_hosts=('testserver', '127.0.0.1', '127.0.0.1:*'))
    app = create_app(settings)
    with TestClient(app) as client:
        yield app, client, settings


def authorization(token):
    return {'Authorization': 'Bearer ' + token}


def add_library(client, name):
    result = client.post('/admin/api/libraries', headers=authorization(ADMIN),
                         json={'id': name, 'name': name, 'path': name})
    assert result.status_code == 201, result.text
    return result.json()


def add_key(client, libraries, name='agent'):
    result = client.post('/admin/api/keys', headers=authorization(ADMIN),
                         json={'agent_name': name, 'library_ids': libraries})
    assert result.status_code == 201, result.text
    return result.json()


def test_admin_boundary_and_one_time_secret(managed):
    app, client, settings = managed
    assert client.get('/admin').status_code == 200
    assert client.get('/admin/assets/app.js').status_code == 200
    assert "script-src 'self'" in client.get('/admin').headers['Content-Security-Policy']
    assert client.get('/admin/api/overview').status_code == 401
    assert client.get('/api/v1/list', headers=authorization(ADMIN)).status_code == 401
    add_library(client, 'alpha')
    key = add_key(client, ['alpha'])
    assert client.get('/admin/api/overview', headers=authorization(key['token'])).status_code == 401
    assert client.get('/admin/api/overview', headers=dict(authorization(ADMIN), Origin='http://evil.example')).status_code == 403
    assert client.get('/admin/api/overview', headers=dict(authorization(ADMIN), Origin='http://[')).status_code == 403
    assert client.get('/admin/api/overview', headers=dict(authorization(ADMIN), Origin='http://testserver')).status_code == 200
    data = client.get('/admin/api/overview', headers=authorization(ADMIN)).json()
    assert key['token'] not in json.dumps(data) and 'digest' not in json.dumps(data)
    with app.state.registry.store.transaction() as db:
        disk = '\n'.join(db.iterdump())
    assert key['token'] not in disk and ADMIN not in disk
    assert client.get('/admin/api/overview', headers=authorization(ADMIN)).headers['cache-control'] == 'no-store'


def test_dynamic_scoping_disable_change_and_revoke(managed):
    app, client, settings = managed
    add_library(client, 'alpha'); add_library(client, 'beta')
    key = add_key(client, ['alpha'])
    headers = authorization(key['token'])
    assert client.get('/api/v1/libraries', headers=headers).json()['libraries'] == [{'id': 'alpha', 'name': 'alpha', 'availability': {'status': 'available', 'message': '正常'}}]
    assert client.get('/api/v1/read', headers=headers, params={'path': 'README.md'}).json()['lines'][0]['text'].startswith('alpha')
    assert client.get('/api/v1/read', headers=headers, params={'library_id': 'beta', 'path': 'README.md'}).status_code == 403
    assert client.get('/api/v1/read', headers=headers, params={'path': '../beta/README.md'}).status_code == 403
    assert client.put('/admin/api/libraries/alpha', headers=authorization(ADMIN),
                      json={'id': 'alpha', 'name': 'alpha', 'path': 'replacement', 'enabled': True}).status_code == 200
    assert client.get('/api/v1/read', headers=headers, params={'path': 'README.md'}).json()['lines'][0]['text'].startswith('replacement')
    client.put('/admin/api/libraries/alpha', headers=authorization(ADMIN),
               json={'id': 'alpha', 'name': 'alpha', 'path': 'replacement', 'enabled': False})
    assert client.get('/api/v1/read', headers=headers, params={'path': 'README.md', 'library_id': 'alpha'}).status_code == 403
    assert client.get('/api/v1/libraries', headers=headers).json()['libraries'] == []
    assert client.post('/admin/api/keys/'+key['key']['id']+'/revoke', headers=authorization(ADMIN)).status_code == 200
    assert client.get('/api/v1/libraries', headers=headers).status_code == 401


def test_multiple_libraries_require_explicit_selection(managed):
    _, client, _ = managed
    add_library(client, 'alpha'); add_library(client, 'beta')
    key = add_key(client, ['alpha', 'beta'])
    assert client.get('/api/v1/info', headers=authorization(key['token'])).status_code == 400
    assert client.get('/api/v1/info', headers=authorization(key['token']), params={'library_id':'beta'}).json()['library_id'] == 'beta'


def test_edit_key_grants_without_rotating_secret(managed):
    app, client, settings = managed
    add_library(client, 'alpha'); add_library(client, 'beta')
    key = add_key(client, ['alpha'])
    url = '/admin/api/keys/' + key['key']['id'] + '/grants'
    agent = authorization(key['token'])
    admin = authorization(ADMIN)
    with app.state.registry.store.transaction() as db:
        before = app.state.registry.store.key(db, key['key']['id'])
    assert client.get('/api/v1/read', headers=agent, params={'path':'README.md'}).status_code == 200
    assert client.put(url, json={'library_ids':['beta']}).status_code == 401
    assert client.put(url, headers=agent, json={'library_ids':['beta']}).status_code == 401
    assert client.put(url, headers=admin, json={'library_ids':['missing']}).status_code == 400
    assert client.put(url, headers=admin, json={'library_ids':['beta'], 'expires_at':None}).status_code == 422
    response = client.put(url, headers=admin, json={'library_ids':['beta','beta']})
    assert response.status_code == 200 and response.json()['library_ids'] == ['beta']
    assert 'digest' not in response.json() and 'token' not in response.json()
    assert client.get('/api/v1/read', headers=agent,
                      params={'library_id':'alpha','path':'README.md'}).status_code == 403
    assert client.get('/api/v1/read', headers=agent, params={'path':'README.md'}).json()['library_id'] == 'beta'
    with app.state.registry.store.transaction() as db:
        after = app.state.registry.store.key(db, key['key']['id'])
    assert {k:v for k,v in before.items() if k!='library_ids'} == {k:v for k,v in after.items() if k!='library_ids'}
    assert Registry(settings.root, settings.state_dir).authenticate(key['token'])['library_ids'] == ['beta']
    edits = [e for e in app.state.registry.recent_events()['events'] if e.get('action')=='key_grants_update']
    assert edits[0]['previous_library_ids'] == ['alpha'] and edits[0]['library_ids'] == ['beta']
    assert client.put(url, headers=admin, json={'library_ids':[]}).status_code == 200
    assert client.get('/api/v1/libraries', headers=agent).json()['libraries'] == []
    assert client.get('/api/v1/read', headers=agent,
                      params={'library_id':'beta','path':'README.md'}).status_code == 403
    assert client.put(url, headers=admin, json={'library_ids':['alpha']}).status_code == 200
    assert client.get('/api/v1/read', headers=agent, params={'path':'README.md'}).status_code == 200
    client.post('/admin/api/keys/'+key['key']['id']+'/revoke', headers=admin)
    assert client.put(url, headers=admin, json={'library_ids':['beta']}).status_code == 409
    assert client.get('/api/v1/libraries', headers=agent).status_code == 401
    assert client.put('/admin/api/keys/unknown/grants', headers=admin, json={'library_ids':[]}).status_code == 404


def test_edit_grants_preserves_expiry_and_unavailable_grants(managed):
    app, client, settings = managed
    add_library(client, 'alpha'); add_library(client, 'beta')
    key = add_key(client, ['alpha'])
    registry = app.state.registry
    with registry.store.transaction(write=True) as db:
        db.execute('UPDATE agent_keys SET expires_at=? WHERE id=?', ((datetime.now(timezone.utc)-timedelta(days=1)).isoformat(), key['key']['id']))
    (Path(settings.root)/'beta/README.md').unlink()
    (Path(settings.root)/'beta').rmdir()
    response = client.put('/admin/api/keys/'+key['key']['id']+'/grants', headers=authorization(ADMIN),
                          json={'library_ids':['beta']})
    assert response.status_code == 200 and response.json()['expires_at'] is not None
    assert client.get('/api/v1/libraries', headers=authorization(key['token'])).status_code == 401


def test_missing_library_recovery_and_independent_access(managed):
    app, client, settings = managed
    add_library(client, 'alpha'); add_library(client, 'beta')
    key = add_key(client, ['alpha', 'beta'])
    headers = authorization(key['token'])
    directory = Path(settings.root) / 'alpha'
    (directory / 'README.md').unlink()
    # A missing file is still a 404, not a whole-library outage.
    assert client.get('/api/v1/read', headers=headers,
                      params={'library_id': 'alpha', 'path': 'README.md'}).status_code == 404
    directory.rmdir()
    listed = client.get('/api/v1/libraries', headers=headers).json()['libraries']
    assert listed[0]['availability']['status'] == 'missing'
    assert listed[1]['availability']['status'] == 'available'
    assert client.get('/api/v1/read', headers=headers,
                      params={'library_id': 'beta', 'path': 'README.md'}).status_code == 200
    for operation in ('info', 'list', 'read', 'grep'):
        result = client.get('/api/v1/' + operation, headers=headers,
                            params={'library_id': 'alpha', 'path': 'README.md', 'query': 'x'})
        assert result.status_code == 503
        assert result.json()['error'] == 'library_unavailable'
        assert result.json()['reason'] == 'missing'
        assert settings.root not in result.text
    assert client.get('/admin/api/browse/alpha', headers=authorization(ADMIN)).status_code == 503
    row = client.get('/admin/api/overview', headers=authorization(ADMIN)).json()['libraries'][0]
    assert row['enabled'] and row['availability']['status'] == 'missing'
    event = app.state.registry.recent_events(key_id=key['key']['id'])['events'][0]
    assert event['library_id'] == 'alpha' and event['reason'] == 'missing'
    # No health data is persisted, and the existing grant survives recovery.
    with app.state.registry.store.transaction() as db:
        assert 'availability' not in dict(db.execute('SELECT * FROM libraries WHERE id=?', ('alpha',)).fetchone())
    directory.mkdir()
    (directory / 'README.md').write_text('restored', encoding='utf-8')
    assert client.get('/api/v1/read', headers=headers,
                      params={'library_id': 'alpha', 'path': 'README.md'}).json()['lines'][0]['text'] == 'restored'
    assert app.state.registry.snapshot()['libraries'][0]['availability']['status'] == 'available'


def test_unavailable_library_can_be_disabled_and_remapped(managed):
    _, client, settings = managed
    add_library(client, 'alpha')
    key = add_key(client, ['alpha'])
    directory = Path(settings.root) / 'alpha'
    (directory / 'README.md').unlink(); directory.rmdir()
    value = {'id': 'alpha', 'name': 'Renamed', 'path': 'alpha', 'enabled': False}
    assert client.put('/admin/api/libraries/alpha', headers=authorization(ADMIN), json=value).status_code == 200
    assert client.get('/api/v1/libraries', headers=authorization(key['token'])).json()['libraries'] == []
    value.update(path='replacement', enabled=True)
    assert client.put('/admin/api/libraries/alpha', headers=authorization(ADMIN), json=value).status_code == 200
    assert client.get('/api/v1/read', headers=authorization(key['token']),
                      params={'path': 'README.md'}).status_code == 200


@pytest.mark.parametrize('failure,expected', [(PermissionError, 'permission_denied'), (OSError, 'io_error')])
def test_directory_probe_errors_are_isolated(managed, monkeypatch, failure, expected):
    app, client, settings = managed
    add_library(client, 'alpha'); add_library(client, 'beta')
    key = add_key(client, ['alpha'])
    original = os.scandir
    def scan(path):
        if Path(path) == Path(settings.root) / 'alpha':
            raise failure('private server path must not leak')
        return original(path)
    monkeypatch.setattr(os, 'scandir', scan)
    rows = app.state.registry.snapshot()['libraries']
    assert rows[0]['availability']['status'] == expected
    assert rows[1]['availability']['status'] == 'available'
    response = client.get('/api/v1/info', headers=authorization(key['token']))
    assert response.status_code == 503 and response.json()['reason'] == expected
    assert 'private server path' not in response.text


def test_replaced_directory_and_disappearance_during_read(managed, monkeypatch):
    app, client, settings = managed
    add_library(client, 'alpha')
    key = add_key(client, ['alpha'])
    directory = Path(settings.root) / 'alpha'
    original = KnowledgeFolder.read_file
    def disappearing(folder, **kwargs):
        (directory / 'README.md').unlink(); directory.rmdir()
        return original(folder, **kwargs)
    monkeypatch.setattr(KnowledgeFolder, 'read_file', disappearing)
    response = client.get('/api/v1/read', headers=authorization(key['token']), params={'path': 'README.md'})
    assert response.status_code == 503 and response.json()['reason'] == 'missing'
    directory.write_text('now a file', encoding='utf-8')
    assert app.state.registry.snapshot()['libraries'][0]['availability']['status'] == 'not_directory'


@pytest.mark.skipif(os.name == 'nt', reason='POSIX permission bits required')
def test_real_directory_permissions(managed):
    if os.geteuid() == 0:
        pytest.skip('Root bypasses mode bits')
    app, client, settings = managed
    add_library(client, 'alpha')
    directory = Path(settings.root) / 'alpha'
    mode = directory.stat().st_mode
    try:
        directory.chmod(0)
        assert app.state.registry.snapshot()['libraries'][0]['availability']['status'] == 'permission_denied'
    finally:
        directory.chmod(mode)
    assert app.state.registry.snapshot()['libraries'][0]['availability']['status'] == 'available'


def test_expiry_and_persistence(managed):
    app, client, settings = managed
    add_library(client, 'alpha')
    past = (datetime.now(timezone.utc)-timedelta(days=1)).isoformat()
    assert client.post('/admin/api/keys', headers=authorization(ADMIN),
                       json={'agent_name': 'old', 'library_ids': ['alpha'], 'expires_at': past}).status_code == 400
    key = add_key(client, ['alpha'])
    restarted = Registry(settings.root, settings.state_dir)
    assert restarted.authenticate(key['token'])['id'] == key['key']['id']
    with restarted.store.transaction(write=True) as db:
        db.execute('UPDATE agent_keys SET expires_at=? WHERE id=?', (past, key['key']['id']))
    assert client.get('/api/v1/list', headers=authorization(key['token'])).status_code == 401


def test_admin_path_boundaries_and_read_only_preview(managed):
    _, client, settings = managed
    for path in ('..', '/etc', 'alpha/README.md'):
        result = client.post('/admin/api/libraries', headers=authorization(ADMIN),
                             json={'id': 'bad', 'name': 'bad', 'path': path})
        assert result.status_code in (400, 403)
    add_library(client, 'alpha')
    before = (Path(settings.root)/'alpha/README.md').read_bytes()
    assert client.get('/admin/api/browse/alpha', headers=authorization(ADMIN), params={'path':'README.md','read':True}).status_code == 200
    assert client.get('/admin/api/browse/alpha', headers=authorization(ADMIN), params={'path':'../beta/README.md','read':True}).status_code == 403
    assert (Path(settings.root)/'alpha/README.md').read_bytes() == before
    with pytest.raises(ValueError):
        Registry(settings.root, str(Path(settings.root)/'state'))
    with pytest.raises(ValueError):
        Settings(settings.root, admin_token=ADMIN, state_dir=settings.state_dir, token=ADMIN)


def test_parallel_keys_and_sqlite_state_not_lost(managed):
    app, client, settings = managed
    add_library(client, 'alpha')
    def create(i):
        return Registry(settings.root, settings.state_dir).create_key('agent-'+str(i), ['alpha'], None)
    with ThreadPoolExecutor(max_workers=6) as workers:
        keys = list(workers.map(create, range(12)))
    assert len(app.state.registry.snapshot()['keys']) == 12
    def access(key):
        return app.state.registry.run(key['token'], 'alpha', 'read_file', {'path':'README.md'}, 'rest')
    with ThreadPoolExecutor(max_workers=6) as workers:
        results = list(workers.map(access, keys))
    assert len(results) == 12
    snapshot = app.state.registry.snapshot()
    assert snapshot['metrics']['calls'] == 12
    assert all(k['activity']['calls'] == 1 for k in snapshot['keys'])
    assert not snapshot['active_requests']
    assert not list(Path(settings.state_dir).glob('*.tmp'))


def test_active_requests_and_log_redaction(managed, monkeypatch):
    app, client, settings = managed
    add_library(client, 'alpha')
    key = add_key(client, ['alpha'])
    started, release = threading.Event(), threading.Event()
    original = KnowledgeFolder.grep
    def blocked(folder, **kwargs):
        started.set(); release.wait(5)
        return original(folder, **kwargs)
    monkeypatch.setattr(KnowledgeFolder, 'grep', blocked)
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(app.state.registry.run, key['token'], 'alpha', 'grep', {'query':'SECRET_QUERY_VALUE'}, 'mcp')
        assert started.wait(3)
        assert app.state.registry.snapshot()['active_requests'][0]['key_id'] == key['key']['id']
        release.set(); future.result()
    logs = json.dumps(app.state.registry.recent_events(), ensure_ascii=False)
    assert 'SECRET_QUERY_VALUE' not in logs and 'private knowledge' not in logs and key['token'] not in logs
    assert app.state.registry.recent_events(key_id=key['key']['id'])['events'][0]['transport'] == 'mcp'


def test_real_mcp_identity_isolation_and_live_revocation(managed):
    app, client, settings = managed
    add_library(client, 'alpha'); add_library(client, 'beta')
    alpha = add_key(client, ['alpha'], 'alpha-agent'); beta = add_key(client, ['beta'], 'beta-agent')
    sock = socket.socket(); sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    # A distinct app and lifespan, sharing the SQLite registry as a restarted deployment would.
    server = uvicorn.Server(uvicorn.Config(create_app(settings), log_level='error', access_log=False))
    thread = threading.Thread(target=server.run, kwargs={'sockets':[sock]}, daemon=True); thread.start()
    try:
        deadline = time.monotonic()+10
        while not server.started:
            assert time.monotonic()<deadline
            time.sleep(.02)
        async def use(key, library):
            async with httpx.AsyncClient(headers=authorization(key['token']), trust_env=False) as http:
                async with streamable_http_client(f'http://127.0.0.1:{port}/mcp', http_client=http) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        assert 'list_libraries' in {t.name for t in tools.tools}
                        listed = await session.call_tool('list_libraries', {})
                        assert not listed.isError
                        assert listed.structuredContent['libraries'] == [{'id':library,'name':library,'availability':{'status':'available','message':'正常'}}]
                        result = await session.call_tool('read_file', {'path':'README.md'})
                        assert not result.isError
                        assert result.structuredContent['lines'][0]['text'] == library+' private knowledge'
                        denied = await session.call_tool('read_file', {'path':'README.md','library_id':'beta' if library=='alpha' else 'alpha'})
                        assert denied.isError
                        assert (await session.call_tool('create_key', {})).isError
        async def run():
            await asyncio.gather(use(alpha,'alpha'), use(beta,'beta'))
        asyncio.run(run())
        snapshot = app.state.registry.snapshot()
        assert snapshot['metrics']['calls'] == 6 and snapshot['metrics']['errors'] == 2
        records = [e for e in app.state.registry.recent_events()['events'] if e['kind']=='access']
        assert all(e['agent_name'] == ('alpha-agent' if e['key_id']==alpha['key']['id'] else 'beta-agent') for e in records)
        directory = Path(settings.root) / 'alpha'
        (directory / 'README.md').unlink(); directory.rmdir()
        async def check_outage():
            async with httpx.AsyncClient(headers=authorization(alpha['token']), trust_env=False) as http:
                async with streamable_http_client(f'http://127.0.0.1:{port}/mcp', http_client=http) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        listed = await session.call_tool('list_libraries', {})
                        assert listed.structuredContent['libraries'][0]['availability']['status'] == 'missing'
                        result = await session.call_tool('read_file', {'path': 'README.md'})
                        assert result.isError and 'library_unavailable' in result.content[0].text
                        directory.mkdir()
                        (directory / 'README.md').write_text('recovered', encoding='utf-8')
                        result = await session.call_tool('read_file', {'path': 'README.md'})
                        assert not result.isError
                        async with httpx.AsyncClient(trust_env=False) as admin_http:
                            url = f'http://127.0.0.1:{port}/admin/api/keys/' + alpha['key']['id'] + '/grants'
                            response = await admin_http.put(url, headers=authorization(ADMIN), json={'library_ids':['beta']})
                            assert response.status_code == 200
                            denied = await session.call_tool('read_file', {'library_id':'alpha','path':'README.md'})
                            assert denied.isError
                            listed = await session.call_tool('list_libraries', {})
                            assert [row['id'] for row in listed.structuredContent['libraries']] == ['beta']
                            allowed = await session.call_tool('read_file', {'path':'README.md'})
                            assert not allowed.isError and allowed.structuredContent['library_id'] == 'beta'
        asyncio.run(check_outage())
        app.state.registry.revoke_key(alpha['key']['id'])
        with httpx.Client(trust_env=False) as http:
            assert http.post(f'http://127.0.0.1:{port}/mcp', headers=authorization(alpha['token']), json={}).status_code == 401
    finally:
        server.should_exit=True;thread.join(10);sock.close()
    assert not thread.is_alive()
