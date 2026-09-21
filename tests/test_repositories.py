import io
import json
from pathlib import Path
import subprocess
import tarfile

import pytest
from fastapi.testclient import TestClient

from folder_kb.errors import KnowledgeError
from folder_kb.git_worker import GitWorker, SyncError, worker_lock
from folder_kb.management import Registry
from folder_kb.repositories import RepositoryCatalog, remote_url
from folder_kb.server import Settings, create_app


URL = 'https://git.example.test/team/docs.git'
ADMIN = 'git-test-admin-token-with-sufficient-length'


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], stderr=subprocess.PIPE).decode().strip()


class LocalTransportWorker(GitWorker):
    """Replace only the test remote transport; all publication logic runs normally."""
    source = None

    def git(self, args, **kwargs):
        if 'fetch' in args:
            args = ['-c', 'protocol.file.allow=always', *[str(self.source) if a == URL else a for a in args]]
        return super().git(args, **kwargs)


@pytest.fixture
def setup(tmp_path):
    root = tmp_path/'knowledge'
    source = root/'checkout'
    source.mkdir(parents=True)
    git(source, 'init', '-b', 'main')
    git(source, 'config', 'user.name', 'Test')
    git(source, 'config', 'user.email', 'test@example.test')
    git(source, 'config', 'core.autocrlf', 'false')
    git(source, 'remote', 'add', 'origin', URL)
    for name in ('a','b'):
        (source/name).mkdir()
        (source/name/'guide.md').write_text('version one\n', encoding='utf-8')
    git(source, 'add', '.')
    git(source, 'commit', '-m', 'initial')
    registry = Registry(str(root),str(tmp_path/'state'))
    worker = LocalTransportWorker(registry)
    worker.source = source
    return registry, RepositoryCatalog(registry), worker, source


def published(setup):
    registry, catalog, worker, source = setup
    repo = catalog.create('Docs', URL, 'main')
    assert worker.tick()
    row = catalog.list()['repositories'][0]
    assert row['job']['status'] == 'succeeded', row['job']
    return repo['id']


def update(source, text='version two\n'):
    (source/'a/guide.md').write_text(text, encoding='utf-8')
    git(source, 'add', '.')
    git(source, 'commit', '-m', 'update')


def read(registry, token, lid='guide'):
    return registry.run(token,lid,'read_file',{'path':'guide.md'},'rest')['lines'][0]['text']


def test_publish_multiple_libraries_preserves_keys_and_inflight_reads(setup):
    r,c,w,source = setup
    rid=published(setup)
    r.save_library('guide','Existing','checkout/a',True,create=True)
    token=r.create_key('Agent',['guide'],None)['token']
    c.bind(rid,'guide','Guide','a',existing=True)
    c.bind(rid,'second','Second','b')
    before=r.libraries()
    old=next(x for x in before if x['id']=='guide')
    assert read(r,token)=='version one'
    update(source)
    c.enqueue(rid);w.tick()
    assert read(r,token)=='version two'
    assert all(a['path']!=b['path'] for a,b in zip(before,r.libraries()))
    assert r.access_library(old,'read_file',{'path':'guide.md'})['lines'][0]['text']=='version one'
    assert r.authenticate(token)['library_ids']==['guide']
    with pytest.raises(KnowledgeError,match='Git'):
        r.save_library('guide','Guide','checkout/a',True)
    c.enqueue(rid);w.tick()
    assert c.jobs(rid)['jobs'][0]['status']=='unchanged'
    c.unbind(rid,'guide')
    r.save_library('guide','Guide','checkout/a',True)


def test_missing_subdirectory_rolls_back_all_bindings(setup):
    r,c,w,source=setup;rid=published(setup)
    c.bind(rid,'guide','Guide','a');c.bind(rid,'second','Second','b')
    previous=r.libraries();commit=c.list()['repositories'][0]['current_commit']
    git(source,'rm','b/guide.md');update(source)
    c.enqueue(rid);w.tick()
    assert r.libraries()==previous
    assert c.list()['repositories'][0]['current_commit']==commit
    assert 'library_path_missing' in c.jobs(rid)['jobs'][0]['error']


def test_fetch_failure_and_history_rewrite_keep_published_version(setup):
    r,c,w,source=setup;rid=published(setup)
    c.bind(rid,'guide','Guide','a');previous=r.libraries()
    git(source,'checkout','--orphan','replacement')
    git(source,'commit','-m','unrelated history')
    git(source,'branch','-M','main')
    c.enqueue(rid);w.tick()
    assert 'history_diverged' in c.jobs(rid)['jobs'][0]['error']
    assert r.libraries()==previous
    w.source=source/'missing'
    c.enqueue(rid);w.tick()
    assert c.jobs(rid)['jobs'][0]['status']=='failed'
    assert r.libraries()==previous


def test_adopt_dirty_checkout_is_never_modified(setup):
    r,c,w,source=setup
    (source/'untracked.md').write_text('local work',encoding='utf-8')
    repo=c.create('Adopt',import_path='checkout')
    w.tick()
    assert 'local_changes' in c.jobs(repo['id'])['jobs'][0]['error']
    assert (source/'untracked.md').read_text()=='local work'
    (source/'untracked.md').unlink()
    original=git(source,'rev-parse','HEAD')
    c.enqueue(repo['id']);w.tick()
    assert c.jobs(repo['id'])['jobs'][0]['status']=='succeeded'
    assert git(source,'rev-parse','HEAD')==original
    assert not git(source,'status','--porcelain')
    git(source,'config','filter.evil.clean','echo injected')
    c.enqueue(repo['id']);w.tick()
    assert 'import_config' in c.jobs(repo['id'])['jobs'][0]['error']


