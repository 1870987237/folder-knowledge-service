"""Versioned material envelopes and a durable, separately authorized inbox."""
from __future__ import annotations

import hashlib
import json
import secrets
import time
import uuid
from typing import Literal

from fastapi import Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import KnowledgeError
from .storage import now


INBOX_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS inbox_grants (
        key_id TEXT NOT NULL, library_id TEXT NOT NULL,
        PRIMARY KEY(key_id,library_id),
        FOREIGN KEY(key_id,library_id) REFERENCES key_libraries(key_id,library_id) ON DELETE CASCADE)""",
    """CREATE TABLE IF NOT EXISTS inbox_items (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        id TEXT NOT NULL UNIQUE,
        library_id TEXT NOT NULL REFERENCES libraries(id),
        key_id TEXT NOT NULL REFERENCES agent_keys(id),
        submission_id TEXT NOT NULL,
        payload TEXT NOT NULL, digest TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending','processing','completed')),
        claim_digest TEXT, lease_until REAL, attempts INTEGER NOT NULL DEFAULT 0,
        result TEXT,
        UNIQUE(key_id,library_id,submission_id))""",
    'CREATE INDEX IF NOT EXISTS inbox_queue ON inbox_items(library_id,status,sequence)',
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class Content(StrictModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=False)
    format: Literal['text', 'markdown'] = 'markdown'
    text: str = Field(min_length=1, max_length=32768)

    @field_validator('text')
    @classmethod
    def byte_limit(cls, value):
        if not value.strip():
            raise ValueError('Content cannot be blank')
        if len(value.encode('utf-8')) > 32768:
            raise ValueError('Content must be at most 32768 UTF-8 bytes')
        return value


class Source(StrictModel):
    # Descriptive references only: the service never fetches or opens them.
    reference: str = Field(default='', max_length=2048)
    description: str = Field(default='', max_length=1024)


class Material(StrictModel):
    schema_version: Literal['1.0'] = '1.0'
    submission_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]*$')
    library_id: str = Field(min_length=1, max_length=64, pattern=r'^[a-z0-9][a-z0-9_-]*$')
    kind: Literal['new_content', 'correction', 'observation'] = 'new_content'
    title: str = Field(default='', max_length=200)
    content: Content
    source: Source | None = None
    context: dict[str, str] = Field(default_factory=dict, max_length=16)

    @model_validator(mode='after')
    def envelope_limits(self):
        if any(not k.strip() or len(k) > 64 or len(v) > 1024 for k, v in self.context.items()):
            raise ValueError('Context keys: 1..64 characters; values: at most 1024 characters')
        if len(self.model_dump_json().encode('utf-8')) > 49152:
            raise ValueError('Material must be at most 49152 UTF-8 bytes')
        return self


class SubmissionGrants(StrictModel):
    library_ids: list[str] = Field(max_length=64)


class KeyPermissions(StrictModel):
    library_ids: list[str] = Field(max_length=64)
    submit_library_ids: list[str] = Field(max_length=64)


class Claim(StrictModel):
    lease_seconds: int = Field(default=900, ge=30, le=3600)


class Completion(StrictModel):
    claim_token: str = Field(min_length=1, max_length=128)
    outcome: Literal['accepted', 'partially_accepted', 'rejected']
    summary: str = Field(min_length=1, max_length=4000)
    references: list[str] = Field(default_factory=list, max_length=32)
    version: str | None = Field(default=None, max_length=200)

    @field_validator('references')
    @classmethod
    def reference_limits(cls, values):
        if any(not value.strip() or len(value) > 1024 for value in values):
            raise ValueError('References must contain 1..1024 characters')
        return values


class Release(StrictModel):
    claim_token: str = Field(min_length=1, max_length=128)


class Inbox:
    def __init__(self, registry):
        self.registry = registry
        self.store = registry.store

    def authorize(self, db, token, library_id):
        from .collaboration import owned_key_allowed
        key = self.registry._authenticate(token, db)
        row = db.execute(
            'SELECT 1 FROM inbox_grants g JOIN libraries l ON l.id=g.library_id '
            'WHERE g.key_id=? AND g.library_id=? AND l.enabled=1',
            (key['id'], library_id)).fetchone()
        if not row or not owned_key_allowed(db, key['id'], library_id, submit=True):
            raise KnowledgeError('inbox_forbidden', 'Material submission is not granted for this library', 403)
        return key

    def grants(self, key_id, library_ids=None):
        with self.store.transaction(write=library_ids is not None) as db:
            key = self.store.key(db, key_id)
            if key is None:
                raise KnowledgeError('unknown_key', 'Key does not exist', 404)
            if library_ids is not None:
                if key['revoked_at']:
                    raise KnowledgeError('key_revoked', 'Cannot grant a revoked key', 409)
                if not set(library_ids).issubset(key['library_ids']):
                    raise KnowledgeError('invalid_grants', 'Submission libraries must be within current library grants')
                db.execute('DELETE FROM inbox_grants WHERE key_id=?', (key_id,))
                db.executemany('INSERT INTO inbox_grants VALUES (?,?)',
                               [(key_id, lid) for lid in sorted(set(library_ids))])
                self.store.event(db, {'kind': 'admin', 'action': 'inbox_grants_update',
                                      'key_id': key_id, 'library_ids': library_ids, 'status': 'ok'})
            return {'key_id': key_id, 'submit_library_ids': [r[0] for r in db.execute(
                'SELECT library_id FROM inbox_grants WHERE key_id=? ORDER BY library_id', (key_id,))]}

    def permissions(self, key_id, value: KeyPermissions | None = None):
        """Read or save both scopes together, so one save cannot partially succeed."""
        with self.store.transaction(write=value is not None) as db:
            key = self.store.key(db, key_id)
            if key is None:
                raise KnowledgeError('unknown_key', 'Key does not exist', 404)
            if value is not None:
                if key['revoked_at']:
                    raise KnowledgeError('key_revoked', 'Cannot grant a revoked key', 409)
                reads = sorted(set(value.library_ids))
                submissions = sorted(set(value.submit_library_ids))
                if not set(submissions).issubset(reads):
                    raise KnowledgeError('invalid_grants', 'Submission libraries must be within access grants')
                self.registry.validate_grants(db, reads)
                previous = key['library_ids']
                db.executemany('DELETE FROM key_libraries WHERE key_id=? AND library_id=?',
                               [(key_id, lid) for lid in set(previous) - set(reads)])
                db.executemany('INSERT OR IGNORE INTO key_libraries VALUES (?,?)',
                               [(key_id, lid) for lid in reads])
                db.execute('DELETE FROM inbox_grants WHERE key_id=?', (key_id,))
                db.executemany('INSERT INTO inbox_grants VALUES (?,?)',
                               [(key_id, lid) for lid in submissions])
                self.store.event(db, {'kind': 'admin', 'action': 'key_grants_update', 'key_id': key_id,
                                      'previous_library_ids': previous, 'library_ids': reads, 'status': 'ok'})
                self.store.event(db, {'kind': 'admin', 'action': 'inbox_grants_update', 'key_id': key_id,
                                      'library_ids': submissions, 'status': 'ok'})
                key['library_ids'] = reads
            return {'key_id': key_id, 'library_ids': key['library_ids'], 'submit_library_ids': [r[0] for r in db.execute(
                'SELECT library_id FROM inbox_grants WHERE key_id=? ORDER BY library_id', (key_id,))]}

    @staticmethod
    def record(row, include_material=True):
        value = {key: row[key] for key in ('id', 'library_id', 'submission_id', 'created_at',
                                          'updated_at', 'status', 'attempts', 'lease_until')}
        value['submitted_by'] = {'type': 'agent_key', 'id': row['key_id']}
        value['result'] = json.loads(row['result']) if row['result'] else None
        material = json.loads(row['payload'])
        value['title'], value['kind'] = material['title'], material['kind']
        # A processing conclusion does not attest publication.
        if include_material:
            value['material'] = material
        return value

    @staticmethod
    def require_item(db, item_id):
        row = db.execute('SELECT * FROM inbox_items WHERE id=?', (item_id,)).fetchone()
        if row is None:
            raise KnowledgeError('inbox_not_found', 'Submission does not exist', 404)
        return row

    def submit(self, token, material: Material, transport='rest'):
        payload = json.dumps(material.model_dump(), ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self.store.transaction(write=True) as db:
            key = self.authorize(db, token, material.library_id)
            row = db.execute('SELECT * FROM inbox_items WHERE key_id=? AND library_id=? AND submission_id=?',
                             (key['id'], material.library_id, material.submission_id)).fetchone()
            duplicate = row is not None
            if row and row['digest'] != digest:
                raise KnowledgeError('submission_conflict', 'This submission_id was used for different material', 409)
            if not row:
                item_id, timestamp = uuid.uuid4().hex, now()
                db.execute("""INSERT INTO inbox_items
                    (id,library_id,key_id,submission_id,payload,digest,created_at,updated_at,status)
                    VALUES (?,?,?,?,?,?,?,?,'pending')""",
                    (item_id, material.library_id, key['id'], material.submission_id, payload, digest, timestamp, timestamp))
                row = self.require_item(db, item_id)
            self.store.event(db, {'kind': 'access', 'tool': 'submit_material', 'key_id': key['id'],
                                  'library_id': material.library_id, 'request_id': row['id'],
                                  'transport': transport, 'status': 'ok'})
            return dict(self.record(row, False), duplicate=duplicate, knowledge_updated=False)

    def get(self, item_id, token=None):
        with self.store.transaction() as db:
            row = self.require_item(db, item_id)
            if token is not None:
                key = self.registry._authenticate(token, db)
                if key['id'] != row['key_id']:
                    raise KnowledgeError('inbox_not_found', 'Submission does not exist', 404)
                self.authorize(db, token, row['library_id'])
            return dict(self.record(row), update_job=self.job_status(db, item_id))

    @staticmethod
    def job_status(db, item_id):
        job = db.execute('SELECT j.id,j.status FROM update_jobs j JOIN update_job_items i ON i.job_id=j.id WHERE i.item_id=? ORDER BY j.created_at DESC,j.id DESC LIMIT 1', (item_id,)).fetchone()
        return dict(job) if job else None

    @staticmethod
    def assert_manual(db, item_id):
        if db.execute("SELECT 1 FROM update_jobs j JOIN update_job_items i ON i.job_id=j.id WHERE i.item_id=? AND j.status IN ('queued','running','candidate_ready','publish_queued','publishing')", (item_id,)).fetchone():
            raise KnowledgeError('inbox_managed_by_job', 'Submission belongs to an update job; review it on the update jobs page', 409)

    def listing(self, library_id=None, status=None, after=0, limit=50):
        with self.store.transaction() as db:
            rows = db.execute(
                'SELECT * FROM inbox_items WHERE sequence>? '
                'AND (? IS NULL OR library_id=?) AND (? IS NULL OR status=?) ORDER BY sequence LIMIT ?',
                (after, library_id, library_id, status, status, limit + 1)).fetchall()
            page = rows[:limit]
            return {'items': [dict(self.record(row, False), update_job=self.job_status(db, row['id'])) for row in page],
                    'next_after': page[-1]['sequence'] if len(rows) > limit else None}

    def claim(self, item_id, lease_seconds):
        with self.store.transaction(write=True) as db:
            self.assert_manual(db, item_id)
            row = self.require_item(db, item_id)
            if row['status'] == 'completed' or (
                    row['status'] == 'processing' and row['lease_until'] > time.time()):
                raise KnowledgeError('inbox_claim_conflict', 'Submission is completed or already claimed', 409)
            if not db.execute('SELECT 1 FROM libraries WHERE id=? AND enabled=1',
                              (row['library_id'],)).fetchone():
                raise KnowledgeError('library_disabled', 'Library is disabled', 409)
            claim_token = secrets.token_urlsafe(32)
            db.execute("""UPDATE inbox_items SET status='processing',claim_digest=?,lease_until=?,
                          attempts=attempts+1,updated_at=? WHERE id=?""",
                       (hashlib.sha256(claim_token.encode()).hexdigest(), time.time() + lease_seconds, now(), item_id))
            self.event(db, item_id, row['library_id'], 'inbox_claim')
            return dict(self.record(self.require_item(db, item_id)), claim_token=claim_token)

    def finish(self, item_id, value: Completion | Release):
        with self.store.transaction(write=True) as db:
            self.assert_manual(db, item_id)
            row = self.require_item(db, item_id)
            if (row['status'] != 'processing' or row['lease_until'] <= time.time() or
                    not secrets.compare_digest(row['claim_digest'], hashlib.sha256(value.claim_token.encode()).hexdigest())):
                raise KnowledgeError('inbox_claim_conflict', 'Claim is invalid or expired; claim the submission again', 409)
            complete = isinstance(value, Completion)
            result = value.model_dump(exclude={'claim_token'}) if complete else None
            db.execute('UPDATE inbox_items SET status=?,result=?,claim_digest=NULL,lease_until=NULL,updated_at=? WHERE id=?',
                       ('completed' if complete else 'pending', json.dumps(result, ensure_ascii=False) if result else None,
                        now(), item_id))
            self.event(db, item_id, row['library_id'], 'inbox_complete' if complete else 'inbox_release')
            return self.record(self.require_item(db, item_id))

    def event(self, db, item_id, library_id, action):
        self.store.event(db, {'kind': 'admin', 'action': action, 'request_id': item_id,
                              'library_id': library_id, 'status': 'ok'})


def add_inbox_routes(app, inbox):
    from .server import bearer

    @app.post('/api/v1/inbox/submissions', status_code=202)
    def submit(request: Request, value: Material):
        return inbox.submit(bearer(request.headers), value)

    @app.get('/api/v1/inbox/submissions/{item_id}')
    def receipt(request: Request, item_id: str):
        return inbox.get(item_id, bearer(request.headers))

    @app.get('/admin/api/keys/{key_id}/inbox-grants')
    def get_grants(key_id: str):
        return inbox.grants(key_id)

    @app.put('/admin/api/keys/{key_id}/inbox-grants')
    def set_grants(key_id: str, value: SubmissionGrants):
        return inbox.grants(key_id, value.library_ids)

    @app.get('/admin/api/keys/{key_id}/permissions')
    def get_permissions(key_id: str):
        return inbox.permissions(key_id)

    @app.put('/admin/api/keys/{key_id}/permissions')
    def set_permissions(key_id: str, value: KeyPermissions):
        return inbox.permissions(key_id, value)

    @app.get('/admin/api/inbox')
    def listing(library_id: str | None = None, status: Literal['pending', 'processing', 'completed'] | None = None,
                after: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100)):
        return inbox.listing(library_id, status, after, limit)

    @app.get('/admin/api/inbox/{item_id}')
    def detail(item_id: str):
        return inbox.get(item_id)

    @app.post('/admin/api/inbox/{item_id}/claim')
    def claim(item_id: str, value: Claim):
        return inbox.claim(item_id, value.lease_seconds)

    @app.post('/admin/api/inbox/{item_id}/complete')
    def complete(item_id: str, value: Completion):
        return inbox.finish(item_id, value)

    @app.post('/admin/api/inbox/{item_id}/release')
    def release(item_id: str, value: Release):
        return inbox.finish(item_id, value)
