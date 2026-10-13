import os
import threading
import time

from fastapi.testclient import TestClient
import pytest

from folder_kb.management import Registry
from folder_kb.repositories import RepositoryCatalog
from folder_kb.server import Settings, create_app
from folder_kb.service import child_environment, supervise
from folder_kb.worker_runtime import liveness, worker_heartbeat, worker_lock
from folder_kb.workers import worker_status


@pytest.fixture
def registry(tmp_path):
    root = tmp_path/'knowledge'
    root.mkdir()
    return Registry(str(root), str(tmp_path/'state'))


def test_status_authentication_counts_and_failure_without_content(registry):
    app = create_app(Settings(str(registry.pool.root), state_dir=str(registry.directory),
                             admin_token='test-worker-admin-long-enough', allowed_hosts=('testserver',)))
    client = TestClient(app)
    assert client.get('/admin/api/workers').status_code == 401
    repo = RepositoryCatalog(registry).create('Public docs', 'https://example.com/docs.git', 'main')
    with registry.store.transaction(write=True) as db:
        db.execute("UPDATE repository_jobs SET status='failed',error='branch_not_found',finished_at='2026-10-13T00:00:00+00:00'")
    response = client.get('/admin/api/workers', headers={'Authorization': 'Bearer test-worker-admin-long-enough'})
    assert response.status_code == 200
    workers = response.json()['workers']
    assert workers[0]['status'] == 'offline'
    assert workers[0]['failed'] == 1
    assert workers[0]['recent_failures'][0]['resource_id'] == repo['id']
    assert 'payload' not in response.text
    RepositoryCatalog(registry).enqueue(repo['id'])
    assert worker_status(registry)['workers'][0]['queued'] == 1


@pytest.mark.parametrize('value', [None, '', 'bad', 'NaN', 'inf', '2026-10-13T00:00:00', '10000000000000000000'])
def test_bad_heartbeat_is_offline(value):
    assert liveness(value, now=100)['status'] == 'offline'


def test_heartbeat_expiry_and_iso_compatibility():
    assert liveness('90', now=100)['status'] == 'online'
    assert liveness('70', now=100)['status'] == 'offline'
    assert liveness('1970-01-01T00:01:30+00:00', now=100)['status'] == 'online'
    assert liveness('106', now=100)['status'] == 'offline'


def test_heartbeat_during_work_and_cleanup(registry):
    with worker_lock(registry.directory), worker_heartbeat(registry.store, 'git', interval=.02):
        first = worker_status(registry)['workers'][0]['last_seen']
        deadline = time.monotonic() + 2
        while worker_status(registry)['workers'][0]['last_seen'] == first and time.monotonic() < deadline:
            time.sleep(.02)
        assert worker_status(registry)['workers'][0]['last_seen'] != first
        with pytest.raises(RuntimeError):
            with worker_lock(registry.directory):
                pass
    assert worker_status(registry)['workers'][0]['status'] == 'offline'


def test_child_environments_limit_credentials():
    source = dict(PATH='bin', KB_ROOT='/root', KB_STATE_DIR='/state', KB_ADMIN_TOKEN='admin-secret',
                  KB_TEMPLATE_TRUSTED_PYTHON='1', KB_MODEL_KEY_ALLOWLIST='MODEL_KEY', MODEL_KEY='model-secret',
                  OTHER_SECRET='other', KB_GIT_CREDENTIALS_DIR='/credentials')
    http, git, updates = (child_environment(name, source) for name in ('http', 'git', 'updates'))
    assert http['KB_ADMIN_TOKEN'] == 'admin-secret'
    assert 'MODEL_KEY' not in http and 'KB_GIT_CREDENTIALS_DIR' not in http
    assert git['KB_GIT_CREDENTIALS_DIR'] == '/credentials'
    assert 'KB_ADMIN_TOKEN' not in git and 'MODEL_KEY' not in git
    assert updates['MODEL_KEY'] == 'model-secret'
    assert 'KB_ADMIN_TOKEN' not in updates and 'OTHER_SECRET' not in updates


def test_supervisor_stops_other_children_on_failure(tmp_path, monkeypatch):
    from folder_kb import service
    children, stopped = [], []
    class Process:
        pid = 123
        returncode = 1
        def __init__(self, *args, **kwargs):
            self.number = len(children)
            children.append(self)
        def poll(self):
            return 1 if self.number == 1 else None
    monkeypatch.setattr(service.subprocess, 'Popen', Process)
    monkeypatch.setattr(service, 'stop_process', lambda p: stopped.append(p))
    env = dict(KB_ROOT='/root', KB_STATE_DIR='/state')
    assert supervise([('http', ['http']), ('git', ['git']), ('updates', ['updates'])], env, tmp_path, threading.Event()) == 1
    assert stopped == list(reversed(children))


def test_supervisor_partial_start_failure_cleans_up(tmp_path, monkeypatch):
    from folder_kb import service
    stopped = []
    class Process:
        pid = 123
    calls = []
    def start(*args, **kwargs):
        if calls:
            raise OSError('Cannot launch')
        calls.append(Process())
        return calls[0]
    monkeypatch.setattr(service.subprocess, 'Popen', start)
    monkeypatch.setattr(service, 'stop_process', lambda p: stopped.append(p))
    with pytest.raises(OSError):
        supervise([('http', ['http']), ('git', ['git'])], dict(KB_ROOT='/root', KB_STATE_DIR='/state'), tmp_path, threading.Event())
    assert stopped == calls
