import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import socket
import threading
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import pytest
import uvicorn

from folder_kb.capabilities import Capabilities, Policy
from folder_kb.errors import KnowledgeError
from folder_kb.file_writes import FileWrite, FileWrites
from folder_kb.server import create_app
from tests.test_collaboration import env, account, library, headers, ADMIN
from tests.test_template_management import env as mounted_env, auth
from tests.test_retrieval import setup


def native(c, registry):
    uid, user = account(c, 'owner')
    lid = library(c, user)
    key = c.post('/portal/api/keys', headers=headers(user), json={'name': 'memory agent', 'library_ids': [lid]}).json()
    settings = f'/portal/api/libraries/{lid}/agent-settings'
    assert c.put(settings, headers=headers(user), json={'purpose': 'Private memory', 'direct_enabled': True}).status_code == 200
    service = Capabilities(registry)
    service.policy(key['id'], Policy(permissions=[{'library_id': lid, 'grep': False, 'write_policy': 'direct'}], instructions='owner-only-guidance'))
    return uid, user, lid, key


def test_policy_atomic_cas_legacy_and_owner_boundaries(env):
    c, r = env
    _, owner, lid, key = native(c, r)
    _, stranger = account(c, 'stranger')
    url = f'/portal/api/keys/{key["id"]}/policy'
    assert c.get(url, headers=headers(stranger)).status_code == 403
    assert c.put(f'/portal/api/libraries/{lid}/agent-settings', headers=headers(stranger), json={'direct_enabled': True}).status_code == 403
    result = c.get(url, headers=headers(owner)).json()
    value = {'permissions': [{'library_id': lid, 'grep': True, 'search': False, 'write_policy': 'submit'}], 'instructions': 'new instructions', 'expected_revision': result['policy_revision']}
    assert c.put(url, headers=headers(owner), json=value).status_code == 200
    assert c.put(url, headers=headers(owner), json=value).status_code == 409
    bad = dict(value, expected_revision=None, permissions=[{'library_id': 'not-a-library'}])
    assert c.put(url, headers=headers(owner), json=bad).status_code == 400
    assert c.get(url, headers=headers(owner)).json()['instructions'] == 'new instructions'
    assert c.put(url, headers=headers(owner), json={'permissions':[{'library_id':lid,'read':False,'grep':True}]}).status_code == 422
    # Legacy grants cannot leave hidden direct rights active.
    assert c.put(f'/admin/api/keys/{key["id"]}/permissions', headers=headers(ADMIN), json={'library_ids': [lid], 'submit_library_ids': []}).status_code == 200
    assert c.get(url, headers=headers(owner)).json()['permissions'][0]['write_policy'] == 'readonly'


def test_mixed_libraries_grep_search_submission_and_missing_index(mounted_env, tmp_path):
    app, c, _, settings = mounted_env
    retrieval = setup(app, c, settings, tmp_path)
    r = app.state.registry
    key = r.create_key('mixed', ['alpha','beta'],None)
    Capabilities(r).policy(key['key']['id'], Policy(permissions=[
        {'library_id':'alpha','search':True,'write_policy':'submit'},
        {'library_id':'beta','grep':True}]))
    agent=auth(key['token'])
    names=Capabilities(r).discovery(key['token'])[0]
    assert {'search','grep','submit_material','get_submission'} <= names
    assert c.get('/api/v1/search',headers=agent,params={'library_id':'alpha','query':'密钥'}).json()['error']=='index_missing'
    retrieval.build('alpha')
    assert c.get('/api/v1/search',headers=agent,params={'library_id':'alpha','query':'密钥'}).status_code==200
    assert c.get('/api/v1/search',headers=agent,params={'library_id':'beta','query':'密钥'}).status_code==403
    assert c.get('/api/v1/grep',headers=agent,params={'library_id':'alpha','query':'密钥'}).status_code==403
    assert c.get('/api/v1/grep',headers=agent,params={'library_id':'beta','query':'密钥'}).status_code==200
    assert c.get('/api/v1/read',headers=agent,params={'path':'guide.md'}).json()['error']=='library_required'
    material={'library_id':'alpha','submission_id':'new-1','content':{'text':'new knowledge'}}
    assert c.post('/api/v1/inbox/submissions',headers=agent,json=material).status_code==202
    assert c.post('/api/v1/inbox/submissions',headers=agent,json=dict(material,library_id='beta')).status_code==403
    listed=c.get('/api/v1/libraries',headers=agent).json()['libraries']
    assert listed[0]['capabilities']['search'] and not listed[0]['capabilities']['grep']
    assert not listed[1]['capabilities']['search'] and listed[1]['capabilities']['grep']


