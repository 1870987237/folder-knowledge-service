"""Console-free, one-shot Windows host; run directly with pythonw.exe.

The scheduler must invoke pythonw, not a PowerShell wrapper. Children use
CREATE_NO_WINDOW. This host never retries an episode or modifies a frozen plan.
Optional adoption waits for an already-running prepare-only process by HANDLE
and creation time, so PID reuse cannot attach an unrelated process.
"""
import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from benchmarks.browsecomp.full_prepare import atomic_json
from folder_kb.worker_runtime import worker_lock


class ProcessHandle:
    def __init__(self, pid, expected_created=None):
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.api.OpenProcess.restype = wintypes.HANDLE
        self.api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        self.api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.api.WaitForSingleObject.restype = wintypes.DWORD
        self.api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.api.OpenProcess(0x100000 | 0x1000, False, pid)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not self.api.GetProcessTimes(self.handle, *(ctypes.byref(t) for t in times)):
                raise ctypes.WinError(ctypes.get_last_error())
            self.created = times[0].dwLowDateTime | (times[0].dwHighDateTime << 32)
            if expected_created is not None and self.created != expected_created:
                raise ValueError('Process creation time changed; refusing PID reuse')
        except BaseException:
            self.close()
            raise

    def wait(self, milliseconds):
        state = self.api.WaitForSingleObject(self.handle, milliseconds)
        if state == 258:
            return None
        if state != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        code = wintypes.DWORD()
        if not self.api.GetExitCodeProcess(self.handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return code.value

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def adopt_prepare(root, pid, created, pulse, poll_ms=5000):
    """Observe an existing prepare process; never stop it if this host fails."""
    with worker_lock(root, 'workflow.lock'):
        status_path = root / 'agent/status.json'
        previous = read(root / 'workflow.json')
        child = read(status_path)
        if child.get('worker_pid') != pid or child.get('state') != 'preparing':
            raise ValueError('Adoption requires the recorded preparing worker')
        if previous.get('stage') != 'prepare':
            raise ValueError('Only preparation can be adopted')
        if any((root / 'agent/execution/episodes').iterdir()):
            raise ValueError('Cannot adopt preparation after answers started')
        process = ProcessHandle(pid, created)
        try:
            atomic_json(root / f'workflow-before-adoption-{time.time_ns()}.json', previous)
            while True:
                child = read(status_path)
                state = dict(previous, state='running', stage='prepare', worker_pid=os.getpid(),
                    child_pid=pid, child_state=child, adopted=True, updated_at=time.time())
                atomic_json(root / 'workflow.json', state)
                pulse('waiting_for_existing_prepare', child_pid=pid, child_state=child)
                code = process.wait(poll_ms)
                if code is not None:
                    break
            child = read(status_path)
            if code != 0 or child.get('state') != 'index_ready' or not (root / 'agent/index.json').is_file():
                raise RuntimeError(f'Adopted preparation did not publish index (exit {code})')
            atomic_json(root / 'adoption.json', dict(pid=pid, created=created, exit_code=code,
                index_ready=True, finished_at=time.time()))
        except BaseException as exc:
            atomic_json(root / 'workflow.json', dict(previous, state='interrupted',
                error_type=type(exc).__name__, updated_at=time.time()))
            raise
        finally:
            process.close()


def supervise(root, command, pulse, poll_seconds=5):
    with (root / 'worker.stdout.log').open('ab') as out, (root / 'worker.stderr.log').open('ab') as err:
        child = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
            stdout=out, stderr=err, env=dict(os.environ, PYTHONIOENCODING='utf-8'),
            creationflags=subprocess.CREATE_NO_WINDOW)
        while True:
            pulse('workflow', child_pid=child.pid)
            try:
                return child.wait(timeout=poll_seconds)
            except subprocess.TimeoutExpired:
                pass


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--python', type=Path, required=True)
    p.add_argument('--omp', required=True)
    p.add_argument('--models-config', type=Path, required=True)
    p.add_argument('--wait-pid', type=int)
    p.add_argument('--wait-created', type=int)
    p.add_argument('--resume', action='store_true')
    args = p.parse_args()
    if os.name != 'nt':
        p.error('Windows host requires Windows')
    if bool(args.wait_pid) != bool(args.wait_created):
        p.error('Both --wait-pid and --wait-created are required for adoption')
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    # pythonw has no stdout/stderr: retain diagnostics without creating a console.
    with (root / 'host.log').open('a', encoding='utf-8', buffering=1) as log:
        sys.stdout = sys.stderr = log
        with worker_lock(root, 'host.lock'):
            def pulse(stage, **values):
                atomic_json(root / 'host.json', dict(state='running', stage=stage,
                    pid=os.getpid(), console_attached=bool(ctypes.windll.kernel32.GetConsoleWindow()),
                    updated_at=time.time(), **values))
            try:
                if ctypes.windll.kernel32.GetConsoleWindow():
                    raise RuntimeError('Use pythonw directly; host must have no console')
                if args.wait_pid and not (root / 'adoption.json').exists():
                    adopt_prepare(root, args.wait_pid, args.wait_created, pulse)
                command = [str(args.python.resolve()), '-B', '-m', 'benchmarks.browsecomp.workflow',
                    '--output', str(root), '--omp', args.omp, '--models-config', str(args.models_config.resolve())]
                if args.resume:
                    command.append('--resume')
                code = supervise(root, command, pulse)
                atomic_json(root / 'host.json', dict(state='completed' if code == 0 else 'interrupted',
                    pid=os.getpid(), returncode=code, updated_at=time.time()))
                return code
            except BaseException as exc:
                traceback.print_exc()
                atomic_json(root / 'host.json', dict(state='interrupted', pid=os.getpid(),
                    error_type=type(exc).__name__, updated_at=time.time()))
                return 1


if __name__ == '__main__':
    raise SystemExit(main())
