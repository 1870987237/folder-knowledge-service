"""Lifecycle regressions use synthetic libraries, no live submissions or model calls."""
from folder_kb.template_worker import TemplateWorker
from tests.test_library_updates import scenario, material
from tests.test_collaboration import ADMIN, env, headers


def new_library(s, enabled=True):
    r = s['c'].post('/portal/api/libraries', headers=headers(s['owner']), json={
        'name': 'Bootstrap lifecycle', 'template': dict(s['template'], enabled=enabled)})
    assert r.status_code == 201, r.text
    return dict(s, lid=r.json()['id'], base='/portal/api/libraries/' + r.json()['id'])


def state(s):
    return s['c'].get(s['base'] + '/update-jobs', headers=headers(s['owner'])).json()


def run(s, **body):
    return s['c'].post(s['base'] + '/update-jobs', headers=headers(s['owner']), json=body)


def test_creation_initializes_before_material_update_and_keeps_review(scenario):
    s = new_library(scenario); worker = TemplateWorker(s['registry'])
    initial = state(s)
    assert initial['initialization'] == 'queued' and len(initial['jobs']) == 1
    receipt = material(s, s['owner'])
    blocked = run(s)
    assert blocked.status_code == 409 and blocked.json()['error'] == 'library_not_initialized'
    assert len(state(s)['jobs']) == 1
    assert state(s)['pending_count'] == 1
    assert worker.tick() and state(s)['initialization'] == 'publish_queued'
    assert worker.tick() and state(s)['initialization'] == 'ready'
    assert not state(s)['can_initialize'] and state(s)['can_run']
    job = run(s).json()
    assert worker.tick()
    assert worker.updates.get(job['id'])['status'] == 'candidate_ready'
    assert not worker.tick()
    with s['registry'].store.transaction() as db:
        assert db.execute('SELECT status FROM inbox_items WHERE id=?', (receipt['id'],)).fetchone()[0] == 'processing'


def test_bootstrap_failure_retry_and_no_automatic_retry_loop(scenario, monkeypatch):
    s = new_library(scenario); worker = TemplateWorker(s['registry'])
    job = state(s)['jobs'][0]
    material(s, s['owner'])
    monkeypatch.delenv('KB_TEMPLATE_TRUSTED_PYTHON')
    assert worker.tick()
    assert state(s)['initialization'] == 'required'
    assert state(s)['jobs'][0]['error_code'] == 'template_runtime_disabled'
    assert not worker.tick() and not worker.tick()
    assert len(state(s)['jobs']) == 1 and state(s)['pending_count'] == 1
    monkeypatch.setenv('KB_TEMPLATE_TRUSTED_PYTHON', '1')
    retry = s['c'].post(s['base'] + '/update-jobs/' + job['id'] + '/retry', headers=headers(s['owner']))
    assert retry.status_code == 200
    assert worker.tick() and worker.tick()
    assert state(s)['initialization'] == 'ready'


def test_seed_edit_between_generation_and_publication_blocks_auto_publish(scenario):
    s = new_library(scenario); worker = TemplateWorker(s['registry'])
    assert worker.tick()
    root = worker.updates.catalog.snapshot_source(s['lid'])['root']
    (root / 'human.md').write_text('Preserve this human edit', encoding='utf-8')
    assert worker.tick()
    assert state(s)['jobs'][0]['error_code'] == 'bootstrap_workspace_changed'
    assert (root / 'human.md').read_text(encoding='utf-8') == 'Preserve this human edit'
    assert state(s)['initialization'] == 'required'
    # Recovery is an explicit, reviewed initialization; never another automatic overwrite.
    job = run(s, operation='initialize').json()
    assert worker.tick()
    assert worker.updates.get(job['id'])['status'] == 'candidate_ready'


def test_disabled_creation_initializes_when_enabled(scenario):
    s = new_library(scenario, enabled=False)
    assert not state(s)['jobs'] and not state(s)['can_run']
    r = s['c'].put(s['base'] + '/plugin-settings', headers=headers(s['owner']), json=s['template'])
    assert r.status_code == 200, r.text
    assert state(s)['initialization'] == 'queued'
    worker = TemplateWorker(s['registry'])
    assert worker.tick() and worker.tick()
    assert state(s)['initialization'] == 'ready'


