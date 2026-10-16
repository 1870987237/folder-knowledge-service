import importlib.util
import json
from pathlib import Path

import pytest

from folder_kb.inbox import Inbox, Material
from folder_kb.management import Registry
from folder_kb.plugin_package import package_plugin
from folder_kb.retrieval import Retrieval
from folder_kb.template_protocol import JobRequest, Record, TemplateRef, prepare_job, validate_result
from folder_kb.template_worker import TemplateWorker
from folder_kb.templates import TemplateBinding, TemplateRegistration
from folder_kb.updates import UpdatePolicy, Updates
from folder_kb.workspaces import WorkspaceSpec

PACKAGE = Path('examples/plugins/general-knowledge/package')
spec = importlib.util.spec_from_file_location('general_knowledge', PACKAGE / 'program.py')
program = importlib.util.module_from_spec(spec)
spec.loader.exec_module(program)


def material(ident, text='The orchard opens at nine.', kind='new_content', **context):
    return Record(id=ident, material=Material(submission_id=ident, library_id='demo', kind=kind,
        title='Opening hours', content={'text': text}, source={'reference': 'synthetic:test'},
        context={'knowledge_type': 'reference', 'knowledge_summary': 'Opening hours', **context}))


def run(tmp_path, source=None, records=(), planner=None, config=None):
    job = tmp_path / f'job-{len(list(tmp_path.glob("job-*")))}'
    if source is None:
        source = tmp_path / 'empty'
        source.mkdir(exist_ok=True)
    request = JobRequest(job_id=job.name, operation='update' if records else 'initialize',
        library_id='demo', base_version=program.workspace_digest(source),
        template=TemplateRef(id='general-knowledge', version='1.0.0', package_sha256='a'*64),
        knowledge_subpath='knowledge', workspace_key='test', config=config or {'mode': 'structured'})
    prepare_job(job, request, list(records), source)
    before = program.workspace_digest(job/'input/workspace')
    program.run(request.operation, job/'input', job/'work', job/'output', planner=planner)
    assert program.workspace_digest(job/'input/workspace') == before
    validate_result(job, request, list(records))
    return job/'output/workspace'


def state(root):
    return json.loads((root/'metadata/general-knowledge/state.json').read_text(encoding='utf-8'))


def test_exact_dedup_preserves_sources_skips_model_and_replay_is_identical(tmp_path):
    initial = run(tmp_path)
    calls = []
    def planner(mat, docs, scope, cfg):
        calls.append(mat)
        return program.structured_plan(mat)
    a, b = material('a'), material('b')
    result = run(tmp_path, initial, [a, b], planner)
    assert len(calls) == 1
    docs = state(result)['documents']
    assert len(docs) == 1
    assert len(next(iter(docs.values()))['sources']) == 2
    assert len(list((result/'knowledge/reference').glob('*.md'))) == 1
    replay = run(tmp_path, result, [a, b], lambda *args: pytest.fail('Replay called model'))
    assert program.workspace_digest(result) == program.workspace_digest(replay)
    with pytest.raises(program.Invalid, match='reused'):
        run(tmp_path, result, [material('a', 'Different content')])


def test_merge_correction_conflict_and_observation(tmp_path):
    initial = run(tmp_path)
    base = run(tmp_path, initial, [material('a')])
    ident = next(iter(state(base)['documents']))
    merged = run(tmp_path, base, [material('b', 'Closed on Sunday.', knowledge_action='merge', knowledge_target=ident)])
    assert 'Closed on Sunday.' in state(merged)['documents'][ident]['body']
    corrected = run(tmp_path, merged, [material('c', 'Opens at ten; closed on Sunday.', kind='correction', knowledge_action='revise', knowledge_target=ident)])
    assert state(corrected)['documents'][ident]['body'] == 'Opens at ten; closed on Sunday.'
    ledger = (corrected/'metadata/general-knowledge/processed.jsonl').read_text(encoding='utf-8')
    assert 'The orchard opens at nine.' in ledger
    def defer(mat, *args):
        return [dict(action='defer', reason='Conflicting claim; review required', evidence=[mat['content']['text']])]
    review = run(tmp_path, corrected, [material('d', 'Opens at midnight.'), material('e', 'Maybe always closed.', kind='observation')], defer)
    assert state(review)['documents'] == state(corrected)['documents']
    assert 'midnight' not in ''.join(p.read_text(encoding='utf-8') for p in (review/'knowledge').rglob('*.md'))
    assert len((review/'metadata/general-knowledge/review.jsonl').read_text(encoding='utf-8').splitlines()) == 2
    # A correction invalidates the old content fingerprint: do not discard a new old claim as a duplicate.
    called = []
    def record_defer(*args):
        called.append(1)
        return defer(*args)
    run(tmp_path, corrected, [material('f')], record_defer)
    assert called


@pytest.mark.parametrize('fault', ['invented_evidence', 'cross_scope', 'bad_path', 'implicit_overwrite'])
def test_invalid_model_decisions_do_not_change_published_files(tmp_path, fault):
    base = run(tmp_path, run(tmp_path), [material('a')])
    digest = program.workspace_digest(base)
    ident = next(iter(state(base)['documents']))
    rec = material('b', 'New detail', scope='other' if fault == 'cross_scope' else 'general')
    def planner(mat, *args):
        d = dict(action='merge', target=ident, body='New detail', reason='test', evidence=['New detail'])
        if fault == 'invented_evidence': d['evidence'] = ['invented']
        if fault == 'bad_path': d['target'] = '../../elsewhere'
        if fault == 'implicit_overwrite': d.update(action='revise', title='Replacement', summary='test')
        return [d]
    with pytest.raises(program.Invalid): run(tmp_path, base, [rec], planner)
    assert program.workspace_digest(base) == digest


