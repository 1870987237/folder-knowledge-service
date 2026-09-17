import hashlib
from pathlib import Path
import tracemalloc

import pytest
from fastapi.testclient import TestClient

from folder_kb.core import KnowledgeError, KnowledgeFolder, Limits
from folder_kb.server import Settings, create_app


def collect(folder, path, **kwargs):
    page = folder.read_file(path, **kwargs)
    pages = [page]
    for _ in range(1000):
        if page['next_cursor'] is None:
            return pages
        cursor = page['next_cursor']
        page = folder.read_file(path, cursor=cursor)
        assert page['next_cursor'] != cursor
        pages.append(page)
    pytest.fail('Continuation did not terminate')


def search(folder, **kwargs):
    page = folder.grep(**kwargs)
    pages = [page]
    for _ in range(1000):
        if page['next_cursor'] is None:
            return pages
        cursor = page['next_cursor']
        page = folder.grep(**kwargs, cursor=cursor)
        assert page['next_cursor'] != cursor
        pages.append(page)
    pytest.fail('Search did not progress')


def test_large_file_first_page_is_bounded_and_does_not_hash(tmp_path, monkeypatch):
    file = tmp_path/'large.jsonl'
    with file.open('wb') as stream:
        for _ in range(4096):
            stream.write(b'{"question":"'+b'x'*4096+b'"}\n')
    folder = KnowledgeFolder(tmp_path)
    monkeypatch.setattr(folder, '_legacy_hash', lambda *args: pytest.fail('Unexpected full-file hash'))
    tracemalloc.start()
    try:
        page = folder.read_file('large.jsonl', end_line=2)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert page['file_bytes'] > 16*1024*1024
    assert page['scanned_bytes'] <= 65536 and page['sha256'] is None
    assert page['total_lines'] is None and page['next_cursor']
    assert peak < 2*1024*1024


def test_long_unicode_line_round_trip_with_mixed_newlines(tmp_path):
    lines = ['中🙂abc'*19000, 'second', '', 'final']
    (tmp_path/'long.jsonl').write_bytes(b'\xef\xbb\xbf'+('\r\n'.join(lines[:2])+'\r\r'+lines[3]).encode())
    folder = KnowledgeFolder(tmp_path, limits=Limits(response_chars=97, scan_bytes=4096))
    pages = collect(folder, 'long.jsonl')
    grouped = {}
    for page in pages:
        assert sum(len(r['text']) for r in page['lines']) <= 97
        for row in page['lines']:
            prior = grouped.get(row['line'], '')
            assert row['column'] == len(prior)+1
            grouped[row['line']] = prior+row['text']
    assert list(grouped.values()) == lines
    assert pages[-1]['total_lines'] == 4


@pytest.mark.parametrize('raw,expected', [(b'', []), (b'\xef\xbb\xbf', []), (b'\n', ['']),
                                         (b'a\r\nb\rc\n', ['a','b','c'])])
def test_empty_and_newlines(tmp_path, raw, expected):
    (tmp_path/'sample.txt').write_bytes(raw)
    page = KnowledgeFolder(tmp_path).read_file('sample.txt')
    assert [r['text'] for r in page['lines']] == expected
    assert page['total_lines'] == len(expected) and page['eof']


def test_deep_line_scan_continues_and_rejects_changed_version(tmp_path):
    file = tmp_path/'many.txt'
    file.write_text(('0123456789\n'*90)+'target\n', encoding='utf-8')
    folder = KnowledgeFolder(tmp_path, limits=Limits(scan_bytes=128))
    pages = collect(folder, 'many.txt', start_line=91, end_line=91)
    assert pages[0]['lines'] == [] and pages[0]['reason'] == 'scan_limit'
    assert ''.join(r['text'] for p in pages for r in p['lines']) == 'target'
    cursor = pages[0]['next_cursor']
    file.write_text('changed\n', encoding='utf-8')
    with pytest.raises(KnowledgeError, match='version changed'):
        folder.read_file('many.txt', cursor=cursor)


