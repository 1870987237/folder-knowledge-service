import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from folder_kb.errors import KnowledgeError
from folder_kb.inbox import Inbox, Material
from folder_kb.server import Settings, create_app
from folder_kb.template_package import build_package
from folder_kb.templates import TemplateBinding, TemplateRegistration
from folder_kb.template_worker import TemplateWorker
from folder_kb.updates import ModelConnection, UpdatePolicy, Updates
from folder_kb.workspaces import WorkspaceSpec


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root, state = tmp_path/'knowledge', tmp_path/'state'
    (root/'memory/knowledge').mkdir(parents=True)
    (root/'memory/knowledge/INDEX.md').write_text('# Initial\n', encoding='utf-8')
    (root/'memory/.env').write_text('PRIVATE=keep', encoding='utf-8')
    app = create_app(Settings(str(root), state_dir=str(state), admin_token='test-only-admin-long-enough', allowed_hosts=('testserver',)))
    registry = app.state.registry
    registry.save_library('memory','Memory','memory/knowledge',True,create=True)
    updates, worker, inbox = Updates(registry), TemplateWorker(registry), Inbox(registry)
    examples = Path(__file__).resolve().parents[1]/'examples'
    definition = json.loads((examples/'template-platform-config.json').read_text(encoding='utf-8'))
    archive = build_package(examples/'templates/reference-docs')
    definition['source'] = {'archive_sha256': hashlib.sha256(archive).hexdigest()}
    updates.catalog.register(TemplateRegistration(**definition))
    installed = updates.catalog.install(definition['id'],definition['version'],archive)
    updates.catalog.bind('memory', TemplateBinding(template_id=definition['id'],template_version=definition['version'],workspace=WorkspaceSpec(type='local',root_path='memory',knowledge_subpath='knowledge')))
    updates.save_policy('memory', UpdatePolicy(enabled=True,approved_package_sha256=installed['package_sha256']))
    key = registry.create_key('test-agent',['memory'],None)
    inbox.grants(key['key']['id'],['memory'])
    monkeypatch.setenv('KB_TEMPLATE_TRUSTED_PYTHON','1')
    return app, registry, updates, worker, inbox, key


def submit(setup, identifier='one'):
    _, _, _, _, inbox, key = setup
    return inbox.submit(key['token'],Material(submission_id=identifier,library_id='memory',content={'text':'A synthetic fact for the protocol test.'}))


def candidate(setup):
    receipt = submit(setup)
    _, _, updates, worker, _, _ = setup
    assert worker.tick()  # durable auto scheduling
    assert worker.tick()  # program execution
    job = updates.listing()['jobs'][0]
    assert job['status']=='candidate_ready',job
    return receipt,job


def test_manual_review_roundtrip_and_second_update(setup):
    app, registry, updates, worker, inbox, key = setup
    receipt, job = candidate(setup)
    assert registry.libraries()[0]['path']=='memory/knowledge'
    assert inbox.get(receipt['id'],key['token'])['update_job']['status']=='candidate_ready'
    with pytest.raises(KnowledgeError,match='update job'):
        inbox.claim(receipt['id'],30)
    assert updates.preview(job['id'])['changes']
    assert not worker.tick()  # no auto publication
    updates.act(job['id'],'approve')
    worker.tick()
    assert updates.get(job['id'])['status']=='published'
    root = updates.catalog.snapshot_source('memory')['root']
    assert (root/'.env').read_text()=='PRIVATE=keep'
    assert (root/'knowledge/INDEX.md').is_file()
    assert inbox.get(receipt['id'])['result']['published'] is True
    assert submit(setup)['duplicate'] is True
    assert not worker.tick()
    submit(setup,'two')
    worker.tick(); worker.tick()
    second=updates.listing()['jobs'][0]
    assert second['status']=='candidate_ready'
    updates.act(second['id'],'approve'); worker.tick()
    assert updates.get(second['id'])['status']=='published'
    assert (root/'knowledge/INDEX.md').is_file()  # old release retained


@pytest.mark.parametrize('change,code', [('source','workspace_changed'),('candidate','candidate_changed'),('result','candidate_changed'),('permission','submission_unavailable')])
def test_recheck_before_publication(setup,change,code):
    _, registry, updates, worker, inbox, key=setup
    receipt,job=candidate(setup)
    updates.act(job['id'],'approve')
    if change=='source':
        (registry.pool.root/'memory/knowledge/INDEX.md').write_text('Externally changed')
    elif change=='candidate':
        (updates.jobs_root/job['id']/'output/workspace/knowledge/INDEX.md').write_text('Tampered')
    elif change=='result':
        p=updates.jobs_root/job['id']/'output/result.json'
        value=json.loads(p.read_text(encoding='utf-8')); value['summary']='Tampered'
        p.write_text(json.dumps(value))
    else:
        inbox.grants(key['key']['id'],[])
    worker.tick()
    assert updates.get(job['id'])['error_code']==code
    assert registry.libraries()[0]['path']=='memory/knowledge'
    assert inbox.get(receipt['id'])['status']=='pending'


