import hashlib
import json
from pathlib import Path

import pytest

from folder_kb.inbox import Material
from folder_kb.template_package import build_package
from folder_kb.template_worker import TemplateWorker
from folder_kb.updates import ModelConnection
from tests.test_collaboration import ADMIN, account, env, headers, library, organization


@pytest.fixture
def scenario(env, monkeypatch):
    c, registry = env
    owner_id, owner = account(c, 'owner')
    editor_id, editor = account(c, 'editor')
    publisher_id, publisher = account(c, 'publisher')
    outsider_id, outsider = account(c, 'outsider')
    oid = organization(c, 'owner')
    for name in ('editor', 'publisher'):
        assert c.put(f'/portal/api/organizations/{oid}/members', headers=headers(owner),
                     json={'username': name, 'role': 'member'}).status_code == 200
    definition = json.loads(Path('examples/template-platform-config.json').read_text(encoding='utf-8'))
    archive = build_package(Path('examples/templates/reference-docs'))
    definition['source'] = {'archive_sha256': hashlib.sha256(archive).hexdigest()}
    assert c.post('/admin/api/templates', headers=headers(ADMIN), json=definition).status_code == 201
    assert c.put('/admin/api/templates/reference-docs/versions/0.2.0/package', headers=headers(ADMIN), content=archive).status_code == 200
    resource = '/admin/api/resource-access/plugin/reference-docs@0.2.0'
    granted = {'approved': True, 'organization_ids': [oid], 'user_ids': [owner_id]}
    assert c.put(resource, headers=headers(ADMIN), json=granted).status_code == 200
    template = {'plugin_id': 'reference-docs', 'plugin_version': '0.2.0', 'enabled': True}
    result = c.post('/portal/api/libraries', headers=headers(owner), json={'name': 'Synthetic template library', 'organization_id': oid, 'template': template})
    assert result.status_code == 201, result.text
    lid = result.json()['id']
    base = f'/portal/api/libraries/{lid}'
    for uid, role in ((editor_id, 'editor'), (publisher_id, 'publisher')):
        assert c.put(base + '/members', headers=headers(owner), json={'user_id': uid, 'role': role}).status_code == 200
    monkeypatch.setenv('KB_TEMPLATE_TRUSTED_PYTHON', '1')
    return locals()


def material(s, token=None, number='one'):
    r = s['c'].post(s['base'] + '/inbox', headers=headers(token or s['editor']), json={
        'library_id': s['lid'], 'submission_id': number, 'title': 'Synthetic material',
        'content': {'text': 'A synthetic protocol fact, no private content.'}})
    assert r.status_code == 201, r.text
    return r.json()


def candidate(s):
    receipt = material(s)
    r = s['c'].post(s['base'] + '/update-jobs', headers=headers(s['publisher']), json={'operation': 'update'})
    assert r.status_code == 201, r.text
    worker = TemplateWorker(s['registry'])
    assert worker.tick()
    job = s['c'].get(s['base'] + '/update-jobs/' + r.json()['id'], headers=headers(s['publisher'])).json()
    assert job['status'] == 'candidate_ready', job
    return receipt, job, worker


def test_creation_and_scoped_catalog(scenario):
    s = scenario; c = s['c']
    assert not next(l for l in c.get('/portal/api/libraries', headers=headers(s['owner'])).json() if l['id'] == s['lid'])['editable']
    assert c.get('/portal/api/resources', headers=headers(s['outsider'])).json() == {'plugins': [], 'models': []}
    assert c.get('/portal/api/resources?organization_id=' + s['oid'], headers=headers(s['editor'])).status_code == 403
    resources = c.get('/portal/api/resources?organization_id=' + s['oid'], headers=headers(s['owner'])).json()
    assert len(resources['plugins']) == 1
    assert 'entrypoints' not in resources['plugins'][0] and 'libraries' not in resources['plugins'][0]
    before = len(s['registry'].libraries())
    dirs = set((s['registry'].pool.root / 'document-releases').iterdir())
    for token, oid in ((s['outsider'], None), (s['editor'], s['oid'])):
        result = c.post('/portal/api/libraries', headers=headers(token), json={'name': 'Forbidden', 'organization_id': oid, 'template': s['template']})
        assert result.status_code == 403, result.text
    assert len(s['registry'].libraries()) == before
    assert set((s['registry'].pool.root / 'document-releases').iterdir()) == dirs
    for token in (s['editor'], s['publisher'], s['outsider']):
        assert c.put(s['base'] + '/plugin-settings', headers=headers(token), json=s['template']).status_code == 403
    assert c.post('/portal/api/libraries', headers=headers(s['owner']), json={'name': 'Private template', 'template': s['template']}).status_code == 201


