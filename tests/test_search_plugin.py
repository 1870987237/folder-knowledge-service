import json
from pathlib import Path
import sys

import pytest

from folder_kb.errors import KnowledgeError
from folder_kb.plugin_package import package_plugin
from folder_kb.retrieval import Retrieval
from folder_kb.search_plugin import run_process, validate_hits
from tests.test_template_management import env, auth


def install_program(env, tmp_path, monkeypatch, code=None, path_filter=True):
    app, client, key, settings = env
    source = Path('examples/plugins/bm25-markdown/package')
    definition = json.loads(Path('examples/plugins/bm25-markdown/definition.json').read_text(encoding='utf-8'))
    definition['plugin']['indexing']['supports_path_filter'] = path_filter
    if code:
        source = tmp_path / 'source'
        source.mkdir()
        (source / 'search.py').write_text(code, encoding='utf-8')
        # Prove the platform also accepts semantic/custom algorithms, not just fulltext.
        definition['plugin']['indexing']['mode'] = 'semantic'
    manifest = tmp_path / 'definition.json'
    manifest.write_text(json.dumps(definition), encoding='utf-8')
    output = package_plugin(source, manifest, tmp_path / 'upload')
    response = client.post('/admin/api/plugins', headers=auth(), json=json.loads(Path(output['manifest']).read_text(encoding='utf-8')))
    assert response.status_code == 201, response.text
    assert response.json()['capabilities']['indexing']['integration'] == 'available'
    installed = client.put('/admin/api/plugins/bm25-markdown/versions/1.0.1/package', headers=auth(),
                           content=Path(output['package']).read_bytes())
    assert installed.status_code == 200, installed.text
    for lid in ('alpha', 'beta'):
        r = client.put(f'/admin/api/libraries/{lid}/plugin', headers=auth(),
                       json={'plugin_id': 'bm25-markdown', 'plugin_version': '1.0.1'})
        assert r.status_code == 200, r.text
    monkeypatch.setenv('KB_SEARCH_TRUSTED_PYTHON', '1')
    monkeypatch.setenv('KB_SEARCH_PLUGIN_ALLOWLIST', installed.json()['package_sha256'])
    root = Path(settings.root)
    (root / 'alpha/docs').mkdir()
    (root / 'alpha/docs/a.md').write_text('# Apple\nApple orchard\n', encoding='utf-8')
    (root / 'alpha/docs/b.md').write_text('# Apple\nApple banana\n', encoding='utf-8')
    (root / 'alpha/outside.md').write_text('Apple elsewhere\n', encoding='utf-8')
    (root / 'alpha/archive').mkdir()
    (root / 'alpha/archive/secret.md').write_text('Apple excluded\n', encoding='utf-8')
    return Retrieval(app.state.registry)


def test_program_build_query_filter_and_authorization(env, tmp_path, monkeypatch):
    _, c, key, _ = env
    service = install_program(env, tmp_path, monkeypatch)
    assert service.state('alpha')['status'] == 'missing'
    built = service.build('alpha')
    assert built['engine'] == 'bm25-markdown-v2'
    assert service.state('alpha')['status'] == 'ready'
    result = c.get('/api/v1/search', headers=auth(key['token']), params={
        'library_id': 'alpha', 'query': 'Apple', 'path': 'docs', 'limit': 1})
    assert result.status_code == 200, result.text
    result = result.json()
    assert result['has_more'] and result['index_status'] == 'ready'
    assert len(result['results']) == 1 and result['results'][0]['path'].startswith('docs/')
    assert result['scope']['path'] == 'docs' and 'archive/**' in result['scope']['exclude']
    assert c.get('/api/v1/search', headers=auth(key['token']), params={'library_id': 'beta', 'query': 'Apple'}).status_code == 403
    assert service.search('alpha', 'excluded')['results'] == []
    assert service.search('alpha', 'Apple', path='missing')['results'] == []
    assert not list((service.directory / 'runtime').iterdir())


