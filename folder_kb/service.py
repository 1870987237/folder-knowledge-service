"""Local process supervisor. Queues and retries remain in business services.

    python -m folder_kb.service --host 127.0.0.1 --port 8791

Docker/systemd can continue to supervise the three modules separately.
"""
import argparse
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading

from .worker_runtime import worker_lock


def child_environment(component, source):
    base = {k: v for k, v in source.items() if k.upper() in ('PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'HOME')}
    base['PYTHONIOENCODING'] = 'utf-8'
    if component == 'http':
        base.update({k: v for k, v in source.items() if k.startswith('KB_') and not k.startswith(('KB_GIT_', 'KB_MODEL_', 'KB_TEMPLATE_'))})
    else:
        for key in ('KB_ROOT', 'KB_STATE_DIR'):
            base[key] = source[key]
        if component == 'git':
            base.update({k: v for k, v in source.items() if k.startswith('KB_GIT_')})
        else:
            for key in ('KB_TEMPLATE_TRUSTED_PYTHON', 'KB_MODEL_KEY_ALLOWLIST'):
                if key in source:
                    base[key] = source[key]
            for key in source.get('KB_MODEL_KEY_ALLOWLIST', '').split(','):
                key = key.strip()
                if key in source and key not in ('KB_ADMIN_TOKEN', 'KB_TOKEN'):
                    base[key] = source[key]
    return base


def stop_process(process):
    if process.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def supervise(commands, environment, logs, stopped):
    processes, handles = [], []
    try:
        for name, command in commands:
            handle = (logs / (name + '.log')).open('ab')
            handles.append(handle)
            process = subprocess.Popen(command, env=child_environment(name, environment),
                stdin=subprocess.DEVNULL, stdout=handle, stderr=handle,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            processes.append((name, process))
            print(f'{name}: started (pid {process.pid})', flush=True)
        while not stopped.wait(.5):
            for name, process in processes:
                if process.poll() is not None:
                    print(f'{name}: exited ({process.returncode}); stopping service. See {name}.log', file=sys.stderr, flush=True)
                    return 1
        return 0
    finally:
        for _, process in reversed(processes):
            stop_process(process)
        for handle in handles:
            handle.close()


def main():
    parser = argparse.ArgumentParser(description='Start HTTP, Git sync and knowledge update workers together')
    parser.add_argument('--root', default=os.getenv('KB_ROOT'))
    parser.add_argument('--state-dir', default=os.getenv('KB_STATE_DIR'))
    parser.add_argument('--host', default=os.getenv('KB_HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.getenv('KB_PORT', '8790')))
    args = parser.parse_args()
    if not args.root or not args.state_dir or not os.getenv('KB_ADMIN_TOKEN'):
        parser.error('KB_ROOT, KB_STATE_DIR and KB_ADMIN_TOKEN are required')
    if args.host not in ('127.0.0.1', 'localhost', '::1') and not os.getenv('KB_ALLOWED_HOSTS'):
        parser.error('Remote binding requires explicit KB_ALLOWED_HOSTS')
    root, state = Path(args.root).resolve(), Path(args.state_dir).resolve()
    if not root.is_dir() or root.is_relative_to(state) or state.is_relative_to(root):
        parser.error('Knowledge root must exist; root and state must be separate non-nested directories')
    # Fail before starting any worker when an older HTTP instance owns this port.
    try:
        addresses = socket.getaddrinfo(args.host, args.port, type=socket.SOCK_STREAM)
        family, socktype, proto, _, address = addresses[0]
        with socket.socket(family, socktype, proto) as check:
            if os.name == 'nt':
                check.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            check.bind(address)
    except OSError:
        parser.error('HTTP port unavailable; stop the existing service or select another port')
    state.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ, KB_ROOT=str(root), KB_STATE_DIR=str(state))
    logs = state/'service-logs'
    logs.mkdir(exist_ok=True)
    stopped = threading.Event()
    def stop(signum, frame):
        stopped.set()
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    commands = [('http', [sys.executable, '-m', 'folder_kb', '--host', args.host, '--port', str(args.port)]),
                ('git', [sys.executable, '-m', 'folder_kb.git_worker']),
                ('updates', [sys.executable, '-m', 'folder_kb.template_worker'])]
    # Initialize/migrate once before starting concurrent children.
    from .management import Registry
    Registry(str(root), str(state))
    with worker_lock(state, 'service.lock'):
        # Detect standalone workers without taking over or stopping them.
        with worker_lock(state), worker_lock(state, 'template-worker.lock'):
            pass
        return supervise(commands, environment, logs, stopped)


if __name__ == '__main__':
    raise SystemExit(main())
