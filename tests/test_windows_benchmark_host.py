import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SPEC = importlib.util.spec_from_file_location('windows_host', Path(__file__).resolve().parents[1] / 'ops/windows_benchmark_host.py')
host = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(host)
pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows process handles and console isolation')


def test_handle_rejects_reused_pid_and_retains_real_exit_status():
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(.3); raise SystemExit(7)'],
                             creationflags=subprocess.CREATE_NO_WINDOW)
    handle = host.ProcessHandle(child.pid)
    try:
        with pytest.raises(ValueError, match='creation time'):
            host.ProcessHandle(child.pid, handle.created + 1)
        assert handle.wait(10000) == 7
        assert child.wait() == 7
    finally:
        handle.close()
        if child.poll() is None:
            child.kill()
            child.wait()


def test_supervisor_child_has_no_console_and_failure_is_not_retried(tmp_path):
    events = []
    command = [sys.executable, '-c', 'import ctypes; print(bool(ctypes.windll.kernel32.GetConsoleWindow())); raise SystemExit(9)']
    assert host.supervise(tmp_path, command, lambda stage, **v: events.append(stage), .01) == 9
    assert set(events) == {'workflow'}
    assert (tmp_path / 'worker.stdout.log').read_text().strip() == 'False'


@pytest.mark.parametrize('exit_code', [0, 7])
def test_adoption_checks_index_and_exit_status_and_keeps_plan(tmp_path, exit_code):
    (tmp_path / 'agent/execution/episodes').mkdir(parents=True)
    plan = tmp_path / 'workflow-plan.json'
    plan.write_text('{"frozen": true}')
    (tmp_path / 'workflow.json').write_text('{"stage": "prepare", "state": "running"}')
    script = """import json,os,sys,time
from pathlib import Path
root=Path(sys.argv[1]); status=root/'agent/status.json'
status.write_text(json.dumps(dict(state='preparing',worker_pid=os.getpid())))
while not (root/'continue').exists(): time.sleep(.01)
(root/'agent/index.json').write_text('{}')
temp=status.with_suffix('.tmp');temp.write_text(json.dumps(dict(state='index_ready')));temp.replace(status)
raise SystemExit(int(sys.argv[2]))
"""
    child = subprocess.Popen([sys.executable, '-c', script, str(tmp_path), str(exit_code)], creationflags=subprocess.CREATE_NO_WINDOW)
    process = None
    try:
        import time
        for _ in range(200):
            if (tmp_path / 'agent/status.json').is_file():
                break
            time.sleep(.01)
        actual_pid = json.loads((tmp_path / 'agent/status.json').read_text())['worker_pid']
        process = host.ProcessHandle(actual_pid)
        pulses = []
        def pulse(stage, **values):
            pulses.append(stage)
            (tmp_path / 'continue').touch()
        if exit_code:
            with pytest.raises(RuntimeError, match='exit 7'):
                host.adopt_prepare(tmp_path, actual_pid, process.created, pulse, poll_ms=10)
            assert not (tmp_path / 'adoption.json').exists()
            assert json.loads((tmp_path / 'workflow.json').read_text())['state'] == 'interrupted'
        else:
            host.adopt_prepare(tmp_path, actual_pid, process.created, pulse, poll_ms=10)
            assert json.loads((tmp_path / 'adoption.json').read_text())['index_ready']
        assert child.wait(timeout=5) == exit_code
        assert pulses
        assert plan.read_text() == '{"frozen": true}'
        assert len(list(tmp_path.glob('workflow-before-adoption-*'))) == 1
    finally:
        if process:
            process.close()
        if child.poll() is None:
            child.kill(); child.wait()
