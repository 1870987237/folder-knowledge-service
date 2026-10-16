"""Synthetic end-to-end coverage: human/agent edits, pipeline-only policy, independent search."""
import json
from pathlib import Path

import pytest

from folder_kb.capabilities import Capabilities, Policy
from folder_kb.plugin_package import package_plugin
from folder_kb.retrieval import Retrieval
from folder_kb.template_worker import TemplateWorker
from tests.test_collaboration import env, account, headers, ADMIN, draft, action


@pytest.fixture
def setup(env, tmp_path, monkeypatch):
    c, registry = env
    uid, token = account(c, 'owner')
    installed = {}
    for name in ('general-knowledge', 'bm25-markdown'):
        source = Path('examples/plugins') / name
        package = package_plugin(source / 'package', source / 'definition.json', tmp_path / name)
        manifest = json.loads(Path(package['manifest']).read_text(encoding='utf-8'))
        assert c.post('/admin/api/plugins', headers=headers(ADMIN), json=manifest).status_code == 201
        url = f"/admin/api/plugins/{name}/versions/{manifest['version']}/package"
        r = c.put(url, headers=headers(ADMIN), content=Path(package['package']).read_bytes())
        assert r.status_code == 200, r.text
        installed[name] = r.json()
        assert c.put(f"/admin/api/resource-access/plugin/{name}@{manifest['version']}", headers=headers(ADMIN),
                     json={'approved': True, 'user_ids': [uid]}).status_code == 200
    settings = {'plugin_id': 'general-knowledge', 'plugin_version': '1.1.0', 'config': {'mode': 'structured'}, 'enabled': True}
    r = c.post('/portal/api/libraries', headers=headers(token), json={'name': 'Editable synthetic', 'template': settings})
    assert r.status_code == 201, r.text
    lid = r.json()['id']; base = f'/portal/api/libraries/{lid}'
    monkeypatch.setenv('KB_TEMPLATE_TRUSTED_PYTHON', '1')
    monkeypatch.setenv('KB_SEARCH_TRUSTED_PYTHON', '1')
    monkeypatch.setenv('KB_SEARCH_PLUGIN_ALLOWLIST', ','.join(p['package_sha256'] for p in installed.values()))
    worker = TemplateWorker(registry)
    def run(operation='update'):
        r = c.post(base + '/update-jobs', headers=headers(token), json={'operation': operation})
        assert r.status_code == 201, r.text
        job = r.json()['id']; assert worker.tick()
        assert worker.updates.get(job)['status'] == 'candidate_ready', worker.updates.get(job)
        return job
    def publish(job):
        r = c.post(base + '/update-jobs/' + job + '/approve', headers=headers(token))
        assert r.status_code == 200, r.text
        assert worker.tick()
        assert worker.updates.get(job)['status'] == 'published', worker.updates.get(job)
    publish(run('initialize'))
    return locals()


def submit(s, number='one', **context):
    r = s['c'].post(s['base'] + '/inbox', headers=headers(s['token']), json={
        'library_id': s['lid'], 'submission_id': number, 'title': 'Orchard',
        'content': {'text': 'Orchard opens at nine.'}, 'context': {'knowledge_summary': 'Opening hours', **context}})
    assert r.status_code == 201, r.text


def test_human_edit_pipeline_merge_move_delete_and_conflict(setup):
    s = setup; c, token = s['c'], s['token']; catalog = s['worker'].updates.catalog
    assert next(x for x in c.get('/portal/api/libraries', headers=headers(token)).json() if x['id'] == s['lid'])['editable']
    submit(s); s['publish'](s['run']())
    root = catalog.snapshot_source(s['lid'])['root']
    note = next((root / 'knowledge/reference').glob('*.md')); path = note.relative_to(root / 'knowledge').as_posix()
    text = note.read_text(encoding='utf-8').replace('Orchard opens at nine.', 'Orchard opens at ten.')
    did = draft(c, token, s['lid'], text, path)
    assert action(c, token, did, 'submit').status_code == 200
    assert action(c, token, did, 'publish', revision=2).status_code == 200
    updated = catalog.snapshot_source(s['lid'])['root']
    assert (updated / 'metadata/general-knowledge/state.json').exists()
    assert (updated / 'knowledge' / path).read_text(encoding='utf-8') == text
    ident = next(iter(json.loads((updated / 'metadata/general-knowledge/state.json').read_text(encoding='utf-8'))['documents']))
    submit(s, 'two', knowledge_action='merge', knowledge_target=ident)
    job = s['run']()
    candidate = s['worker'].jobs_root / job / 'output/workspace/knowledge' / path
    assert 'opens at ten' in candidate.read_text(encoding='utf-8')
    # Another human publication after generation must not be overwritten by a stale candidate.
    did = draft(c, token, s['lid'], text + '\nHuman later edit.\n', path)
    assert action(c, token, did, 'submit').status_code == 200
    assert action(c, token, did, 'publish', revision=2).status_code == 200
    result = c.post(s['base'] + '/update-jobs/' + job + '/approve', headers=headers(token))
    if result.status_code == 200:
        s['worker'].tick()
    assert s['worker'].updates.get(job)['status'] != 'published'
    latest = catalog.snapshot_source(s['lid'])['root']
    assert 'Human later edit.' in (latest / 'knowledge' / path).read_text(encoding='utf-8')