def test_cursor_tamper_scope_and_path_boundaries(tmp_path):
    (tmp_path/'a.txt').write_text('abc\ndef\n', encoding='utf-8')
    (tmp_path/'b.txt').write_text('other', encoding='utf-8')
    folder = KnowledgeFolder(tmp_path)
    cursor = folder.read_file('a.txt', end_line=1)['next_cursor']
    for token, path in ((cursor[:-5]+'wrong', 'a.txt'), (cursor,'b.txt'), (cursor,'../a.txt')):
        with pytest.raises(KnowledgeError):
            folder.read_file(path, cursor=token)


def test_literal_search_crosses_chunks_and_scan_pages_without_duplicates(tmp_path):
    (tmp_path/'data.jsonl').write_text('x'*63+'needle'+'y'*300+'needle\nnext needle\n', encoding='utf-8')
    folder = KnowledgeFolder(tmp_path, limits=Limits(scan_bytes=64))
    pages = search(folder, query='needle', context=0, limit=1)
    hits = [h for p in pages for h in p['matches']]
    assert [(h['line'],h['column']) for h in hits] == [(1,64),(2,6)]
    assert all('needle' in h['text'] for h in hits)
    assert any(p['reason']=='scan_limit' and not p['matches'] for p in pages)


def test_search_crosses_unicode_boundaries_and_files(tmp_path):
    (tmp_path/'a.txt').write_text('a'*65534+'你好🙂needle\n', encoding='utf-8')
    (tmp_path/'b.txt').write_text('你好🙂needle\n', encoding='utf-8')
    folder = KnowledgeFolder(tmp_path, limits=Limits(scan_bytes=65536))
    pages = search(folder, query='你好🙂needle', limit=1, context=0)
    hits = [h for p in pages for h in p['matches']]
    assert [h['path'] for h in hits] == ['a.txt','b.txt']
    assert hits[0]['column'] == 65535


def test_regex_context_and_long_line_limit_are_explicit(tmp_path):
    (tmp_path/'a.txt').write_text('before\nvalue=42\nafter\n'+'x'*200000+'\nvalue=43\n', encoding='utf-8')
    folder = KnowledgeFolder(tmp_path, limits=Limits(regex_line_chars=10000))
    page = folder.grep(r'value=\d+', regex_mode=True, context=1)
    assert [h['line'] for h in page['matches']] == [2,5]
    assert [r['text'] for r in page['matches'][0]['context']] == ['before','value=42','after']
    assert page['partial'] and any(s['reason']=='regex_line_limit' for s in page['skipped'])


def test_search_continuation_rejects_changed_manifest_and_query(tmp_path):
    (tmp_path/'a.txt').write_text('needle\nneedle\n', encoding='utf-8')
    folder = KnowledgeFolder(tmp_path)
    cursor = folder.grep('needle', limit=1)['next_cursor']
    with pytest.raises(KnowledgeError, match='another request'):
        folder.grep('other', limit=1, cursor=cursor)
    (tmp_path/'b.txt').write_text('needle\n', encoding='utf-8')
    with pytest.raises(KnowledgeError, match='files changed'):
        folder.grep('needle', limit=1, cursor=cursor)


def test_rest_read_cursor_and_explicit_legacy_hash(tmp_path):
    raw = b'x'*50000
    (tmp_path/'a.txt').write_bytes(raw)
    with TestClient(create_app(Settings(str(tmp_path), allowed_hosts=('testserver',))), client=('127.0.0.1',1234)) as client:
        first = client.get('/api/v1/read', params={'path':'a.txt'}).json()
        second = client.get('/api/v1/read', params={'path':'a.txt','cursor':first['next_cursor']}).json()
        assert second['lines'][0]['column'] == 48001 and second['eof']
    folder = KnowledgeFolder(tmp_path, limits=Limits(hash_bytes=10))
    with pytest.raises(KnowledgeError, match='expected_version'):
        folder.read_file('a.txt', expected_sha256=hashlib.sha256(raw).hexdigest())