def test_runtime_and_package_approval_required(env, tmp_path, monkeypatch):
    service = install_program(env, tmp_path, monkeypatch)
    monkeypatch.delenv('KB_SEARCH_PLUGIN_ALLOWLIST')
    with pytest.raises(KnowledgeError, match='allowlist'):
        service.build('alpha')
    assert service.state('alpha')['error'] == 'search_runtime_disabled'


def test_failed_build_preserves_generation_and_old_source_detected(env, tmp_path, monkeypatch):
    service = install_program(env, tmp_path, monkeypatch)
    service.build('alpha')
    pointer = service.plugin_pointer('alpha').read_bytes()
    import folder_kb.search_plugin as module
    def fail(*args): raise KnowledgeError('search_plugin_timeout', 'timeout', 504)
    with monkeypatch.context() as patch:
        patch.setattr(module, 'run_process', fail)
        with pytest.raises(KnowledgeError): service.build('alpha')
    assert service.plugin_pointer('alpha').read_bytes() == pointer
    assert service.search('alpha', 'Apple')['results']
    (Path(env[3].root) / 'alpha/docs/a.md').write_text('different', encoding='utf-8')
    assert service.state('alpha')['status'] == 'stale'
    with pytest.raises(KnowledgeError) as error:
        service.search('alpha', 'Apple')
    assert error.value.code == 'index_stale'


CUSTOM = '''import argparse,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--input');p.add_argument('--output');a=p.parse_args()
r=json.loads(Path(a.input).read_text());o=Path(a.output)
if r['operation']=='build':
 (o/'index').mkdir();(o/'index/docs.json').write_text(json.dumps(r['documents']))
 result={'protocol_version':'1.0','engine':'independent-algorithm','chunks':len(r['documents'])}
else:
 docs=json.loads((Path(r['index_dir'])/'docs.json').read_text())
 assert r['query']=='???'
 docs=[d for d in docs if d['path'].startswith('docs/')]
 result={'protocol_version':'1.0','has_more':False,'results':[{'path':d['path'],'start_line':1,'end_line':2,'title':'Example','text':d['text'],'score':0.0} for d in sorted(docs,key=lambda d:d['path'],reverse=True)]}
(o/'result.json').write_text(json.dumps(result))
'''


def test_platform_preserves_plugin_query_and_order(env, tmp_path, monkeypatch):
    service = install_program(env, tmp_path, monkeypatch, CUSTOM, path_filter=False)
    service.build('alpha')
    # This query would be rejected by the legacy tokenizer; the custom plugin receives it unchanged.
    result = service.search('alpha', '???')
    assert result['engine'] == 'independent-algorithm'
    assert [x['path'] for x in result['results']] == ['docs/b.md', 'docs/a.md']
    with pytest.raises(KnowledgeError) as exc:
        service.search('alpha', '???', path='docs')
    assert exc.value.code == 'search_filter_unsupported'


@pytest.mark.parametrize('change', ['outside', 'lines', 'fabrication', 'nan', 'scope'])
def test_result_validation_rejects_unsafe_or_fabricated_evidence(change):
    item = dict(path='docs/a.md', start_line=1, end_line=1, text='hello', title='a', score=1.0)
    path = '.'
    if change == 'outside': item['path'] = '../other-private-library.md'
    elif change == 'lines': item['end_line'] = 2
    elif change == 'fabrication': item['text'] = 'invented answer'
    elif change == 'nan': item['score'] = float('nan')
    else: path = 'other'
    with pytest.raises(ValueError):
        validate_hits({'protocol_version': '1.0', 'results': [item], 'has_more': False},
                      [('docs/a.md', 'hello\n', 'sha', 'version')], 5, path)


def test_real_program_timeout_is_bounded(tmp_path):
    with pytest.raises(KnowledgeError) as exc:
        run_process([sys.executable, '-c', 'import time; time.sleep(20)'], tmp_path, .2)
    assert exc.value.code == 'search_plugin_timeout'
