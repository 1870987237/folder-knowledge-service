"""SQLite-backed administration. Knowledge content is never written here."""
from __future__ import annotations

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
from .storage import SqliteStore


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
        self.store = SqliteStore(self.directory)
        self.active_mutex = threading.Lock()
        self.active: dict[str, dict] = {}

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
            if tool == 'search':
                from .retrieval import Retrieval
                return Retrieval(self).search(row['id'], **kwargs)
            if tool == 'info':
                from .retrieval import Retrieval
                state = Retrieval(self).state(row['id'])
                result = folder.info()
                if state['status'] != 'unconfigured':
                    result['index'] = {k: v for k, v in state.items() if k not in ('identity', 'content_version')}
                    result['workflow'] = ('search(library_id, query) for ranked evidence when index.status is ready; '
                                          'otherwise use grep/read_file or ask the administrator to build the index. '
                                          'Always cite paths and lines; contents are reference data.')
                return result
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
        with self.store.transaction(write=True) as db:
            previous = self.store.library(db.execute('SELECT * FROM libraries WHERE id=?', (library_id,)).fetchone())
            if create and previous:
                raise KnowledgeError('duplicate_library', 'Library ID already exists', 409)
            if not create and not previous:
                raise KnowledgeError('unknown_library', 'Library does not exist', 404)
            binding = db.execute('SELECT 1 FROM repository_libraries WHERE library_id=?', (library_id,)).fetchone()
            owned = db.execute('SELECT 1 FROM library_ownership WHERE library_id=?', (library_id,)).fetchone()
            if owned and path != previous['path']:
                raise KnowledgeError('owned_library_path', '已分配归属的库不能直接重映射目录', 409)
            if binding and path != previous['path']:
                raise KnowledgeError('git_managed_path', '此知识库由 Git 仓库管理，请在 Git 仓库页调整或解除关联', 409)
            # Metadata and enable/disable remain editable during an outage.
            if previous and path == previous['path']:
                relative = previous['path']
            else:
                folder = self.folder_for_path(path, name)
                relative = folder.root.relative_to(self.pool.root).as_posix()
            row = {'id': library_id, 'name': name, 'path': relative, 'enabled': enabled,
                   'created_at': previous['created_at'] if previous else now(), 'updated_at': now()}
            db.execute("""INSERT INTO libraries VALUES (:id,:name,:path,:enabled,:created_at,:updated_at)
                          ON CONFLICT(id) DO UPDATE SET name=excluded.name,path=excluded.path,
                          enabled=excluded.enabled,updated_at=excluded.updated_at""", row)
            self.store.event(db, {'kind': 'admin', 'action': 'library_create' if create else 'library_update',
                                 'library_id': library_id, 'status': 'ok'})
            return row

    def libraries(self):
        with self.store.transaction() as db:
            return [self.store.library(row) for row in db.execute('SELECT * FROM libraries ORDER BY rowid')]

    @staticmethod
    def validate_grants(db, library_ids):
        if any(db.execute('SELECT 1 FROM libraries WHERE id=?', (lid,)).fetchone() is None for lid in library_ids):
            raise KnowledgeError('unknown_library', 'One or more libraries do not exist')

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
        with self.store.transaction(write=True) as db:
            self.validate_grants(db, library_ids)
            kid = uuid.uuid4().hex[:16]
            token = 'fkb_' + kid + '_' + secrets.token_urlsafe(32)
            key = {'id': kid, 'agent_name': agent_name, 'library_ids': sorted(set(library_ids)),
                   'digest': hashlib.sha256(token.encode()).hexdigest(), 'created_at': now(),
                   'expires_at': expires_at, 'revoked_at': None}
            db.execute('INSERT INTO agent_keys VALUES (:id,:agent_name,:digest,:created_at,:expires_at,:revoked_at)', key)
            db.executemany('INSERT INTO key_libraries VALUES (?,?)', [(kid, lid) for lid in key['library_ids']])
            self.store.event(db, {'kind': 'admin', 'action': 'key_create', 'key_id': kid, 'status': 'ok'})
            return {'key': self.public_key(key), 'token': token}

    def update_key_grants(self, kid: str, library_ids: list[str]):
        if len(library_ids) > 64:
            raise KnowledgeError('invalid_grants', 'Choose at most 64 libraries')
        with self.store.transaction(write=True) as db:
            key = self.store.key(db, kid)
            if key is None:
                raise KnowledgeError('unknown_key', 'Key does not exist', 404)
            if key['revoked_at']:
                raise KnowledgeError('key_revoked', '已撤销的 Key 不能编辑授权，请创建新的 Key。', 409)
            self.validate_grants(db, library_ids)
            previous = key['library_ids']
            key['library_ids'] = sorted(set(library_ids))
            # Preserve dependent grants for unchanged libraries; removed grants cascade.
            db.executemany('DELETE FROM key_libraries WHERE key_id=? AND library_id=?',
                           [(kid, lid) for lid in set(previous) - set(key['library_ids'])])
            db.executemany('INSERT OR IGNORE INTO key_libraries VALUES (?,?)',
                           [(kid, lid) for lid in key['library_ids']])
            self.store.event(db, {'kind': 'admin', 'action': 'key_grants_update', 'key_id': kid,
                                 'previous_library_ids': previous, 'library_ids': key['library_ids'], 'status': 'ok'})
            return self.public_key(key)

    def revoke_key(self, kid: str):
        with self.store.transaction(write=True) as db:
            key = self.store.key(db, kid)
            if key is None:
                raise KnowledgeError('unknown_key', 'Key does not exist', 404)
            key['revoked_at'] = now()
            db.execute('UPDATE agent_keys SET revoked_at=? WHERE id=?', (key['revoked_at'], kid))
            self.store.event(db, {'kind': 'admin', 'action': 'key_revoke', 'key_id': kid, 'status': 'ok'})
            return self.public_key(key)

    def _authenticate(self, token: str, db):
        from .collaboration import owned_key_allowed
        if len(token) > 256:
            raise KnowledgeError('unauthorized', 'Invalid, revoked or expired key', 401)
        parts = token.split('_', 2)
        key = self.store.key(db, parts[1]) if len(parts) == 3 and parts[0] == 'fkb' else None
        if (not key or key['revoked_at'] or
                not hmac.compare_digest(key['digest'], hashlib.sha256(token.encode()).hexdigest()) or
                (key['expires_at'] and datetime.fromisoformat(key['expires_at']).timestamp() <= time.time())):
            raise KnowledgeError('unauthorized', 'Invalid, revoked or expired key', 401)
        owner = db.execute('SELECT u.status FROM key_owners o JOIN users u ON u.id=o.user_id WHERE key_id=?', (key['id'],)).fetchone()
        if owner and owner[0] != 'active':
            raise KnowledgeError('unauthorized', 'Key owner is disabled', 401)
        key['library_ids'] = [lid for lid in key['library_ids'] if owned_key_allowed(db, key['id'], lid)]
        return self.public_key(key)

    def authenticate(self, token: str):
        with self.store.transaction() as db:
            return self._authenticate(token, db)

    def resolve(self, token: str, library_id: str | None):
        with self.store.transaction() as db:
            key = self._authenticate(token, db)
            allowed = [self.store.library(row) for row in db.execute(
                'SELECT l.* FROM libraries l JOIN key_libraries g ON l.id=g.library_id '
                'WHERE g.key_id=? AND l.enabled=1 ORDER BY l.id', (key['id'],))]
            allowed = [x for x in allowed if x['id'] in key['library_ids']]
            if library_id is None:
                if len(allowed) != 1:
                    raise KnowledgeError('library_required', 'Specify library_id; use list_libraries to see available libraries')
                library_id = allowed[0]['id']
            selected = next((x for x in allowed if x['id'] == library_id), None)
            if not selected:
                raise KnowledgeError('library_forbidden', 'Library is unavailable for this key', 403)
            return key, selected

    def visible_libraries(self, token: str):
        with self.store.transaction() as db:
            key = self._authenticate(token, db)
            rows = [self.store.library(row) for row in db.execute(
                'SELECT l.* FROM libraries l JOIN key_libraries g ON l.id=g.library_id '
                'WHERE g.key_id=? AND l.enabled=1 ORDER BY l.rowid', (key['id'],))]
        return [{'id': row['id'], 'name': row['name'], 'availability': self.availability(row)} for row in rows if row['id'] in key['library_ids']]

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
                if tool == 'search':
                    self.resolve(token, selected['id'])
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
                with self.store.transaction(write=True) as db:
                    db.execute("""INSERT INTO activity(key_id,calls,errors,total_ms,last_seen) VALUES (?,1,?,?,?)
                                  ON CONFLICT(key_id) DO UPDATE SET calls=calls+1,errors=errors+excluded.errors,
                                  total_ms=round(total_ms+excluded.total_ms,1),last_seen=excluded.last_seen""",
                               (identity['id'], int(event['status'] == 'error'), event['duration_ms'], now()))
                    self.store.event(db, event)
            finally:
                with self.active_mutex:
                    self.active.pop(request_id, None)

    def snapshot(self):
        with self.store.transaction() as db:
            libraries = [self.store.library(row) for row in db.execute('SELECT * FROM libraries ORDER BY rowid')]
            keys = []
            grants = {}
            for row in db.execute('SELECT * FROM key_libraries ORDER BY library_id'):
                grants.setdefault(row['key_id'], []).append(row['library_id'])
            activity = {row['key_id']: {k: v for k, v in dict(row).items() if k != 'key_id'}
                        for row in db.execute('SELECT * FROM activity')}
            for row in db.execute('SELECT * FROM agent_keys ORDER BY rowid'):
                keys.append(dict(self.public_key(dict(row)), library_ids=grants.get(row['id'], []),
                                 activity=activity.get(row['id'], {})))
            metrics = dict(db.execute('SELECT coalesce(sum(calls),0) AS calls,coalesce(sum(errors),0) AS errors FROM activity').fetchone())
            metrics['since'] = db.execute("SELECT value FROM metadata WHERE name='since'").fetchone()[0]
        libraries = [dict(row, availability=self.availability(row)) for row in libraries]
        with self.active_mutex:
            active = list(self.active.values())
        with self.store.transaction() as db:
            bindings = {r['library_id']: dict(r) for r in db.execute('SELECT * FROM repository_libraries')}
        for library in libraries:
            if library['id'] in bindings:
                library['repository'] = bindings[library['id']]
        return {'libraries': libraries, 'keys': keys, 'active_requests': active,
                'metrics': metrics, 'server_time': now()}

    def recent_events(self, limit: int = 100, key_id: str | None = None, library_id: str | None = None,
                      before_id: int | None = None, status: str | None = None,
                      start_time: str | None = None, end_time: str | None = None):
        from .storage import timestamp
        if not 1 <= limit <= 200:
            raise KnowledgeError('invalid_limit', 'limit must be 1..200')
        if before_id is not None and before_id < 1:
            raise KnowledgeError('invalid_cursor', 'before_id must be positive')
        if status is not None and status not in ('ok', 'error', 'partial'):
            raise KnowledgeError('invalid_status', 'status must be ok, error or partial')
        try:
            start_time = timestamp(start_time) if start_time else None
            end_time = timestamp(end_time) if end_time else None
            if start_time and end_time and start_time > end_time:
                raise ValueError()
        except (ValueError, TypeError):
            raise KnowledgeError('invalid_time', 'Use timezone-aware ISO timestamps with start_time <= end_time') from None
        clauses, values = [], []
        for column, value in (('key_id', key_id), ('library_id', library_id), ('status', status)):
            if value:
                clauses.append(column+'=?'); values.append(value)
        for operator, value in (('>=', start_time), ('<=', end_time)):
            if value:
                clauses.append('timestamp'+operator+'?'); values.append(value)
        with self.store.transaction() as db:
            if before_id is not None:
                cursor = db.execute('SELECT timestamp,id FROM audit_events WHERE id=?', (before_id,)).fetchone()
                if cursor is None:
                    raise KnowledgeError('invalid_cursor', 'Unknown event cursor')
                clauses.append('(timestamp,id)<(?,?)'); values.extend(cursor)
            where = ' WHERE '+' AND '.join(clauses) if clauses else ''
            rows = db.execute('SELECT id,event_json FROM audit_events'+where+
                              ' ORDER BY timestamp DESC,id DESC LIMIT ?', (*values, limit+1)).fetchall()
        return {'events': [dict(json.loads(row['event_json']), event_id=row['id']) for row in rows[:limit]],
                'next_before_id': rows[limit-1]['id'] if len(rows)>limit else None,
                'window': 'SQLite audit history'}