def test_jobs_coalesce_schedule_pause_recovery_and_process_lock(setup):
    r,c,w,source=setup
    repo=c.create('Docs',URL,'main',interval_seconds=60);rid=repo['id']
    assert c.enqueue(rid)['id']==c.enqueue(rid)['id']
    with worker_lock(r.directory):
        with pytest.raises(SyncError):
            with worker_lock(r.directory):
                pass
    assert w.claim()
    w.recover()
    assert 'worker_interrupted' in c.jobs(rid)['jobs'][0]['error']
    with r.store.transaction(write=True) as db:
        db.execute('UPDATE repositories SET next_sync=0')
    assert w.tick()
    assert c.jobs(rid)['jobs'][0]['trigger']=='scheduled'
    c.update(rid,'Docs','',60,False)
    assert not w.tick()
    c.enqueue(rid);assert w.tick()  # Manual refresh remains available when schedule is paused.


@pytest.mark.parametrize('url',['file:///etc/passwd','/srv/repo','http://host/repo','https://secret@host/repo','https://host/repo?token=secret','ssh://git:secret@host/repo','ext::sh -c bad'])
def test_reject_unsafe_or_credential_bearing_remotes(url):
    with pytest.raises(KnowledgeError):
        remote_url(url)


@pytest.mark.parametrize('name,kind',[('../escape',tarfile.REGTYPE),('link',tarfile.SYMTYPE),('/absolute',tarfile.REGTYPE)])
def test_archive_rejects_links_and_path_escape(setup,name,kind):
    r,c,w,source=setup
    data=io.BytesIO()
    with tarfile.open(fileobj=data,mode='w') as archive:
        item=tarfile.TarInfo(name);item.type=kind;item.linkname='../escape'
        archive.addfile(item,io.BytesIO())
    data.seek(0)
    with pytest.raises(SyncError,match='unsupported_entry'):
        w.make_release('test','a'*40,data)


def test_lfs_and_snapshot_size_rejected(setup):
    r,c,w,source=setup
    (source/'large.bin').write_text('version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 100\n')
    git(source,'add','.');git(source,'commit','-m','LFS pointer')
    repo=c.create('Docs',URL,'main');w.tick()
    assert 'lfs_unsupported' in c.jobs(repo['id'])['jobs'][0]['error']
    w.max_bytes=100
    c.enqueue(repo['id']);w.tick()
    assert c.jobs(repo['id'])['jobs'][0]['status']=='failed'
    assert c.list()['repositories'][0]['release_path'] is None


def test_credentials_are_ephemeral_not_in_database(setup,tmp_path):
    r,c,w,source=setup
    credentials=tmp_path/'credentials';credentials.mkdir()
    (credentials/'reader.json').write_text(json.dumps({'type':'https_token','token_file':'token.txt','username':'oauth2'}))
    secret='test-secret-only-123456'
    (credentials/'token.txt').write_text(secret)
    w.credentials=credentials
    repo=c.create('Docs',URL,'main',credential='reader')
    with w.authenticated_env(repo) as env:
        assert env['FKB_GIT_TOKEN']==secret
        askpass=Path(env['GIT_ASKPASS']);assert askpass.exists()
        assert subprocess.check_output([str(askpass),'Username for repo:'],env=env).decode().strip()=='oauth2'
        assert subprocess.check_output([str(askpass),'Password for repo:'],env=env).decode().strip()==secret
    assert not askpass.exists()
    w.tick()
    with r.store.transaction() as db:
        assert secret not in '\n'.join(db.iterdump())


def test_admin_http_routes_and_agent_boundary(setup):
    r,c,w,source=setup
    app=create_app(Settings(str(r.pool.root),admin_token=ADMIN,state_dir=str(r.directory),allowed_hosts=('testserver',)))
    headers={'Authorization':'Bearer '+ADMIN}
    with TestClient(app) as client:
        assert client.get('/admin/assets/repositories.js').status_code==200
        assert client.post('/admin/api/repositories',json={'name':'Docs','url':URL,'branch':'main'}).status_code==401
        result=client.post('/admin/api/repositories',headers=headers,json={'name':'Docs','url':URL,'branch':'main'})
        assert result.status_code==201,result.text
        rid=result.json()['id'];w.tick()
        binding={'library_id':'guide','name':'Guide','subpath':'a'}
        assert client.post(f'/admin/api/repositories/{rid}/libraries',headers=headers,json=binding).status_code==200
        token=r.create_key('Agent',['guide'],None)['token']
        assert client.post(f'/admin/api/repositories/{rid}/sync',headers={'Authorization':'Bearer '+token}).status_code==401
        assert client.get('/api/v1/read',headers={'Authorization':'Bearer '+token},params={'path':'guide.md'}).json()['lines'][0]['text']=='version one'
        assert client.post(f'/admin/api/repositories/{rid}/sync',headers=headers).status_code==202
        assert client.get(f'/admin/api/repositories/{rid}/jobs',headers=headers).status_code==200
        assert client.delete(f'/admin/api/repositories/{rid}/libraries/guide',headers=headers).status_code==200


def test_schema_v1_migration_preserves_grants(setup):
    r,c,w,source=setup
    r.save_library('guide','Guide','checkout/a',True,create=True)
    token=r.create_key('Agent',['guide'],None)['token']
    with r.store.transaction(write=True) as db:
        for table in ('repository_libraries','repository_jobs','repositories'):
            db.execute('DROP TABLE '+table)
        db.execute('PRAGMA user_version=1')
    reopened=Registry(str(r.pool.root),str(r.directory))
    assert read(reopened,token)=='version one'
    assert RepositoryCatalog(reopened).list()['repositories']==[]
