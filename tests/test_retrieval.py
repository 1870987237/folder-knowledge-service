import asyncio
import json
from pathlib import Path
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from folder_kb.plugin_package import package_plugin
from folder_kb.retrieval import Retrieval, chunks
from folder_kb.server import create_app
from tests.test_template_management import env, auth


def setup(app, client, settings, tmp_path):
    root = Path(settings.root)
    (root / 'alpha/guide.md').write_text('# 模型配置\n设置服务地址和模型名称。\n## 访问密钥\n管理员生成专用密钥，不共享给其他组织。\n', encoding='utf-8')
    (root / 'alpha/other.txt').write_text('Deployment rollback preserves the previous release.', encoding='utf-8')
    (root / 'alpha/.hidden.md').write_text('hidden-secret', encoding='utf-8')
    (root / 'alpha/archive').mkdir()
    (root / 'alpha/archive/old.md').write_text('outdated-secret', encoding='utf-8')
    (root / 'beta/guide.md').write_text('# 访问密钥\nBeta private data secret-beta', encoding='utf-8')
    package = package_plugin(Path('examples/plugins/simple-rag/package'),
                             Path('examples/plugins/simple-rag/definition.json'), tmp_path / 'upload')
    manifest = json.loads(Path(package['manifest']).read_text(encoding='utf-8'))
    response = client.post('/admin/api/plugins', headers=auth(), json=manifest)
    assert response.status_code == 201, response.text
    assert response.json()['capabilities']['indexing']['integration'] == 'available'
    assert client.put('/admin/api/plugins/simple-rag/versions/1.0.0/package', headers=auth(),
                      content=Path(package['package']).read_bytes()).status_code == 200
    for lid in ('alpha', 'beta'):
        assert client.put(f'/admin/api/libraries/{lid}/plugin', headers=auth(), json={
            'plugin_id': 'simple-rag', 'plugin_version': '1.0.0'}).status_code == 200
    return Retrieval(app.state.registry)


def test_rag_evidence_isolation_lifecycle(env, tmp_path):
    app, c, key, settings = env
    service = setup(app, c, settings, tmp_path)
    assert service.state('alpha')['status'] == 'missing'
    assert c.get('/api/v1/search', headers=auth(key['token']), params={'library_id':'alpha','query':'密钥'}).json()['error'] == 'index_missing'
    for lid in ('alpha', 'beta'):
        assert c.post(f'/admin/api/libraries/{lid}/index/build', headers=auth()).status_code == 200
    assert service.state('alpha')['files'] == 2
    info = c.get('/api/v1/info', headers=auth(key['token']), params={'library_id':'alpha'}).json()
    assert info['index']['status'] == 'ready'
    assert info['index']['engine'] == 'sqlite-fts5-bm25-v1'
    result = c.get('/api/v1/search', headers=auth(key['token']), params={'library_id':'alpha','query':'如何生成访问密钥'}).json()
    assert result['results'][0]['path'] == 'guide.md'
    assert result['results'][0]['start_line'] == 3
    assert result['results'][0]['end_line'] == 4
    assert '管理员生成' in result['results'][0]['text']
    assert 'secret-beta' not in json.dumps(result)
    assert c.get('/api/v1/search', headers=auth(key['token']), params={'library_id':'beta','query':'密钥'}).status_code == 403
    assert c.post('/admin/api/libraries/alpha/index/build', headers=auth(key['token'])).status_code == 401
    assert not service.search('alpha', 'outdated hidden secret')['results']
    assert service.search('alpha', 'rollback')['results'][0]['path'] == 'other.txt'
    assert service.search('alpha', 'unfindable')['results'] == []
    # Both libraries can be addressed by one key without merging their indexes.
    shared = app.state.registry.create_key('shared', ['alpha', 'beta'], None)
    assert 'secret-beta' in c.get('/api/v1/search', headers=auth(shared['token']), params={'library_id':'beta','query':'密钥'}).text
    assert c.get('/api/v1/search', headers=auth(key['token']), params={'library_id':'alpha','query':'" OR * NOT (','limit':11}).status_code == 422
    from folder_kb.errors import KnowledgeError
    with pytest.raises(KnowledgeError): service.search('alpha', '***')
    # Old file tools coexist with search.
    assert c.get('/api/v1/grep', headers=auth(key['token']), params={'library_id':'alpha','query':'密钥'}).json()['matches']
    app.state.registry.revoke_key(key['key']['id'])
    assert c.get('/api/v1/search', headers=auth(key['token']), params={'library_id':'alpha','query':'密钥'}).status_code == 401