def test_model_authorization_and_redaction(scenario):
    s = scenario; c = s['c']
    model = {'id': 'synthetic', 'name': 'Synthetic model', 'base_url': 'http://127.0.0.1:9100/v1', 'model': 'fake', 'api_key_env': 'SYNTHETIC_API_KEY'}
    assert c.put('/admin/api/models/synthetic', headers=headers(ADMIN), json=model).status_code == 200
    config = dict(s['template'], model_connection_id='synthetic')
    # A grant to the admin's personal library cannot be spent by their organization.
    path = '/admin/api/resource-access/model/synthetic'
    c.put(path, headers=headers(ADMIN), json={'approved': True, 'user_ids': [s['owner_id']]})
    assert c.put(s['base'] + '/plugin-settings', headers=headers(s['owner']), json=config).status_code == 403
    c.put(path, headers=headers(ADMIN), json={'approved': True, 'organization_ids': [s['oid']]})
    assert c.put(s['base'] + '/plugin-settings', headers=headers(s['owner']), json=config).status_code == 200
    resources = c.get('/portal/api/resources?library_id=' + s['lid'], headers=headers(s['owner'])).json()
    assert resources['models'] == [{'id': 'synthetic', 'name': 'Synthetic model', 'model': 'fake'}]
    assert 'SYNTHETIC_API_KEY' not in json.dumps(resources)
    assert c.put('/admin/api/resource-access/plugin/reference-docs@0.2.0', headers=headers(s['owner']), json={}).status_code == 401
    assert c.put(s['resource'], headers=headers(ADMIN), json={'model_environment': {'PATH': 'api_key'}}).status_code == 422


def test_overlapping_foreign_library_blocks_delegated_updates(scenario):
    s = scenario; c = s['c']; registry = s['registry']
    with registry.store.transaction() as db:
        root = db.execute('SELECT path FROM libraries WHERE id=?', (s['lid'],)).fetchone()[0]
    (registry.pool.root / root / 'nested').mkdir()
    registry.save_library('foreign', 'Synthetic foreign scope', root + '/nested', True, create=True)
    with registry.store.transaction(write=True) as db:
        db.execute('INSERT INTO library_ownership VALUES (?,?,NULL,0)', ('foreign', s['outsider_id']))
    assert c.post(s['base'] + '/update-jobs', headers=headers(s['owner']), json={}).status_code == 403
    from folder_kb.errors import KnowledgeError
    with pytest.raises(KnowledgeError, match='其他归属'):
        with registry.store.transaction() as db:
            TemplateWorker(registry).updates.context(db, s['lid'])


def test_mixed_human_agent_manual_review_roundtrip(scenario):
    s = scenario; c = s['c']; base = s['base']
    human = material(s)
    assert material(s)['duplicate'] is True
    key = s['registry'].create_key('synthetic-agent', [s['lid']], None)
    from folder_kb.inbox import Inbox
    inbox = Inbox(s['registry'])
    inbox.grants(key['key']['id'], [s['lid']])
    agent = inbox.submit(key['token'], Material(library_id=s['lid'], submission_id='agent-one', content={'text': 'Another synthetic fact.'}))
    assert c.get(base + '/inbox', headers=headers(s['editor'])).json()['items'][0]['submitted_by']['type'] == 'user'
    assert len(c.get(base + '/inbox', headers=headers(s['editor'])).json()['items']) == 1
    assert c.get(base + '/inbox/' + agent['id'], headers=headers(s['editor'])).status_code == 404
    assert len(c.get(base + '/inbox', headers=headers(s['publisher'])).json()['items']) == 2
    assert c.post(base + '/update-jobs', headers=headers(s['editor']), json={}).status_code == 403
    job = c.post(base + '/update-jobs', headers=headers(s['publisher']), json={}).json()
    worker = TemplateWorker(s['registry']); worker.tick()
    assert not worker.tick()  # Waits for an explicit review.
    preview = c.get(base + '/update-jobs/' + job['id'] + '/preview', headers=headers(s['publisher']))
    assert preview.status_code == 200 and preview.json()['changes']
    assert c.post(base + '/update-jobs/' + job['id'] + '/approve', headers=headers(s['publisher'])).status_code == 200
    worker.tick()
    assert c.get(base + '/update-jobs/' + job['id'], headers=headers(s['publisher'])).json()['status'] == 'published'
    assert inbox.get(human['id'])['result']['published'] is True
    assert inbox.get(agent['id'])['result']['published'] is True
    assert c.get(base + '/browse', headers=headers(s['editor'])).status_code == 200