def test_direct_owner_requires_publisher_and_mounted_libraries_cannot_enable(env):
    from tests.test_collaboration import organization
    c,r=env
    uid,user=account(c,'org-admin')
    member,member_token=account(c,'member')
    oid=organization(c,'org-admin')
    lid=library(c,user,oid)
    with r.store.transaction(write=True) as db:
        db.execute('INSERT INTO organization_members VALUES (?,?,?)',(oid,member,'member'))
        db.execute('INSERT INTO library_members VALUES (?,?,?)',(lid,member,'publisher'))
    c.put(f'/portal/api/libraries/{lid}/agent-settings',headers=headers(user),json={'direct_enabled':True})
    key=c.post('/portal/api/keys',headers=headers(member_token),json={'name':'publisher','library_ids':[lid]}).json()
    policy={'permissions':[{'library_id':lid,'write_policy':'direct'}]}
    assert c.put(f'/portal/api/keys/{key["id"]}/policy',headers=headers(member_token),json=policy).status_code==200
    with r.store.transaction(write=True) as db:
        db.execute("UPDATE library_members SET role='editor' WHERE library_id=? AND user_id=?",(lid,member))
    assert 'write_file' not in Capabilities(r).discovery(key['token'])[0]
    assert c.put(f'/portal/api/keys/{key["id"]}/policy',headers=headers(member_token),json=policy).status_code==403
    root=r.pool.root/'mounted';root.mkdir()
    r.save_library('mounted','Mounted','mounted',True,create=True)
    assert c.put('/admin/api/libraries/mounted/agent-settings',headers=headers(ADMIN),json={'direct_enabled':True}).status_code==409


def test_direct_write_version_idempotency_snapshot_and_permissions(env):
    c, r = env
    uid, user, lid, key = native(c, r)
    agent = headers(key['token'])
    read = c.get('/api/v1/read', headers=agent, params={'path':'README.md'}).json()
    assert c.get('/api/v1/grep', headers=agent, params={'query':'test'}).status_code == 403
    _, before = r.resolve(key['token'], lid)
    body = {'path':'README.md','content':'# New memory\nPrefer Chinese answers.\n','operation':'replace','expected_version':read['version'],'request_id':'replace-1'}
    result = c.post('/api/v1/write', headers=agent, json=body)
    assert result.status_code == 200, result.text
    assert c.post('/api/v1/write', headers=agent, json=body).json()['duplicate']
    assert c.post('/api/v1/write', headers=agent, json=dict(body,content='different')).status_code == 409
    assert c.post('/api/v1/write', headers=agent, json=dict(body,request_id='stale')).json()['error'] == 'version_changed'
    assert r.folder_for_path(before['path']).read_file('README.md')['version'] == read['version']
    assert c.get('/api/v1/read', headers=agent, params={'path':'README.md'}).json()['version'] == result.json()['version']
    create = {'path':'new.md','content':'new','operation':'create','request_id':'create-1'}
    assert c.post('/api/v1/write', headers=agent, json=create).status_code == 200
    assert c.post('/api/v1/write', headers=agent, json=dict(create, request_id='exists')).status_code == 409
    for path in ('../outside.md', '/absolute.md', '.hidden.md', 'bad.json', 'new-folder/new.md'):
        assert c.post('/api/v1/write', headers=agent, json=dict(create,path=path,request_id=path.replace('/','-'))).status_code != 200
    assert c.post('/api/v1/write', headers=agent, json=dict(create, content='中' * 100000)).status_code == 422
    assert c.put(f'/portal/api/libraries/{lid}/agent-settings', headers=headers(user), json={'direct_enabled':False}).status_code == 200
    # Retries also check the current permission, even when a receipt exists.
    assert c.post('/api/v1/write', headers=agent, json=body).status_code == 403
    assert 'write_file' not in Capabilities(r).discovery(key['token'])[0]
    with r.store.transaction() as db:
        assert db.execute('SELECT count(*) FROM file_write_receipts').fetchone()[0] == 2


