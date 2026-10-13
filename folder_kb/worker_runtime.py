"""Process lock and liveness only; business queues belong to their workers."""
from contextlib import contextmanager
from datetime import datetime, timezone
import logging
import os
import threading
import time


HEARTBEATS = {'git': 'git_worker_heartbeat', 'updates': 'template_worker_heartbeat'}
STALE_SECONDS = 30


@contextmanager
def worker_lock(directory, filename='git-worker.lock'):
    with (directory / filename).open('a+b') as handle:
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b'0'); handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError('已有进程持有 worker 锁') from None
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError('已有进程持有 worker 锁') from None
        try:
            yield
        finally:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def heartbeat_time(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value)
            return parsed.timestamp() if parsed.tzinfo else None
        except (TypeError, ValueError, OverflowError):
            return None


def liveness(value, now=None):
    stamp = heartbeat_time(value)
    now = time.time() if now is None else now
    if stamp is None or not 0 <= stamp <= now + 5:
        return {'status': 'offline', 'last_seen': None}
    return {'status': 'online' if now - stamp < STALE_SECONDS else 'offline',
            'last_seen': datetime.fromtimestamp(stamp, timezone.utc).isoformat()}


@contextmanager
def worker_heartbeat(store, kind, interval=5):
    """Independent heartbeat stays fresh during long copy/hash/publication steps.

    Must be entered while holding the worker's exclusive process lock.
    Liveness is not task progress; callers display both separately.
    """
    name = HEARTBEATS[kind]
    stopped = threading.Event()

    def pulse():
        value = str(time.time()) if kind == 'git' else datetime.now(timezone.utc).isoformat()
        with store.transaction(write=True) as db:
            db.execute('INSERT INTO metadata VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value',
                       (name, value))

    def loop():
        while not stopped.wait(interval):
            try:
                pulse()
            except Exception:
                # A blocked/unavailable store will make the last heartbeat stale.
                logging.getLogger(__name__).error('Worker %s heartbeat unavailable', kind)

    pulse()
    thread = threading.Thread(target=loop, name=kind+'-heartbeat', daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join()
        with store.transaction(write=True) as db:
            db.execute('DELETE FROM metadata WHERE name=?', (name,))
