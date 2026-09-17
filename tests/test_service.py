import asyncio
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import socket
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
import uvicorn

from folder_kb.core import KnowledgeError, KnowledgeFolder, Limits
from folder_kb.server import Settings, create_app


TOKEN = 'test-only-token-not-for-deployment-12345'


@pytest.fixture
def root(tmp_path):
    base = tmp_path / 'knowledge'
    base.mkdir()
    (base / 'guides').mkdir()
    (base / 'README.md').write_text('# Index\nSee guides.\n', encoding='utf-8')
    (base / 'guides' / '经验.md').write_text('场景\nGuide version=1.8\n边界\nGuide revision=2.0\n', encoding='utf-8')
    (base / '.env').write_text('SECRET=not-readable', encoding='utf-8')
    (base / '.git').mkdir()
    (base / '.git' / 'secret.md').write_text('SECRET', encoding='utf-8')
    (base / 'image.png').write_bytes(b'\x89PNG\x00')
    return base


@pytest.fixture
def folder(root):
    return KnowledgeFolder(root)


def test_list_glob_and_pagination(folder):
    first = folder.list_directory(limit=1)
    second = folder.list_directory(offset=first['next_offset'], limit=1)
    assert first['entries'] != second['entries']
    assert first['partial'] is False
    files = folder.glob('**/*.md')['files']
    assert [r['path'] for r in files] == ['README.md', 'guides/经验.md']
    assert [r['path'] for r in folder.glob('*.md')['files']] == ['README.md']
    assert folder.glob('*.md', path='guides')['files'][0]['path'] == 'guides/经验.md'


def test_read_lines_bom_and_live_update(root, folder):
    p = root / 'new.md'
    raw = b'\xef\xbb\xbf' + '第一行\r\n第二行\r\n'.encode()
    p.write_bytes(raw)
    result = folder.read_file('new.md', 2, 2)
    assert result['lines'] == [{'line': 2, 'text': '第二行'}]
    assert result['sha256'] == hashlib.sha256(raw).hexdigest()
    p.write_text('changed', encoding='utf-8')
    with pytest.raises(KnowledgeError, match='hash changed'):
        folder.read_file('new.md', expected_sha256=result['sha256'])
    assert folder.read_file('new.md')['lines'][0]['text'] == 'changed'


@pytest.mark.parametrize('path', ['../secret.md', '/etc/passwd', 'C:/secret.md', 'guides/../../secret.md',
                                  'guides\\经验.md', '.env', '.git/secret.md', 'README.md:stream', 'bad\x00.md'])
def test_path_boundary(folder, path):
    with pytest.raises(KnowledgeError):
        folder.read_file(path)