def test_concurrent_writes_one_version_wins_and_rollback(env, monkeypatch):
    c, r = env
    _, _, lid, key = native(c, r)
    version = c.get('/api/v1/read', headers=headers(key['token']), params={'path':'README.md'}).json()['version']
    def write(i):
        try:
            return FileWrites(r).write(key['token'], FileWrite(path='README.md',content=f'writer {i}',operation='replace',expected_version=version,request_id=f'r{i}'))
        except KnowledgeError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, (1,2)))
    assert sum(isinstance(x,dict) for x in results) == 1
    assert 'version_changed' in results
    _, row = r.resolve(key['token'], lid)
    import folder_kb.file_writes as module
    def fail_copy(*args, **kwargs): raise OSError('storage full')
    monkeypatch.setattr(module,'copy_workspace',fail_copy)
    response = c.post('/api/v1/write', headers=headers(key['token']), json={'path':'x.md','content':'x','operation':'create','request_id':'fail'})
    assert response.status_code == 503
    assert r.resolve(key['token'],lid)[1]['path'] == row['path']


def test_effective_owner_role_and_library_changes(env):
    c, r = env
    uid, _, lid, key = native(c, r)
    # No owner privilege escalation through a key; role is checked at each call.
    with r.store.transaction(write=True) as db:
        db.execute('UPDATE library_ownership SET native=0 WHERE library_id=?',(lid,))
    assert 'write_file' not in Capabilities(r).discovery(key['token'])[0]
    with r.store.transaction(write=True) as db:
        db.execute('UPDATE users SET status=? WHERE id=?',('disabled',uid))
    assert c.get('/api/v1/libraries',headers=headers(key['token'])).status_code == 401


def test_real_mcp_scoped_instructions_tools_and_live_changes(env):
    c, r = env
    _, _, lid, key = native(c, r)
    uid2, user2 = account(c, 'other')
    lid2 = library(c,user2)
    other = r.create_key('other agent',[lid2],None)
    Capabilities(r).policy(other['key']['id'], Policy(permissions=[{'library_id':lid2,'grep':True}], instructions='other-only-guidance'))
    settings = c.app.user_middleware[0].kwargs['settings']
    sock = socket.socket(); sock.bind(('127.0.0.1',0)); port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(replace(settings,allowed_hosts=('127.0.0.1:*',))),log_level='error',access_log=False))
    thread = threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True); thread.start()
    try:
        deadline=time.monotonic()+10
        while not server.started and time.monotonic()<deadline: time.sleep(.02)
        assert server.started
        async def use(token,kid,own,foreign,write):
            async with httpx.AsyncClient(headers=headers(token),trust_env=False) as http:
                async with streamable_http_client(f'http://127.0.0.1:{port}/mcp',http_client=http) as (read,output,_):
                    async with ClientSession(read,output) as session:
                        init = await session.initialize()
                        assert own in init.instructions and foreign not in init.instructions
                        tool_list = (await session.list_tools()).tools
                        names = {t.name for t in tool_list}
                        assert ('write_file' in names) == write
                        assert ('apply_changes' in names) == write
                        assert ('file_manifest' in names) == write
                        assert ('grep' in names) != write
                        if write:
                            response = await session.call_tool('write_file',{'path':'memory.md','content':'memory','operation':'create','request_id':'mcp-1'})
                            assert not response.isError, response
                            schema = next(t for t in tool_list if t.name == 'apply_changes').inputSchema
                            assert schema['properties']['request_id']['pattern'] == r'^[A-Za-z0-9][A-Za-z0-9._:-]*$'
                            response = await session.call_tool('file_manifest', {})
                            assert not response.isError, response
                            import json
                            manifest = json.loads(response.content[0].text)
                            response = await session.call_tool('apply_changes', {
                                'request_id': 'mcp-batch', 'expected_library_version': manifest['library_version'],
                                'changes': [{'operation': 'mkdir', 'path': 'notes'},
                                            {'operation': 'create', 'path': 'notes/a.md', 'content': 'note'},
                                            {'operation': 'delete', 'path': 'memory.md'}]})
                            assert not response.isError, response
                            Capabilities(r).policy(kid, Policy(permissions=[]))
                            assert {t.name for t in (await session.list_tools()).tools} == {'list_libraries'}
                            denied = await session.call_tool('write_file',{'library_id':lid,'path':'memory2.md','content':'no','operation':'create','request_id':'mcp-2'})
                            assert denied.isError
        async def check():
            await asyncio.gather(use(key['token'],key['id'],'owner-only-guidance','other-only-guidance',True),
                                 use(other['token'],other['key']['id'],'other-only-guidance','owner-only-guidance',False))
        asyncio.run(check())
    finally:
        server.should_exit=True; thread.join(10); sock.close()
    assert not thread.is_alive()