@pytest.mark.parametrize('change', ['edit', 'delete', 'add', 'remount', 'unbind'])
def test_stale_sources_are_not_returned(env, tmp_path, change):
    app, c, key, settings = env
    service = setup(app, c, settings, tmp_path)
    service.build('alpha')
    root = Path(settings.root)
    if change == 'edit': (root / 'alpha/guide.md').write_text('replacement', encoding='utf-8')
    elif change == 'delete': (root / 'alpha/guide.md').unlink()
    elif change == 'add': (root / 'alpha/new.md').write_text('new', encoding='utf-8')
    elif change == 'remount': app.state.registry.save_library('alpha','alpha','beta',True)
    else: c.delete('/admin/api/libraries/alpha/plugin', headers=auth())
    result = c.get('/api/v1/search', headers=auth(key['token']), params={'library_id':'alpha','query':'密钥'})
    assert result.status_code == (403 if change == 'unbind' else 409), result.text
    assert result.json()['error'] in ('index_stale','capability_forbidden')
    if change != 'unbind':
        assert service.state('alpha')['status'] == 'stale'
        assert service.build('alpha')['status'] == 'ready'
        assert service.state('alpha')['status'] == 'ready'


def test_failed_rebuild_preserves_index(env, tmp_path, monkeypatch):
    app, c, _, settings = env
    service = setup(app, c, settings, tmp_path)
    service.build('alpha')
    original = service.path('alpha').read_bytes()
    import folder_kb.retrieval as module
    monkeypatch.setattr(module, 'MAX_BYTES', 10)
    r = c.post('/admin/api/libraries/alpha/index/build', headers=auth())
    assert r.status_code == 413
    assert service.path('alpha').read_bytes() == original
    assert not list(service.directory.glob('*.tmp'))


def test_chunk_citations_and_long_lines():
    text = '# Heading\n' + 'x' * 4000 + '\n## Another\nend\n'
    parts = list(chunks(text, 'markdown_sections'))
    assert ''.join(p[3] for p in parts) == text
    assert all(len(p[3]) <= 1200 for p in parts)
    assert parts[-1][0:2] == (3,4)


def test_legacy_directory_filter_and_truncation(env, tmp_path):
    app, c, _, settings = env
    service = setup(app, c, settings, tmp_path)
    root = Path(settings.root) / 'alpha'
    (root / 'docs').mkdir()
    for n in range(3):
        (root / f'docs/{n}.md').write_text('rollback example', encoding='utf-8')
    service.build('alpha')
    result = service.search('alpha', 'rollback', limit=1, path='docs')
    assert result['scope']['path'] == 'docs' and result['has_more']
    assert all(hit['path'].startswith('docs/') for hit in result['results'])
    assert service.search('alpha', 'rollback')['scope']['path'] == '.'


def test_failed_install_and_missing_directory(env, tmp_path, monkeypatch):
    app, c, key, settings = env
    service = setup(app, c, settings, tmp_path)
    service.build('alpha')
    original = service.path('alpha').read_bytes()
    import folder_kb.retrieval as module
    def denied(*args): raise PermissionError('simulated busy storage')
    monkeypatch.setattr(module.os, 'replace', denied)
    assert c.post('/admin/api/libraries/alpha/index/build', headers=auth()).status_code == 503
    assert service.path('alpha').read_bytes() == original
    assert not list(service.directory.glob('*.tmp'))
    (Path(settings.root) / 'alpha').rename(Path(settings.root) / 'gone')
    result = c.get('/api/v1/search', headers=auth(key['token']), params={'library_id':'alpha','query':'密钥'})
    assert result.json()['error'] == 'library_unavailable'


@pytest.mark.parametrize('backend', ['legacy', 'program'])
def test_real_mcp_search(env, tmp_path, monkeypatch, backend):
    app, c, key, settings = env
    if backend == 'program':
        from tests.test_search_plugin import install_program
        install_program(env, tmp_path, monkeypatch).build('alpha')
    else:
        setup(app, c, settings, tmp_path).build('alpha')
    sock = socket.socket(); sock.bind(('127.0.0.1',0)); port = sock.getsockname()[1]
    # The fixture only trusts testserver; explicitly permit our isolated loopback server.
    from dataclasses import replace
    server = uvicorn.Server(uvicorn.Config(create_app(replace(settings, allowed_hosts=('127.0.0.1:*',))), log_level='error', access_log=False))
    thread = threading.Thread(target=server.run, kwargs={'sockets':[sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic()+10
        while not server.started and time.monotonic()<deadline: time.sleep(.02)
        assert server.started
        async def check():
            async with httpx.AsyncClient(headers=auth(key['token']), trust_env=False) as http:
                async with streamable_http_client(f'http://127.0.0.1:{port}/mcp', http_client=http) as (read,write,_):
                    async with ClientSession(read,write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        assert {'search','grep','read_file'} <= {t.name for t in tools.tools}
                        assert 'path' in next(t for t in tools.tools if t.name == 'search').inputSchema['properties']
                        result = await session.call_tool('search', {'library_id':'alpha','query':'Apple' if backend == 'program' else '访问密钥',
                                                                  'path': 'docs' if backend == 'program' else '.'})
                        assert not result.isError
                        assert result.structuredContent['results'][0]['path'] == ('docs/a.md' if backend == 'program' else 'guide.md')
                        denied = await session.call_tool('search', {'library_id':'beta','query':'密钥'})
                        assert denied.isError
        asyncio.run(check())
    finally:
        server.should_exit=True; thread.join(10); sock.close()
    assert not thread.is_alive()