def test_link_and_directory_symlink_rejected(root, folder, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret.md').write_text('secret', encoding='utf-8')
    try:
        (root / 'linked.md').symlink_to(outside / 'secret.md')
        (root / 'linked-dir').symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('OS does not allow this account to create symlinks')
    with pytest.raises(KnowledgeError):
        folder.read_file('linked.md')
    with pytest.raises(KnowledgeError):
        folder.read_file('linked-dir/secret.md')
    assert not folder.grep('secret')['matches']


def test_hardlink_rejected(root, folder, tmp_path):
    outside = tmp_path / 'outside.md'
    outside.write_text('secret', encoding='utf-8')
    os.link(outside, root / 'linked.md')
    with pytest.raises(KnowledgeError):
        folder.read_file('linked.md')
    assert not folder.grep('secret')['matches']


def test_open_handle_escape_is_rejected(root, folder, tmp_path, monkeypatch):
    outside = tmp_path / 'outside.md'
    outside.write_text('secret', encoding='utf-8')
    original = os.open

    def raced_open(path, flags):
        return original(outside, flags)

    monkeypatch.setattr(os, 'open', raced_open)
    with pytest.raises(KnowledgeError, match='outside'):
        folder.read_file('README.md')


def test_grep_literal_regex_context_and_paging(folder):
    result = folder.grep('guide', path='guides', ignore_case=True, limit=1)
    assert result['matches'][0]['line'] == 2
    assert result['matches'][0]['path'] == 'guides/经验.md'
    assert result['next_offset'] == 1
    assert folder.grep('guide', path='guides', ignore_case=True, offset=1)['matches'][0]['line'] == 4
    assert len(folder.grep(r'version=\d+\.\d+', regex_mode=True)['matches']) == 1
    assert not folder.grep(r'version=\d+')['matches']
    assert not folder.grep('SECRET')['matches']
    with pytest.raises(KnowledgeError, match='regular expression'):
        folder.grep('(', regex_mode=True)


def test_regex_timeout_is_partial(root, folder):
    (root / 'evil.md').write_text('a' * 100000 + '!', encoding='utf-8')
    started = time.monotonic()
    result = folder.grep('(a+)+$', regex_mode=True)
    assert result['partial'] is True
    assert any(r['reason'] == 'search_timeout' for r in result['skipped'])
    assert time.monotonic() - started < 3


def test_binary_size_and_scan_limits(root):
    folder = KnowledgeFolder(root, limits=Limits(file_bytes=100))
    (root / 'big.md').write_bytes(b'x' * 101)
    (root / 'binary.md').write_bytes(b'\x00\xff')
    with pytest.raises(KnowledgeError):
        folder.read_file('big.md')
    result = folder.grep('missing')
    assert result['partial'] and result['skipped_count'] == 2
    bounded = KnowledgeFolder(root, limits=Limits(entries=1))
    assert bounded.glob('**')['partial']


def test_response_limit_does_not_silently_drop_lines(root):
    folder = KnowledgeFolder(root, limits=Limits(response_chars=12))
    (root / 'small.md').write_text('1234567890\nabcdefghij\n', encoding='utf-8')
    result = folder.read_file('small.md')
    assert result['truncated'] and result['next_start_line'] == 2
    (root / 'long.md').write_text('x' * 13, encoding='utf-8')
    with pytest.raises(KnowledgeError, match='line exceeds'):
        folder.read_file('long.md')


def test_rest_auth_guard_read_only_and_no_mutation(root):
    settings = Settings(str(root), token=TOKEN, allowed_hosts=('testserver',))
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    with TestClient(create_app(settings)) as client:
        assert client.get('/healthz').status_code == 200
        for path in ('/api/v1/info', '/api/v1/list', '/openapi.json', '/mcp'):
            assert client.get(path).status_code == 401
        client.headers['Authorization'] = 'Bearer ' + TOKEN
        assert client.get('/api/v1/info').json()['read_only']
        assert client.get('/api/v1/read', params={'path': 'guides/经验.md'}).json()['total_lines'] == 4
        assert client.get('/api/v1/read', params={'path': '../secret.md'}).status_code == 403
        assert client.get('/api/v1/grep', params={'query': 'Guide'}).json()['matches']
        assert client.get('/api/v1/info', headers={'Host': 'evil.example'}).status_code == 421
        assert client.get('/api/v1/info', headers={'Origin': 'https://evil.example'}).status_code == 403
        for method in ('post', 'put', 'patch', 'delete'):
            assert getattr(client, method)('/api/v1/read?path=README.md').status_code in (404, 405)
        schema = client.get('/openapi.json').json()
        assert all(set(v) <= {'get'} for v in schema['paths'].values())
    after = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert before == after


def test_mcp_over_real_http_matches_rest(root):
    settings = Settings(str(root), token=TOKEN)
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(settings), log_level='error', access_log=False))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                pytest.fail('HTTP test server did not start')
            time.sleep(0.02)

        async def run():
            base = f'http://127.0.0.1:{port}'
            headers = {'Authorization': 'Bearer ' + TOKEN}
            async with httpx.AsyncClient(base_url=base, headers=headers, trust_env=False) as rest:
                expected = (await rest.get('/api/v1/grep', params={'query': 'Guide'})).json()
                assert (await rest.post('/mcp', headers={'Authorization': 'Bearer invalid'}, json={})).status_code == 401
                too_big = await rest.post('/mcp', content=b'x' * 65537,
                                          headers={'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'})
                assert too_big.status_code == 413
            async with streamablehttp_client(base + '/mcp', headers=headers,
                    httpx_client_factory=lambda **kw: httpx.AsyncClient(**kw, trust_env=False)) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    assert {t.name for t in listed.tools} == {'knowledge_info', 'list_directory', 'glob', 'read_file', 'grep'}
                    assert all(t.annotations.readOnlyHint and not t.annotations.destructiveHint for t in listed.tools)
                    result = await session.call_tool('grep', {'query': 'Guide'})
                    assert not result.isError
                    assert result.structuredContent == expected
                    result = await session.call_tool('read_file', {'path': 'guides/经验.md', 'start_line': 2, 'end_line': 2})
                    assert result.structuredContent['lines'][0]['text'] == 'Guide version=1.8'
                    denied = await session.call_tool('read_file', {'path': '../secret.md'})
                    assert denied.isError
                    assert (await session.call_tool('write_file', {'path': 'x', 'text': 'x'})).isError
        asyncio.run(run())
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
    assert not thread.is_alive()


def test_invalid_settings(root):
    with pytest.raises(ValueError):
        Settings(str(root), token='short')
    with pytest.raises(ValueError):
        Settings(str(root), allowed_hosts=('*',))


def test_direct_asgi_runner_cannot_expose_unauthenticated_files(root):
    settings = Settings(str(root), allowed_hosts=('testserver',))
    with TestClient(create_app(settings), client=('10.0.0.2', 12345)) as client:
        assert client.get('/api/v1/info').status_code == 403
    with TestClient(create_app(settings), client=('127.0.0.1', 12345)) as client:
        assert client.get('/api/v1/info').status_code == 200