def test_failure_explicit_retry_and_no_hot_loop(setup,monkeypatch):
    _,_,updates,worker,inbox,_=setup
    receipt=submit(setup)
    monkeypatch.delenv('KB_TEMPLATE_TRUSTED_PYTHON')
    worker.tick(); worker.tick()
    job=updates.listing()['jobs'][0]
    assert job['error_code']=='template_runtime_disabled'
    assert not worker.tick()
    monkeypatch.setenv('KB_TEMPLATE_TRUSTED_PYTHON','1')
    retried=updates.act(job['id'],'retry')
    worker.tick()
    assert updates.get(retried['id'])['status']=='candidate_ready'
    updates.act(retried['id'],'reject')
    assert inbox.get(receipt['id'])['result']['published'] is False


def test_credentials_only_in_child_environment(setup,monkeypatch):
    _,registry,updates,worker,_,_=setup
    updates.save_model(ModelConnection(id='example',name='Example',base_url='http://127.0.0.1:9000/v1',model='test',api_key_env='TEST_MODEL_KEY'))
    policy=UpdatePolicy(**updates.policy('memory')); policy.model_connection_id='example'
    policy.model_environment={'CLIENT_BASE_URL':'base_url','CLIENT_MODEL':'model','CLIENT_API_KEY':'api_key'}
    updates.save_policy('memory',policy)
    with registry.store.transaction() as db:
        context=updates.context(db,'memory')
    monkeypatch.setenv('TEST_MODEL_KEY','synthetic-secret-value')
    monkeypatch.setenv('KB_ADMIN_TOKEN','other-secret-value')
    with pytest.raises(KnowledgeError):
        worker.environment(context)
    monkeypatch.setenv('KB_MODEL_KEY_ALLOWLIST','TEST_MODEL_KEY')
    env=worker.environment(context)
    assert env['CLIENT_API_KEY']=='synthetic-secret-value'
    assert 'KB_ADMIN_TOKEN' not in env and 'TEST_MODEL_KEY' not in env
    assert 'synthetic-secret-value' not in json.dumps(context)
    _,job=candidate(setup)
    assert all(b'synthetic-secret-value' not in p.read_bytes() for p in (updates.jobs_root/job['id']).rglob('*') if p.is_file())


def test_admin_routes_and_context_changes(setup):
    app,_,updates,worker,_,_=setup
    _,job=candidate(setup)
    with TestClient(app) as client:
        assert client.get('/admin/api/update-jobs').status_code==401
        h={'Authorization':'Bearer test-only-admin-long-enough'}
        assert client.get('/admin/api/update-jobs',headers=h).json()['jobs'][0]['id']==job['id']
        assert client.get('/admin/api/update-jobs/'+job['id']+'/preview',headers=h).status_code==200
        policy=updates.policy('memory'); policy['auto_trigger']=False
        assert client.put('/admin/api/libraries/memory/update-policy',headers=h,json=policy).status_code==200
        result=client.post('/admin/api/update-jobs/'+job['id']+'/approve',headers=h)
        assert result.status_code==409 and result.json()['error']=='stale_update_context'


def test_recovery_preserves_review_but_releases_interrupted_jobs(setup):
    _,registry,updates,worker,inbox,_=setup
    receipt,job=candidate(setup)
    worker.recover()
    assert updates.get(job['id'])['status']=='candidate_ready'
    with registry.store.transaction(write=True) as db:
        db.execute("UPDATE update_jobs SET status='running' WHERE id=?",(job['id'],))
    worker.recover()
    assert updates.get(job['id'])['error_code']=='worker_interrupted'
    assert inbox.get(receipt['id'])['status']=='pending'


def test_revoked_pending_item_does_not_starve_other_submitters(setup):
    _,registry,updates,worker,inbox,key=setup
    old=submit(setup)
    inbox.grants(key['key']['id'],[])
    other=registry.create_key('second-agent',['memory'],None)
    inbox.grants(other['key']['id'],['memory'])
    new=inbox.submit(other['token'],Material(submission_id='next',library_id='memory',content={'text':'Another authorized fact'}))
    worker.tick(); worker.tick()
    assert updates.listing()['jobs'][0]['status']=='candidate_ready'
    assert inbox.get(new['id'])['update_job']
    assert inbox.get(old['id'])['update_job'] is None


def test_workspace_serialization_and_required_secrets(setup):
    _,_,updates,worker,_,_=setup
    _,job=candidate(setup)
    submit(setup,'two')
    with pytest.raises(KnowledgeError,match='current workspace job'):
        updates.enqueue('memory')
    context={'template':{'definition':{'runtime':{'type':'trusted-python-demo'},'required_secrets':['MISSING_API_KEY']}},'model':None}
    with pytest.raises(KnowledgeError,match='credentials'):
        worker.environment(context)


def test_timeout_terminates_child(setup):
    import subprocess
    import sys
    import time
    _,registry,_,worker,_,_=setup
    started=time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        worker.run([sys.executable,'-c','import time; time.sleep(30)'],registry.directory,{},.1)
    assert time.monotonic()-started<10