def test_independent_search_and_agent_writes_preserve_metadata(setup):
    s = setup; c, token, lid = s['c'], s['token'], s['lid']
    version = s['installed']['bm25-markdown']['definition']['version']
    assert c.put(s['base'] + '/search-plugin', headers=headers(token), json={
        'plugin_id': 'bm25-markdown', 'plugin_version': version}).status_code == 200
    assert c.get(s['base'] + '/plugin-settings', headers=headers(token)).json()['binding']['plugin_id'] == 'general-knowledge'
    assert c.get(s['base'] + '/search-plugin', headers=headers(token)).json()['binding']['plugin_id'] == 'bm25-markdown'
    assert c.put(s['base'] + '/agent-settings', headers=headers(token), json={'direct_enabled': True}).status_code == 200
    key = c.post('/portal/api/keys', headers=headers(token), json={'name': 'writer', 'library_ids': [lid]}).json()
    Capabilities(s['registry']).policy(key['id'], Policy(permissions=[{'library_id': lid, 'search': True, 'write_policy': 'direct'}]))
    r = c.post('/api/v1/write', headers=headers(key['token']), json={'library_id': lid, 'request_id': 'human-like-agent',
        'path': 'note.md', 'operation': 'create', 'content': '# Orchard\nManually entered orchard details.\n'})
    assert r.status_code == 200, r.text
    root = s['worker'].updates.catalog.snapshot_source(lid)['root']
    assert (root / 'metadata/general-knowledge/state.json').exists()
    search = Retrieval(s['registry'])
    assert search.build(lid)['status'] == 'ready'
    assert any(h['path'] == 'note.md' for h in search.search(lid, 'orchard')['results'])
    assert c.delete(s['base'] + '/search-plugin', headers=headers(token)).status_code == 200
    assert 'search' not in Capabilities(s['registry']).discovery(key['token'])[0]
    assert c.get(s['base'] + '/plugin-settings', headers=headers(token)).json()['policy']['enabled']
    # Generated directory is read-only even when ordinary content permits direct writes.
    assert c.get(s['base'] + '/document?path=INDEX.md', headers=headers(token)).json()['editable'] is False
    r = c.post('/api/v1/files/changes', headers=headers(key['token']), json={'library_id': lid, 'request_id': 'remove-index',
        'changes': [{'operation': 'mkdir', 'path': 'INDEX.md/sub'}]})
    assert r.status_code == 409 and r.json()['error'] == 'generated_document'


def test_pipeline_only_blocks_existing_keys_and_drafts_but_accepts_materials(setup, tmp_path):
    s = setup; c, token, lid = s['c'], s['token'], s['lid']
    did = draft(c, token, lid, '# Manual\n', 'manual.md')
    assert c.put(s['base'] + '/agent-settings', headers=headers(token), json={'direct_enabled': True}).status_code == 200
    key = c.post('/portal/api/keys', headers=headers(token), json={'name': 'writer', 'library_ids': [lid]}).json()
    Capabilities(s['registry']).policy(key['id'], Policy(permissions=[{'library_id': lid, 'write_policy': 'direct'}]))
    # A new immutable version declares a pipeline-only document policy.
    data = s['installed']['general-knowledge']['definition'].copy(); data['version'] = 'pipeline-only-test'
    data['plugin'] = dict(data['plugin'], document_editing='forbidden')
    package = package_plugin(Path('examples/plugins/general-knowledge/package'),
                             Path('examples/plugins/general-knowledge/definition.json'), tmp_path / 'locked')
    assert c.post('/admin/api/plugins', headers=headers(ADMIN), json=data).status_code == 201
    assert c.put('/admin/api/plugins/general-knowledge/versions/pipeline-only-test/package', headers=headers(ADMIN),
                 content=Path(package['package']).read_bytes()).status_code == 200
    b = s['worker'].updates.catalog.binding(lid)['binding']
    assert c.put(f'/admin/api/libraries/{lid}/plugin', headers=headers(ADMIN), json={'plugin_id': 'general-knowledge',
        'plugin_version': 'pipeline-only-test', 'workspace': b['workspace'], 'config': {'mode': 'structured'}}).status_code == 200
    assert action(c, token, did, 'save', content='blocked').json()['error'] == 'plugin_editing_forbidden'
    assert action(c, token, did, 'submit').json()['error'] == 'plugin_editing_forbidden'
    assert 'write_file' not in Capabilities(s['registry']).discovery(key['token'])[0]
    r = c.post('/api/v1/write', headers=headers(key['token']), json={'library_id': lid, 'request_id': 'blocked',
        'path': 'manual.md', 'operation': 'create', 'content': 'blocked'})
    assert r.status_code == 403
    submit(s, 'still-accept-material')


def test_search_resources_are_scoped_and_revocation_takes_effect(setup):
    s = setup; c, token = s['c'], s['token']
    _, outsider = account(c, 'outsider')
    assert c.get(s['base'] + '/search-plugin-options', headers=headers(outsider)).status_code == 403
    value = {'plugin_id': 'bm25-markdown', 'plugin_version': '1.0.1'}
    assert c.put(s['base'] + '/search-plugin', headers=headers(token), json=value).status_code == 200
    assert c.put('/admin/api/resource-access/plugin/bm25-markdown@1.0.1', headers=headers(ADMIN), json={}).status_code == 200
    assert Retrieval(s['registry']).state(s['lid'])['error'] == 'resource_forbidden'
    assert c.put(s['base'] + '/search-plugin', headers=headers(token), json=value).status_code == 403