def test_creator_loses_configuration_permission_before_publication(scenario):
    s = scenario
    r = s['c'].post('/portal/api/libraries', headers=headers(s['owner']), json={
        'name': 'Organization bootstrap', 'organization_id': s['oid'], 'template': s['template']})
    assert r.status_code == 201
    lid = r.json()['id']; worker = TemplateWorker(s['registry'])
    assert worker.tick()
    with s['registry'].store.transaction(write=True) as db:
        db.execute("UPDATE organization_members SET role='member' WHERE organization_id=? AND user_id=?", (s['oid'], s['owner_id']))
    assert worker.tick()
    job = next(j for j in worker.updates.listing()['jobs'] if j['library_id'] == lid)
    assert job['status'] == 'failed' and job['error_code'] in ('configuration_forbidden', 'library_forbidden')


def test_v13_migration_does_not_auto_initialize_existing_libraries(scenario):
    from folder_kb.storage import SqliteStore
    registry = scenario['registry']
    with registry.store.transaction(write=True) as db:
        before = db.execute('SELECT count(*) FROM update_jobs').fetchone()[0]
        db.execute('DROP TABLE library_bootstrap')
        db.execute('PRAGMA user_version=13')
    migrated = SqliteStore(registry.directory)
    with migrated.transaction() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 14
        assert db.execute('SELECT count(*) FROM library_bootstrap').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM update_jobs').fetchone()[0] == before
        assert not db.execute('PRAGMA foreign_key_check').fetchall()


def test_platform_admin_can_create_an_organization_template_library(scenario):
    s = scenario
    r = s['c'].post('/portal/api/libraries', headers=headers(ADMIN), json={
        'name': 'Admin bootstrap', 'organization_id': s['oid'], 'template': s['template']})
    assert r.status_code == 201, r.text
    worker = TemplateWorker(s['registry'])
    assert worker.tick() and worker.tick()
    job = next(j for j in worker.updates.listing()['jobs'] if j['library_id'] == r.json()['id'])
    assert job['status'] == 'published'
    assert job['publish_mode'] == 'new_library_initialization'


def test_manual_process_collects_failed_materials_without_losing_history(scenario, monkeypatch):
    s = scenario; worker = TemplateWorker(s['registry'])
    monkeypatch.delenv('KB_TEMPLATE_TRUSTED_PYTHON')
    ids = []
    for number in range(4):
        material(s, s['owner'], str(number))
        # Automatic scheduling never retries the previous failed items.
        job = worker.updates.enqueue(s['lid'])
        ids.append(job['id']); assert worker.tick()
    assert state(s)['pending_count'] == 4 and state(s)['retry_count'] == 4
    assert not worker.tick()
    monkeypatch.setenv('KB_TEMPLATE_TRUSTED_PYTHON', '1')
    combined = run(s).json()
    assert worker.tick()
    assert worker.updates.get(combined['id'])['status'] == 'candidate_ready'
    with s['registry'].store.transaction() as db:
        assert db.execute('SELECT count(*) FROM update_job_items WHERE job_id=?', (combined['id'],)).fetchone()[0] == 4
        assert all(worker.updates.get(i, db)['status'] == 'failed' for i in ids)
    assert state(s)['pending_count'] == 0


def test_legacy_missing_initialization_is_actionable_before_claim(scenario):
    s = scenario; worker = TemplateWorker(s['registry'])
    # Simulate a pre-upgrade new template library: no bootstrap marker or published history.
    with s['registry'].store.transaction(write=True) as db:
        db.execute('DELETE FROM library_bootstrap WHERE library_id=?', (s['lid'],))
        db.execute("UPDATE update_jobs SET status='rejected' WHERE library_id=?", (s['lid'],))
    root = worker.updates.catalog.snapshot_source(s['lid'])['root']
    (root / 'INDEX.md').unlink()
    material(s, s['owner'])
    assert state(s)['initialization'] == 'required'
    assert run(s).json()['error'] == 'library_not_initialized'
    assert state(s)['pending_count'] == 1
