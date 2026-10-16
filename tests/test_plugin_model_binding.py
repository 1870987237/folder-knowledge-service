"""Web/API configuration -> worker subprocess -> model HTTP, using synthetic data."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from folder_kb.errors import KnowledgeError
from folder_kb.updates import UpdatePolicy
from tests.test_collaboration import ADMIN, env, headers
from tests.test_plugin_composition import setup, submit


def test_declared_defaults_and_wrong_mapping_rejected(setup):
    s = setup; c = s['c']
    version = s['settings']['plugin_version']
    path = f'/admin/api/resource-access/plugin/general-knowledge@{version}'
    access = c.get(path, headers=headers(ADMIN)).json()
    assert access['model_environment']['KNOWLEDGE_LLM_BASE_URL'] == 'base_url'
    wrong = {k: access[k] for k in ('approved', 'organization_ids', 'user_ids')}
    wrong['model_environment'] = UpdatePolicy().model_environment
    r = c.put(path, headers=headers(ADMIN), json=wrong)
    assert r.status_code == 409 and r.json()['error'] == 'model_mapping_invalid'
    assert c.get(path, headers=headers(ADMIN)).json() == access
    llm = dict(s['settings'], config={})  # default mode is llm, not just an explicit mode.
    r = c.put(s['base'] + '/plugin-settings', headers=headers(s['token']), json=llm)
    assert r.status_code == 409 and r.json()['error'] == 'model_required'
    assert c.get(s['base'] + '/plugin-settings', headers=headers(s['token'])).json()['binding']['config']['mode'] == 'structured'


def test_portal_worker_model_roundtrip(setup, monkeypatch):
    s = setup; c = s['c']; calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert self.path == '/v1/chat/completions'
            assert self.headers['Authorization'] == 'Bearer synthetic-model-key'
            assert value['model'] == 'synthetic-llm'
            calls.append(value)
            decision = {'action': 'create', 'type': 'reference', 'title': 'Orchard hours',
                        'summary': 'Opening hours', 'body': 'Orchard opens at nine.',
                        'reason': 'New fact', 'evidence': ['Orchard opens at nine.']}
            response = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'decisions': [decision]})}}]}).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(response)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        monkeypatch.setenv('TEST_MODEL_KEY', 'synthetic-model-key')
        monkeypatch.setenv('KB_MODEL_KEY_ALLOWLIST', 'TEST_MODEL_KEY')
        model = {'id': 'model-contract', 'name': 'Synthetic model', 'base_url': f'http://127.0.0.1:{server.server_port}/v1',
                 'model': 'synthetic-llm', 'api_key_env': 'TEST_MODEL_KEY'}
        assert c.put('/admin/api/models/model-contract', headers=headers(ADMIN), json=model).status_code == 200
        assert c.put('/admin/api/resource-access/model/model-contract', headers=headers(ADMIN),
                     json={'approved': True, 'user_ids': [s['uid']]}).status_code == 200
        config = dict(s['settings'], config={'mode': 'llm'}, model_connection_id='model-contract')
        r = c.put(s['base'] + '/plugin-settings', headers=headers(s['token']), json=config)
        assert r.status_code == 200, r.text
        # No manually supplied KNOWLEDGE_LLM_* variables: defaults must traverse the real worker.
        submit(s)
        job = s['run']()
        assert len(calls) == 1
        assert s['worker'].updates.get(job)['status'] == 'candidate_ready'
        s['publish'](job)
        root = s['worker'].updates.catalog.snapshot_source(s['lid'])['root']
        assert any('Orchard opens at nine.' in p.read_text(encoding='utf-8') for p in (root / 'knowledge/reference').glob('*.md'))
        # Corrupt a saved legacy policy: enqueue/worker must reject before plugin execution.
        updates = s['worker'].updates
        with s['registry'].store.transaction(write=True) as db:
            policy = json.loads(db.execute('SELECT definition FROM update_policies WHERE library_id=?', (s['lid'],)).fetchone()[0])
            policy['model_environment'] = UpdatePolicy().model_environment
            db.execute('UPDATE update_policies SET definition=? WHERE library_id=?', (json.dumps(policy), s['lid']))
        with pytest.raises(KnowledgeError) as error:
            updates.enqueue(s['lid'])
        assert error.value.code in ('model_mapping_changed', 'model_mapping_invalid')
        assert len(calls) == 1
    finally:
        server.shutdown(); server.server_close(); thread.join(5)
