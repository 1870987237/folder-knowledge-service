import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import socket
import sqlite3
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from folder_kb.backup import backup
from folder_kb.errors import KnowledgeError
from folder_kb.inbox import Material
from folder_kb.server import Settings, create_app

ADMIN = 'inbox-administrator-token-for-tests-only'
URL = '/api/v1/inbox/submissions'


def auth(token):
    return {'Authorization': 'Bearer ' + token}


@pytest.fixture
def env(tmp_path):
    root = tmp_path / 'knowledge'
    (root / 'alpha').mkdir(parents=True)
    (root / 'beta').mkdir()
    (root / 'alpha/README.md').write_text('published knowledge', encoding='utf-8')
    settings = Settings(str(root), admin_token=ADMIN, state_dir=str(tmp_path / 'state'),
                        allowed_hosts=('testserver', '127.0.0.1', '127.0.0.1:*'))
    app = create_app(settings)
    registry = app.state.registry
    for lid in ('alpha', 'beta'):
        registry.save_library(lid, lid, lid, True, create=True)
    key = registry.create_key('sender', ['alpha'], None)
    with TestClient(app) as client:
        yield app, client, key, settings


def envelope(**changes):
    return dict({'schema_version': '1.0', 'submission_id': 'feedback-001', 'library_id': 'alpha',
                 'kind': 'correction', 'title': 'Corrections', 'content': {'format': 'markdown', 'text': '  Original evidence\n'},
                 'source': {'reference': 'session:123', 'description': 'Reported by user'},
                 'context': {'scenario': 'A specific usage condition'}}, **changes)


def grant(app, key):
    app.state.inbox.grants(key['key']['id'], ['alpha'])


def test_default_read_only_grants_and_ownership(env):
    app, client, key, _ = env
    assert client.post(URL, headers=auth(key['token']), json=envelope()).status_code == 403
    assert client.post(URL, json=envelope()).status_code == 401
    endpoint = '/admin/api/keys/' + key['key']['id'] + '/inbox-grants'
    assert client.put(endpoint, headers=auth(key['token']), json={'library_ids': ['alpha']}).status_code == 401
    assert client.put(endpoint, headers=auth(ADMIN), json={'library_ids': ['beta']}).status_code == 400
    assert client.put(endpoint, headers=auth(ADMIN), json={'library_ids': ['alpha']}).status_code == 200
    assert client.get(endpoint, headers=auth(ADMIN)).json()['submit_library_ids'] == ['alpha']
    result = client.post(URL, headers=auth(key['token']), json=envelope())
    assert result.status_code == 202
    item = result.json()
    assert item['status'] == 'pending' and item['knowledge_updated'] is False
    assert item['submitted_by']['id'] == key['key']['id']
    assert client.post(URL, headers=auth(key['token']), json=envelope(library_id='beta')).status_code == 403
    second = app.state.registry.create_key('another', ['alpha'], None)
    grant(app, second)
    assert client.get(URL + '/' + item['id'], headers=auth(second['token'])).status_code == 404
    assert client.get('/admin/api/inbox', headers=auth(key['token'])).status_code == 401
    receipt = client.get(URL + '/' + item['id'], headers=auth(key['token'])).json()
    assert receipt['material']['content']['text'] == '  Original evidence\n'
    assert client.get('/api/v1/grep', headers=auth(key['token']), params={'query': 'Original evidence'}).json()['matches'] == []
    # Read-grant edits retain unchanged submission grants, but removing and readding requires fresh opt-in.
    app.state.registry.update_key_grants(key['key']['id'], ['alpha', 'beta'])
    assert app.state.inbox.grants(key['key']['id'])['submit_library_ids'] == ['alpha']
    app.state.registry.update_key_grants(key['key']['id'], ['beta'])
    app.state.registry.update_key_grants(key['key']['id'], ['alpha'])
    assert client.post(URL, headers=auth(key['token']), json=envelope()).status_code == 403
    grant(app, key)
    app.state.registry.revoke_key(key['key']['id'])
    assert client.get(URL + '/' + item['id'], headers=auth(key['token'])).status_code == 401


def test_idempotency_concurrency_restart_backup_and_outage(env, tmp_path):
    app, client, key, settings = env
    grant(app, key)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: app.state.inbox.submit(key['token'], Material(**envelope())), range(12)))
    assert len({r['id'] for r in results}) == 1
    assert sum(not r['duplicate'] for r in results) == 1
    assert client.post(URL, headers=auth(key['token']), json=envelope(title='changed')).status_code == 409
    other_id = envelope(submission_id='feedback-002')
    assert client.post(URL, headers=auth(key['token']), json=other_id).status_code == 202
    page = client.get('/admin/api/inbox?limit=1', headers=auth(ADMIN)).json()
    next_page = client.get('/admin/api/inbox', headers=auth(ADMIN), params={'after': page['next_after']}).json()
    assert len(next_page['items']) == 1 and next_page['items'][0]['id'] != page['items'][0]['id']
    reopened = create_app(settings)
    assert reopened.state.inbox.get(results[0]['id'], key['token'])['material'] == Material(**envelope()).model_dump()
    target = backup(settings.state_dir, tmp_path / 'backup.sqlite3')
    with sqlite3.connect(target) as db:
        assert db.execute('SELECT count(*) FROM inbox_items').fetchone()[0] == 2
        assert db.execute('PRAGMA user_version').fetchone()[0] == 3
    # Inbox references a stable library ID and still accepts reports during directory outages.
    path = app.state.registry.pool.root / 'alpha'
    (path / 'README.md').unlink()
    path.rmdir()
    assert client.post(URL, headers=auth(key['token']), json=envelope(submission_id='outage')).status_code == 202
    app.state.registry.save_library('alpha', 'alpha', 'alpha', False)
    assert client.post(URL, headers=auth(key['token']), json=envelope()).status_code == 403