def test_unmanaged_files_preserved_and_managed_external_edits_not_overwritten(tmp_path):
    base = run(tmp_path, run(tmp_path), [material('a')])
    (base/'metadata/extra.txt').write_text('auxiliary')
    result = run(tmp_path, base, [material('b')])
    assert (result/'metadata/extra.txt').read_text(encoding='utf-8') == 'auxiliary'
    next((result/'knowledge/reference').glob('*.md')).write_text('User edited this')
    with pytest.raises(program.Invalid, match='externally'):
        run(tmp_path, result, [material('c')])


def test_model_http_semantic_merge_and_failure_without_fallback(tmp_path, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(payload)
            data = json.loads(payload['messages'][1]['content'])
            mat, docs = data['material'], data['documents']
            if mat['content']['text'] == 'force invalid response':
                answer = 'invalid JSON'
            else:
                decisions = program.structured_plan(mat) if not docs else [dict(action='merge',
                    target=next(iter(docs)), body=mat['content']['text'], reason='Compatible new detail',
                    evidence=[mat['content']['text']])]
                answer = json.dumps({'decisions': decisions})
            response = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': answer}}]}).encode()
            self.send_response(200); self.send_header('Content-Length', str(len(response))); self.end_headers()
            self.wfile.write(response)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    monkeypatch.setenv('KNOWLEDGE_LLM_BASE_URL', f'http://127.0.0.1:{server.server_port}/v1')
    monkeypatch.setenv('KNOWLEDGE_LLM_MODEL', 'synthetic-contract-model')
    monkeypatch.delenv('KNOWLEDGE_LLM_API_KEY', raising=False)
    try:
        initial = run(tmp_path)
        result = run(tmp_path, initial, [material('a'), material('b', 'Closed on Sunday.')], config={'mode': 'llm'})
        assert len(calls) == 2
        assert len(state(result)['documents']) == 1
        assert 'Closed on Sunday.' in next(iter(state(result)['documents'].values()))['body']
        assert 'sources' not in next(iter(json.loads(calls[1]['messages'][1]['content'])['documents'].values()))
        before = program.workspace_digest(result)
        with pytest.raises(program.Invalid, match='invalid JSON'):
            run(tmp_path, result, [material('c', 'force invalid response')], config={'mode': 'llm'})
        assert program.workspace_digest(result) == before
    finally:
        server.shutdown(); thread.join(); server.server_close()


def test_platform_install_initialize_submit_review_publish_and_search(tmp_path, monkeypatch):
    root, storage = tmp_path/'pool', tmp_path/'state'
    (root/'demo/knowledge').mkdir(parents=True)
    registry = Registry(str(root), str(storage))
    registry.save_library('demo', 'Synthetic general knowledge', 'demo/knowledge', True, create=True)
    package = package_plugin(PACKAGE, PACKAGE.parent/'definition.json', tmp_path/'package')
    updates = Updates(registry)
    updates.catalog.register(TemplateRegistration(**json.loads(Path(package['manifest']).read_text(encoding='utf-8'))))
    installed = updates.catalog.install('general-knowledge', '1.0.0', Path(package['package']).read_bytes())
    updates.catalog.bind('demo', TemplateBinding(template_id='general-knowledge', template_version='1.0.0',
        config={'mode': 'structured'}, workspace=WorkspaceSpec(type='local', root_path='demo', knowledge_subpath='knowledge')))
    updates.save_policy('demo', UpdatePolicy(enabled=True, approved_package_sha256=installed['package_sha256']))
    monkeypatch.setenv('KB_TEMPLATE_TRUSTED_PYTHON', '1')
    worker = TemplateWorker(registry)
    init = updates.enqueue('demo', 'initialize')
    worker.tick()
    assert updates.get(init['id'])['status'] == 'candidate_ready'
    assert not (root/'demo/knowledge/INDEX.md').exists()
    updates.act(init['id'], 'approve'); worker.tick()
    assert updates.get(init['id'])['status'] == 'published'
    key = registry.create_key('synthetic', ['demo'], None)
    inbox = Inbox(registry); inbox.grants(key['key']['id'], ['demo'])
    receipts = [inbox.submit(key['token'], material(ident).material) for ident in ('a', 'b')]
    worker.tick(); worker.tick()
    job = updates.listing()['jobs'][0]
    assert job['status'] == 'candidate_ready', job
    assert updates.preview(job['id'])['changes']
    assert not worker.tick()  # Manual review is mandatory.
    updates.act(job['id'], 'approve'); worker.tick()
    assert updates.get(job['id'])['status'] == 'published'
    published = updates.catalog.snapshot_source('demo')['root']
    assert len(state(published)['documents']) == 1
    assert all(inbox.get(item['id'])['result']['published'] for item in receipts)
    monkeypatch.setenv('KB_SEARCH_TRUSTED_PYTHON', '1')
    monkeypatch.setenv('KB_SEARCH_PLUGIN_ALLOWLIST', installed['package_sha256'])
    retrieval = Retrieval(registry)
    assert retrieval.state('demo')['status'] != 'ready'  # No automatic index build.
    assert retrieval.build('demo')['engine'] == 'bm25-markdown-v1'
    hits = retrieval.search('demo', 'orchard', path='reference')['results']
    assert hits and all(h['path'].startswith('reference/') for h in hits)
    assert (PACKAGE/'search.py').read_bytes() == Path('examples/plugins/bm25-markdown/package/search.py').read_bytes()
