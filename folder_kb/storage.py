"""SQLite management storage and one-time import of legacy management files."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import time

from .core import KnowledgeError


def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


SCHEMA = (
    'CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL)',
    '''CREATE TABLE libraries (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL,
        enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''',
    '''CREATE TABLE agent_keys (
        id TEXT PRIMARY KEY, agent_name TEXT NOT NULL, digest TEXT NOT NULL,
        created_at TEXT NOT NULL, expires_at TEXT, revoked_at TEXT)''',
    '''CREATE TABLE key_libraries (
        key_id TEXT NOT NULL REFERENCES agent_keys(id) ON DELETE CASCADE,
        library_id TEXT NOT NULL REFERENCES libraries(id),
        PRIMARY KEY(key_id,library_id))''',
    'CREATE INDEX grants_by_library ON key_libraries(library_id,key_id)',
    '''CREATE TABLE activity (
        key_id TEXT PRIMARY KEY REFERENCES agent_keys(id),
        calls INTEGER NOT NULL DEFAULT 0 CHECK(calls>=0),
        errors INTEGER NOT NULL DEFAULT 0 CHECK(errors>=0),
        total_ms REAL NOT NULL DEFAULT 0 CHECK(total_ms>=0), last_seen TEXT)''',
    '''CREATE TABLE audit_events (
        id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL,
        key_id TEXT, library_id TEXT, status TEXT NOT NULL, event_json TEXT NOT NULL)''',
    'CREATE INDEX audit_time ON audit_events(timestamp DESC,id DESC)',
    'CREATE INDEX audit_key_time ON audit_events(key_id,timestamp DESC,id DESC)',
    'CREATE INDEX audit_library_time ON audit_events(library_id,timestamp DESC,id DESC)',
    'CREATE INDEX audit_status_time ON audit_events(status,timestamp DESC,id DESC)',
)

EVENT_FIELDS = frozenset(('timestamp', 'kind', 'action', 'request_id', 'key_id', 'agent_name',
                          'library_id', 'library_ids', 'previous_library_ids', 'tool', 'path',
                          'transport', 'started_at', 'status', 'error', 'reason', 'duration_ms'))


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Timestamp must include timezone')
    return parsed.astimezone(timezone.utc).isoformat(timespec='milliseconds')


class SqliteStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / 'management.sqlite3'
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        deadline = time.monotonic() + 5
        while True:
            try:
                self._initialize()
                break
            except KnowledgeError as exc:
                # Concurrent first opens may race while changing journal mode.
                # Only initialization is retried; rolled-back imports are idempotent.
                if exc.code != 'state_busy' or time.monotonic() >= deadline:
                    raise
                time.sleep(.05)

    def _initialize(self):
        # Initialization and legacy import are serialized across processes and atomic.
        with self.connection() as db:
            if db.execute('PRAGMA journal_mode').fetchone()[0] != 'wal':
                db.execute('PRAGMA journal_mode=WAL')
            db.execute('BEGIN IMMEDIATE')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version == 0:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                    raise ValueError('Unrecognized management database; refusing to overwrite it')
                for statement in SCHEMA:
                    db.execute(statement)
                db.execute('INSERT INTO metadata VALUES (?,?)', ('since', now()))
                self._import_legacy(db)
                db.execute('PRAGMA user_version=1')
            elif version not in (1, 2, 3, 4, 5, 6, 7, 8):
                raise ValueError('Unsupported management database version')
            if version in (0, 1):
                from .repositories import REPOSITORY_SCHEMA
                for statement in REPOSITORY_SCHEMA:
                    db.execute(statement)
                db.execute('PRAGMA user_version=2')
            if version in (0, 1, 2):
                from .inbox import INBOX_SCHEMA
                for statement in INBOX_SCHEMA:
                    db.execute(statement)
                db.execute('PRAGMA user_version=3')
            if version in (0, 1, 2, 3):
                from .template_schema import TEMPLATE_SCHEMA
                for statement in TEMPLATE_SCHEMA:
                    db.execute(statement)
                db.execute('PRAGMA user_version=4')
            if version < 5:
                columns = {row['name'] for row in db.execute('PRAGMA table_info(library_templates)')}
                if 'workspace_json' not in columns:
                    db.execute('ALTER TABLE library_templates ADD COLUMN workspace_json TEXT')
                    db.execute('ALTER TABLE library_templates ADD COLUMN workspace_key TEXT')
                db.execute('PRAGMA user_version=5')
            if version < 6:
                from .updates import UPDATE_SCHEMA
                for statement in UPDATE_SCHEMA:
                    db.execute(statement)
                db.execute('PRAGMA user_version=6')
            if version < 7:
                from .collaboration import COLLAB_SCHEMA
                for statement in COLLAB_SCHEMA:
                    db.execute(statement)
                db.execute('PRAGMA user_version=7')
            if version < 8:
                from .capabilities import CAPABILITY_SCHEMA
                for statement in CAPABILITY_SCHEMA:
                    db.execute(statement)
                db.execute('PRAGMA user_version=8')
            db.commit()

    @contextmanager
    def connection(self):
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA busy_timeout=5000')
            db.execute('PRAGMA synchronous=FULL')
            yield db
        except sqlite3.Error as exc:
            code = 'state_busy' if (getattr(exc, 'sqlite_errorcode', 0) & 255) in (
                sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) else 'state_unavailable'
            raise KnowledgeError(code, 'Management database is temporarily unavailable', 503) from None
        finally:
            if db is not None:
                if db.in_transaction:
                    db.rollback()
                db.close()

    @contextmanager
    def transaction(self, write=False):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield db
            db.commit()

    @staticmethod
    def library(row):
        return dict(row, enabled=bool(row['enabled'])) if row else None

    @staticmethod
    def key(db, kid):
        row = db.execute('SELECT * FROM agent_keys WHERE id=?', (kid,)).fetchone()
        if row is None:
            return None
        return dict(row, library_ids=[r[0] for r in db.execute(
            'SELECT library_id FROM key_libraries WHERE key_id=? ORDER BY library_id', (kid,))])

    @staticmethod
    def event(db, event):
        # Allowlist prevents accidental logging of credentials or document content.
        record = {key: value for key, value in event.items() if key in EVENT_FIELDS}
        record['timestamp'] = timestamp(record.get('timestamp', now()))
        if record.get('kind') not in ('admin', 'access') or record.get('status') not in ('ok', 'error', 'partial'):
            raise ValueError('Invalid audit event')
        db.execute('INSERT INTO audit_events(timestamp,key_id,library_id,status,event_json) VALUES (?,?,?,?,?)',
                   (record['timestamp'], record.get('key_id'), record.get('library_id'), record['status'],
                    json.dumps(record, ensure_ascii=False)))

    def _legacy_json(self, name, default):
        path = self.directory / name
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise ValueError(f'Cannot import legacy {name}; original files were not changed') from None

    def _import_legacy(self, db):
        catalog_path = self.directory / 'catalog.json'
        logs = sorted((self.directory / 'audit').glob('*.jsonl'))
        if not catalog_path.exists():
            if (self.directory / 'activity.json').exists() or logs:
                raise ValueError('Legacy records exist without catalog.json; restore the catalog before migration')
            db.execute('INSERT INTO metadata VALUES (?,?)', ('legacy_import', 'none'))
            return
        catalog = self._legacy_json('catalog.json', None)
        metrics = self._legacy_json('activity.json', {'keys': {}})
        try:
            if catalog['version'] != 1:
                raise ValueError('Unsupported legacy version')
            for lid, row in catalog['libraries'].items():
                if row['id'] != lid or not isinstance(row['enabled'], bool):
                    raise ValueError('Invalid library record')
                db.execute('INSERT INTO libraries VALUES (?,?,?,?,?,?)',
                           (lid, row['name'], row['path'], row['enabled'], row['created_at'], row['updated_at']))
            for kid, row in catalog['keys'].items():
                if row['id'] != kid:
                    raise ValueError('Invalid key record')
                db.execute('INSERT INTO agent_keys VALUES (?,?,?,?,?,?)',
                           (kid, row['agent_name'], row['digest'], row['created_at'], row['expires_at'], row['revoked_at']))
                db.executemany('INSERT INTO key_libraries VALUES (?,?)',
                               [(kid, lid) for lid in sorted(set(row['library_ids']))])
            for kid, row in metrics['keys'].items():
                db.execute('INSERT INTO activity VALUES (?,?,?,?,?)',
                           (kid, row['calls'], row['errors'], row['total_ms'], row.get('last_seen')))
            if metrics.get('since'):
                db.execute('UPDATE metadata SET value=? WHERE name=?', (metrics['since'], 'since'))
        except (KeyError, TypeError, ValueError, AttributeError, sqlite3.Error):
            raise ValueError('Invalid legacy configuration or statistics; migration rolled back, original files unchanged') from None
        for path in logs:
            number = 1
            try:
                with path.open(encoding='utf-8') as stream:
                    for number, line in enumerate(stream, 1):
                        if line.strip():
                            self.event(db, json.loads(line))
            except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
                raise ValueError(f'Cannot import legacy audit/{path.name} near line {number}; '
                                 'migration rolled back, original files unchanged') from None
        db.execute('INSERT INTO metadata VALUES (?,?)', ('legacy_import', now()))