def test_claim_lease_retry_completion_and_audit(env):
    app, client, key, _ = env
    grant(app, key)
    item = client.post(URL, headers=auth(key['token']), json=envelope()).json()
    base = '/admin/api/inbox/' + item['id']
    def attempt(_):
        try:
            return app.state.inbox.claim(item['id'], 30)
        except KnowledgeError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(attempt, range(4)))
    winners = [c for c in claims if isinstance(c, dict)]
    assert len(winners) == 1
    old_claim = winners[0]['claim_token']
    assert 'claim_token' not in client.get(base, headers=auth(ADMIN)).text
    with app.state.registry.store.transaction(write=True) as db:
        db.execute('UPDATE inbox_items SET lease_until=0 WHERE id=?', (item['id'],))
    claim = client.post(base + '/claim', headers=auth(ADMIN), json={}).json()
    assert claim['attempts'] == 2
    result = {'claim_token': old_claim, 'outcome': 'accepted', 'summary': 'Checked'}
    assert client.post(base + '/complete', headers=auth(ADMIN), json=result).status_code == 409
    assert client.post(base + '/release', headers=auth(ADMIN), json={'claim_token': claim['claim_token']}).json()['status'] == 'pending'
    claim = client.post(base + '/claim', headers=auth(ADMIN), json={}).json()
    result.update(claim_token=claim['claim_token'], references=['README.md'], version='commit:example')
    complete = client.post(base + '/complete', headers=auth(ADMIN), json=result)
    assert complete.status_code == 200 and complete.json()['status'] == 'completed'
    assert complete.json()['result']['references'] == ['README.md']
    assert client.post(base + '/claim', headers=auth(ADMIN), json={}).status_code == 409
    assert (app.state.registry.pool.root / 'alpha/README.md').read_text() == 'published knowledge'
    logs = json.dumps(app.state.registry.recent_events())
    assert 'Original evidence' not in logs and claim['claim_token'] not in logs and key['token'] not in logs


@pytest.mark.parametrize('patch', [
    {'schema_version': '2.0'}, {'content': {'text': ' '}}, {'content': {'text': '中' * 11000}},
    {'submitted_by': 'admin'}, {'attachments': ['/etc/passwd']}, {'context': {'a': 'x' * 1025}},
    {'submission_id': '../evil'}, {'kind': 'execute'}, {'kind': 'knowledge'}, {'kind': 'memory'},
])
def test_invalid_envelopes(env, patch):
    app, client, key, _ = env
    grant(app, key)
    assert client.post(URL, headers=auth(key['token']), json=envelope(**patch)).status_code == 422


def test_streamed_request_limit(env):
    app, client, key, _ = env
    grant(app, key)
    assert client.post(URL, headers=auth(key['token']), content=iter([b' ' * 40000, b' ' * 40000])).status_code == 413


def test_v2_migration_keeps_existing_read_keys(env):
    app, client, key, settings = env
    with app.state.registry.store.transaction(write=True) as db:
        db.execute('DROP TABLE inbox_items')
        db.execute('DROP TABLE inbox_grants')
        db.execute('PRAGMA user_version=2')
    new_app = create_app(settings)
    assert new_app.state.registry.authenticate(key['token'])['library_ids'] == ['alpha']
    assert new_app.state.inbox.grants(key['key']['id'])['submit_library_ids'] == []
    assert new_app.state.inbox.listing()['items'] == []


def test_real_mcp_material_contract(env):
    app, _, key, settings = env
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(settings), log_level='error', access_log=False))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline
            time.sleep(.02)
        async def run():
            async with httpx.AsyncClient(headers=auth(key['token']), trust_env=False) as http:
                async with streamable_http_client(f'http://127.0.0.1:{port}/mcp', http_client=http) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = {t.name: t for t in (await session.list_tools()).tools}
                        assert tools['submit_material'].annotations.readOnlyHint is False
                        assert tools['get_submission'].annotations.readOnlyHint is True
                        denied = await session.call_tool('submit_material', {'material': envelope()})
                        assert denied.isError
                        grant(app, key)
                        result = await session.call_tool('submit_material', {'material': envelope()})
                        assert not result.isError, result
                        data = result.structuredContent
                        receipt = await session.call_tool('get_submission', {'item_id': data['id']})
                        assert not receipt.isError
                        assert receipt.structuredContent['material']['content']['text'] == envelope()['content']['text']
                        retry = await session.call_tool('submit_material', {'material': envelope()})
                        assert retry.structuredContent['duplicate'] is True
                        invalid = await session.call_tool('submit_material', {'material': envelope(schema_version='2.0')})
                        assert invalid.isError
        asyncio.run(run())
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
