"""File-backed administration. Knowledge content is never written here."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
import uuid

from .core import KnowledgeError, KnowledgeFolder


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


AVAILABILITY = {
    'available': '正常', 'missing': '目录不存在',
    'permission_denied': '无读取或遍历权限', 'not_directory': '路径不是目录',
    'unsafe_path': '路径不符合访问规则', 'io_error': '目录暂不可访问',
}


class LibraryUnavailable(KnowledgeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__('library_unavailable',
                         f'知识库暂不可用：{AVAILABILITY[reason]}（{reason}）', 503)


class Registry:
    def __init__(self, root: str, state_dir: str):
        self.pool = KnowledgeFolder(root)
        self.directory = Path(state_dir).resolve()
        if self.directory.is_relative_to(self.pool.root) or self.pool.root.is_relative_to(self.directory):
            raise ValueError('KB_STATE_DIR and KB_ROOT must be separate, non-nested directories')
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.mutex = threading.RLock()
        self.active_mutex = threading.Lock()
        self.active: dict[str, dict] = {}
        with self.lock():
            if not (self.directory / 'catalog.json').exists():
                self._write('catalog.json', {'version': 1, 'libraries': {}, 'keys': {}})

    @contextmanager
    def lock(self):
        with self.mutex:
            with (self.directory / 'registry.lock').open('a+b') as stream:
                stream.seek(0, 2)
                if stream.tell() == 0:
                    stream.write(b'0')
                    stream.flush()
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    stream.seek(0)
                    if os.name == 'nt':
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _load(self, name: str, default=None):
        try:
            return json.loads((self.directory / name).read_text(encoding='utf-8'))
        except FileNotFoundError:
            if default is not None:
                return default
            raise KnowledgeError('state_unavailable', 'Service configuration is unavailable', 503) from None
        except (OSError, ValueError):
            raise KnowledgeError('state_unavailable', 'Service configuration cannot be read', 503) from None

    def _write(self, name: str, value):
        target = self.directory / name
        temporary = self.directory / ('.' + name + '-' + uuid.uuid4().hex + '.tmp')
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def _catalog(self):
        value = self._load('catalog.json')
        if value.get('version') != 1 or not isinstance(value.get('libraries'), dict) or not isinstance(value.get('keys'), dict):
            raise KnowledgeError('state_unavailable', 'Unsupported service configuration', 503)
        return value

    def folder_for_path(self, path: str, name: str = 'knowledge') -> KnowledgeFolder:
        candidate = self.pool._path(path)
        if not stat.S_ISDIR(candidate.stat().st_mode):
            raise KnowledgeError('not_directory', 'A library must point to an existing directory')
        folder = KnowledgeFolder(candidate, name)
        if not folder.root.is_relative_to(self.pool.root):
            raise KnowledgeError('forbidden_path', 'Library is outside the mounted knowledge pool', 403)
        return folder

    def checked_folder(self, row):
        """Check only the library root; never scan its full contents for health."""
        try:
            folder = self.folder_for_path(row['path'], row['name'])
            # Opening and advancing scandir exercises real directory permissions.
            with os.scandir(folder.root) as entries:
                next(entries, None)
            if os.name != 'nt' and not os.access(folder.root, os.X_OK):
                raise PermissionError()
            return folder
        except KnowledgeError as exc:
            reason = {'not_found': 'missing', 'not_directory': 'not_directory',
                      'permission_denied': 'permission_denied',
                      'forbidden_path': 'unsafe_path', 'link_forbidden': 'unsafe_path',
                      'invalid_path': 'unsafe_path'}.get(exc.code, 'io_error')
        except PermissionError:
            reason = 'permission_denied'
        except FileNotFoundError:
            reason = 'missing'
        except NotADirectoryError:
            reason = 'not_directory'
        except (OSError, RuntimeError, ValueError):
            reason = 'io_error'
        raise LibraryUnavailable(reason) from None

    def availability(self, row):
        status = 'available'
        try:
            self.checked_folder(row)
        except LibraryUnavailable as exc:
            status = exc.reason
        return {'status': status, 'message': AVAILABILITY[status]}

    def access_library(self, row, tool, kwargs):
        folder = self.checked_folder(row)
        try:
            return getattr(folder, tool)(**kwargs)
        except (KnowledgeError, OSError):
            # A vanished root is different from an absent file within a healthy root.
            self.checked_folder(row)
            raise

    def directories(self, path: str = '.', offset: int = 0):
        result = self.pool.list_directory(path, offset, 100)
        result['entries'] = [entry for entry in result['entries'] if entry['type'] == 'directory']
        return result

    def save_library(self, library_id: str, name: str, path: str, enabled: bool, create: bool = False):
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', library_id):
            raise KnowledgeError('invalid_id', 'Library ID must use lowercase letters, digits, - or _')
        name = name.strip()
        if not 1 <= len(name) <= 80:
            raise KnowledgeError('invalid_name', 'Name must contain 1..80 characters')
        with self.lock():
            data = self._catalog()
            exists = library_id in data['libraries']
            if create and exists:
                raise KnowledgeError('duplicate_library', 'Library ID already exists', 409)
            if not create and not exists:
                raise KnowledgeError('unknown_library', 'Library does not exist', 404)
            previous = data['libraries'].get(library_id)
            # Metadata and enable/disable remain editable during an outage.
            if previous and path == previous['path']:
                relative = previous['path']
            else:
                folder = self.folder_for_path(path, name)
                relative = folder.root.relative_to(self.pool.root).as_posix()
            row = {'id': library_id, 'name': name, 'path': relative, 'enabled': enabled,
                   'created_at': data['libraries'].get(library_id, {}).get('created_at', now()), 'updated_at': now()}
            data['libraries'][library_id] = row
            self._write('catalog.json', data)
            self._event({'kind': 'admin', 'action': 'library_create' if create else 'library_update',
                         'library_id': library_id, 'status': 'ok'})
            return row

    def libraries(self):
        with self.lock():
            return list(self._catalog()['libraries'].values())

    @staticmethod
    def public_key(key):
        return {k: v for k, v in key.items() if k != 'digest'}

    def create_key(self, agent_name: str, library_ids: list[str], expires_at: str | None):
        agent_name = agent_name.strip()
        if not 1 <= len(agent_name) <= 80:
            raise KnowledgeError('invalid_name', 'Agent name must contain 1..80 characters')
        if not 1 <= len(library_ids) <= 64:
            raise KnowledgeError('invalid_grants', 'Choose 1..64 libraries')
        if expires_at:
            try:
                expiry = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
                if expiry.tzinfo is None or expiry.timestamp() <= time.time():
                    raise ValueError()
                expires_at = expiry.astimezone(timezone.utc).isoformat()
            except ValueError:
                raise KnowledgeError('invalid_expiry', 'Expiry must be a future timestamp with a timezone') from None
        with self.lock():
            data = self._catalog()
            if any(i not in data['libraries'] for i in library_ids):
                raise KnowledgeError('unknown_library', 'One or more libraries do not exist')
            kid = uuid.uuid4().hex[:16]
            token = 'fkb_' + kid + '_' + secrets.token_urlsafe(32)
            key = {'id': kid, 'agent_name': agent_name, 'library_ids': sorted(set(library_ids)),
                   'digest': hashlib.sha256(token.encode()).hexdigest(), 'created_at': now(),
                   'expires_at': expires_at, 'revoked_at': None}
            data['keys'][kid] = key
            self._write('catalog.json', data)
            self._event({'kind': 'admin', 'action': 'key_create', 'key_id': kid, 'status': 'ok'})
            return {'key': self.public_key(key), 'token': token}

    def revoke_key(self, kid: str):
        with self.lock():
            data = self._catalog()
            if kid not in data['keys']:
                raise KnowledgeError('unknown_key', 'Key does not exist', 404)
            data['keys'][kid]['revoked_at'] = now()
            self._write('catalog.json', data)
            self._event({'kind': 'admin', 'action': 'key_revoke', 'key_id': kid, 'status': 'ok'})
            return self.public_key(data['keys'][kid])

    def _authenticate(self, token: str, data):
        if len(token) > 256:
            raise KnowledgeError('unauthorized', 'Invalid, revoked or expired key', 401)
        parts = token.split('_', 2)
        key = data['keys'].get(parts[1]) if len(parts) == 3 and parts[0] == 'fkb' else None
        if (not key or key['revoked_at'] or
                not hmac.compare_digest(key['digest'], hashlib.sha256(token.encode()).hexdigest()) or
                (key['expires_at'] and datetime.fromisoformat(key['expires_at']).timestamp() <= time.time())):
            raise KnowledgeError('unauthorized', 'Invalid, revoked or expired key', 401)
        return self.public_key(key)

    def authenticate(self, token: str):
        with self.lock():
            return self._authenticate(token, self._catalog())

    def resolve(self, token: str, library_id: str | None):
        with self.lock():
            data = self._catalog()
            key = self._authenticate(token, data)
            allowed = [data['libraries'][i] for i in key['library_ids']
                       if i in data['libraries'] and data['libraries'][i]['enabled']]
            if library_id is None:
                if len(allowed) != 1:
                    raise KnowledgeError('library_required', 'Specify library_id; use list_libraries to see available libraries')
                library_id = allowed[0]['id']
            selected = next((x for x in allowed if x['id'] == library_id), None)
            if not selected:
                raise KnowledgeError('library_forbidden', 'Library is unavailable for this key', 403)
            return key, selected

    def visible_libraries(self, token: str):
        with self.lock():
            data = self._catalog()
            key = self._authenticate(token, data)
            rows = [row for row in data['libraries'].values()
                    if row['enabled'] and row['id'] in key['library_ids']]
        return [{'id': row['id'], 'name': row['name'], 'availability': self.availability(row)}
                for row in rows]

    def _event(self, event):
        # Caller owns lock. No tokens, query text, headers or document content are recorded.
        audit = self.directory / 'audit'
        audit.mkdir(exist_ok=True, mode=0o700)
        record = dict(event, timestamp=now())
        target = audit / (record['timestamp'][:10] + '.jsonl')
        with target.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')

    def run(self, token: str, library_id: str | None, tool: str, kwargs: dict, transport: str):
        identity = self.authenticate(token)
        request_id = uuid.uuid4().hex
        event = {'kind': 'access', 'request_id': request_id, 'key_id': identity['id'],
                 'agent_name': identity['agent_name'], 'library_id': library_id, 'tool': tool,
                 'path': str(kwargs.get('path', '.'))[:1024], 'transport': transport, 'started_at': now()}
        with self.active_mutex:
            self.active[request_id] = dict(event)
        start = time.monotonic()
        try:
            if tool == 'list_libraries':
                result = {'libraries': self.visible_libraries(token)}
            else:
                _, selected = self.resolve(token, library_id)
                event['library_id'] = selected['id']
                with self.active_mutex:
                    self.active[request_id] = dict(event)
                result = self.access_library(selected, tool, kwargs)
                result = dict(result, library_id=selected['id'])
            event['status'] = 'partial' if result.get('partial') else 'ok'
            return result
        except KnowledgeError as exc:
            event['status'], event['error'] = 'error', exc.code
            if isinstance(exc, LibraryUnavailable):
                event['reason'] = exc.reason
            raise
        except Exception:
            event['status'], event['error'] = 'error', 'internal_error'
            raise
        finally:
            event['duration_ms'] = round((time.monotonic() - start) * 1000, 1)
            try:
                with self.lock():
                    metrics = self._load('activity.json', {'since': now(), 'keys': {}})
                    current = metrics['keys'].setdefault(identity['id'], {'calls': 0, 'errors': 0, 'total_ms': 0})
                    current['calls'] += 1
                    current['errors'] += event['status'] == 'error'
                    current['total_ms'] = round(current['total_ms'] + event['duration_ms'], 1)
                    current['last_seen'] = now()
                    self._write('activity.json', metrics)
                    self._event(event)
            finally:
                with self.active_mutex:
                    self.active.pop(request_id, None)

    def snapshot(self):
        with self.lock():
            data = self._catalog()
            activity = self._load('activity.json', {'since': now(), 'keys': {}})
            keys = [dict(self.public_key(k), activity=activity['keys'].get(k['id'], {})) for k in data['keys'].values()]
            libraries = list(data['libraries'].values())
        libraries = [dict(row, availability=self.availability(row)) for row in libraries]
        with self.active_mutex:
            active = list(self.active.values())
        values = list(activity['keys'].values())
        return {'libraries': libraries, 'keys': keys, 'active_requests': active,
                'metrics': {'calls': sum(x['calls'] for x in values), 'errors': sum(x['errors'] for x in values),
                            'since': activity['since']}, 'server_time': now()}

    def recent_events(self, limit: int = 100, key_id: str | None = None, library_id: str | None = None):
        if not 1 <= limit <= 200:
            raise KnowledgeError('invalid_limit', 'limit must be 1..200')
        rows = []
        with self.lock():
            # A bounded recent view, not an unbounded log search across the entire history.
            for path in sorted((self.directory / 'audit').glob('*.jsonl'), reverse=True)[:7]:
                with path.open('rb') as stream:
                    stream.seek(0, 2)
                    size = stream.tell()
                    stream.seek(max(0, size - 262144))
                    if size > 262144:
                        stream.readline()
                    lines = stream.read().decode('utf-8').splitlines()
                for line in reversed(lines):
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if key_id and row.get('key_id') != key_id:
                        continue
                    if library_id and row.get('library_id') != library_id:
                        continue
                    rows.append(row)
                    if len(rows) >= limit:
                        return {'events': rows, 'window': 'Last 7 log files, last 256 KiB per file'}
        return {'events': rows, 'window': 'Last 7 log files, last 256 KiB per file'}