@pytest.mark.parametrize('revoked', ['plugin', 'submitter', 'reviewer', 'model'])
def test_revoke_between_approval_and_publication(scenario, revoked):
    s = scenario; c = s['c']
    if revoked == 'model':
        c.put('/admin/api/models/fake', headers=headers(ADMIN), json={'id': 'fake', 'name': 'Fake', 'model': 'fake', 'base_url': 'http://127.0.0.1:9100'})
        c.put('/admin/api/resource-access/model/fake', headers=headers(ADMIN), json={'approved': True, 'organization_ids': [s['oid']]})
        assert c.put(s['base'] + '/plugin-settings', headers=headers(s['owner']), json=dict(s['template'], model_connection_id='fake')).status_code == 200
    _, job, worker = candidate(s)
    action = s['base'] + '/update-jobs/' + job['id']
    assert c.post(action + '/approve', headers=headers(s['publisher'])).status_code == 200
    if revoked in ('plugin', 'model'):
        path = s['resource'] if revoked == 'plugin' else '/admin/api/resource-access/model/fake'
        assert c.put(path, headers=headers(ADMIN), json={'approved': False}).status_code == 200
    else:
        uid = s['editor_id'] if revoked == 'submitter' else s['publisher_id']
        assert c.put(s['base'] + '/members', headers=headers(s['owner']), json={'user_id': uid, 'role': 'reader'}).status_code == 200
    worker.tick()
    job = c.get(action, headers=headers(s['owner'])).json()
    assert job['status'] == 'failed', job
    assert job['error_code'] in ('resource_forbidden', 'submission_unavailable', 'library_forbidden')


def test_cross_library_ids_and_configuration_races(scenario):
    s = scenario; c = s['c']; base = s['base']
    receipt, job, _ = candidate(s)
    other = library(c, s['owner'])
    for suffix in ('', '/preview'):
        assert c.get(f'/portal/api/libraries/{other}/update-jobs/{job["id"]}{suffix}', headers=headers(s['owner'])).status_code == 404
    assert c.get(f'/portal/api/libraries/{other}/inbox/{receipt["id"]}', headers=headers(s['owner'])).status_code == 404
    assert c.put(base + '/plugin-settings', headers=headers(s['owner']), json=s['template']).status_code == 409
    assert c.post(base + '/update-jobs/' + job['id'] + '/reject', headers=headers(s['publisher'])).status_code == 200
    assert c.put(base + '/plugin-settings', headers=headers(s['owner']), json=s['template']).status_code == 200


def test_initialize_and_failed_retry(scenario, monkeypatch):
    s = scenario; c = s['c']
    jobs = s['base'] + '/update-jobs'
    state = c.get(jobs, headers=headers(s['publisher'])).json()
    assert state['can_run'] and state['can_initialize']
    job = c.post(jobs, headers=headers(s['publisher']), json={'operation': 'initialize'}).json()
    monkeypatch.delenv('KB_TEMPLATE_TRUSTED_PYTHON')
    worker = TemplateWorker(s['registry']); worker.tick()
    assert c.get(jobs + '/' + job['id'], headers=headers(s['publisher'])).json()['status'] == 'failed'
    monkeypatch.setenv('KB_TEMPLATE_TRUSTED_PYTHON', '1')
    retry = c.post(jobs + '/' + job['id'] + '/retry', headers=headers(s['publisher']))
    assert retry.status_code == 200, retry.text
    worker.tick()
    assert c.get(jobs + '/' + retry.json()['id'], headers=headers(s['publisher'])).json()['status'] == 'candidate_ready'


def test_v11_migration_preserves_agent_inbox_and_job_links(scenario):
    from folder_kb.inbox import Inbox, INBOX_SCHEMA
    from folder_kb.storage import SqliteStore
    s = scenario; registry = s['registry']
    key = registry.create_key('migration-agent', [s['lid']], None)
    inbox = Inbox(registry); inbox.grants(key['key']['id'], [s['lid']])
    item = inbox.submit(key['token'], Material(library_id=s['lid'], submission_id='old-item', content={'text': 'Synthetic legacy material'}))
    job = s['c'].post(s['base'] + '/update-jobs', headers=headers(s['publisher']), json={}).json()
    # Recreate exactly the v11 table shape with pending work and foreign-key references.
    with registry.store.connection() as db:
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('BEGIN IMMEDIATE')
        columns = ','.join(r['name'] for r in db.execute('PRAGMA table_info(inbox_items)') if r['name'] != 'user_id')
        db.execute(INBOX_SCHEMA[1].replace('IF NOT EXISTS inbox_items', 'inbox_v11'))
        db.execute(f'INSERT INTO inbox_v11 SELECT {columns} FROM inbox_items')
        db.execute('DROP TABLE inbox_items')
        db.execute('ALTER TABLE inbox_v11 RENAME TO inbox_items')
        db.execute(INBOX_SCHEMA[2])
        db.execute('ALTER TABLE update_jobs DROP COLUMN approved_by')
        db.execute('DROP TABLE delegated_libraries')
        db.execute('DROP TABLE resource_access')
        db.execute('PRAGMA user_version=11')
        db.commit()
    migrated = SqliteStore(registry.directory)
    with migrated.transaction() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 12
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
        row = db.execute('SELECT * FROM inbox_items WHERE id=?', (item['id'],)).fetchone()
        assert row['key_id'] == key['key']['id'] and row['user_id'] is None
        assert db.execute('SELECT item_id FROM update_job_items WHERE job_id=?', (job['id'],)).fetchone()[0] == item['id']
    TemplateWorker(registry).tick()
    assert s['c'].get(s['base'] + '/update-jobs/' + job['id'], headers=headers(s['publisher'])).json()['status'] == 'candidate_ready'
