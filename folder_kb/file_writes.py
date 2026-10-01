"""Explicit, versioned native-library publication for Agent keys."""
import hashlib
import json
from pathlib import PurePosixPath
from typing import Literal
import uuid

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .capabilities import Capabilities
from .collaboration import Collaboration, copy_workspace, workspace_digest, TEXT_BYTES
from .core import is_link
from .errors import KnowledgeError
from .storage import now


class FileWrite(BaseModel):
    model_config = ConfigDict(extra='forbid')
    library_id: str | None = Field(default=None, min_length=1, max_length=64)
    path: str = Field(min_length=1, max_length=1024)
    content: str = Field(max_length=TEXT_BYTES)
    operation: Literal['create', 'replace']
    expected_version: str | None = Field(default=None, max_length=200)
    request_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]*$')

    @model_validator(mode='after')
    def validate_content(self):
        try:
            size = len(self.content.encode('utf-8'))
        except UnicodeError:
            raise ValueError('Content must be valid UTF-8 text') from None
        if size > TEXT_BYTES or '\x00' in self.content:
            raise ValueError('Content must be UTF-8 text, at most 256 KiB, without NUL')
        if self.operation == 'replace' and not self.expected_version:
            raise ValueError('Replacing a file requires expected_version from read_file')
        if self.operation == 'create' and self.expected_version is not None:
            raise ValueError('Creating a file must not specify expected_version')
        return self


class FileWrites:
    def __init__(self, registry):
        self.registry, self.store = registry, registry.store

    def write(self, token, value: FileWrite, transport='rest'):
        _, selected = self.registry.resolve(token, value.library_id)
        body = dict(value.model_dump(), library_id=selected['id'])
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        # Authorization, CAS, idempotency and the publication pointer share one transaction.
        with self.store.transaction(write=True) as db:
            key = self.registry._authenticate(token, db)
            row = db.execute('SELECT * FROM libraries WHERE id=?', (selected['id'],)).fetchone()
            Capabilities(self.registry).require(db, key, row, 'write_file')
            old = db.execute('SELECT * FROM file_write_receipts WHERE key_id=? AND request_id=?', (key['id'], value.request_id)).fetchone()
            if old:
                if old['digest'] != fingerprint:
                    raise KnowledgeError('request_conflict', 'request_id was used for a different write', 409)
                return dict(json.loads(old['receipt']), duplicate=True)
            folder = self.registry.checked_folder(row)
            # Validate portable path, encoding, size and parent even for new files.
            Collaboration.text(folder, value.path, missing=value.operation == 'create')
            current_version = None
            try:
                with folder._open_text(value.path) as (_, _, version):
                    current_version = version
            except KnowledgeError as exc:
                if exc.code != 'not_found' or value.operation != 'create':
                    raise
            if value.operation == 'create' and current_version is not None:
                raise KnowledgeError('file_exists', 'File already exists; read before replacing', 409)
            if value.operation == 'replace' and current_version != value.expected_version:
                raise KnowledgeError('version_changed', 'File changed; read again before replacing', 409)
            release = self.registry.pool.root / 'document-releases'
            if is_link(release.lstat()):
                raise KnowledgeError('unsafe_path', 'Publication directory is unsafe', 409)
            target = release / uuid.uuid4().hex
            before = workspace_digest(folder.root)
            try:
                copy_workspace(folder.root, target)
                if workspace_digest(target) != before:
                    raise KnowledgeError('version_changed', 'Source changed during snapshot', 409)
                # Check the metadata token again so a pre-snapshot external edit cannot slip through.
                if value.operation == 'replace':
                    with folder._open_text(value.path) as (_, _, version):
                        if version != value.expected_version:
                            raise KnowledgeError('version_changed', 'File changed during publication', 409)
                (target / PurePosixPath(value.path)).write_text(value.content, encoding='utf-8', newline='\n')
                if workspace_digest(folder.root) != before:
                    raise KnowledgeError('version_changed', 'Source changed during publication', 409)
            except OSError:
                raise KnowledgeError('document_storage_unavailable', 'Publication storage unavailable', 503) from None
            relative = target.relative_to(self.registry.pool.root).as_posix()
            with self.registry.folder_for_path(relative)._open_text(value.path) as (_, _, version):
                published_version = version
            receipt = {'request_id': value.request_id, 'library_id': row['id'], 'path': value.path,
                       'operation': value.operation, 'version': published_version, 'published_at': now(),
                       'knowledge_updated': True, 'duplicate': False}
            db.execute('UPDATE libraries SET path=?,updated_at=? WHERE id=?', (relative, receipt['published_at'], row['id']))
            db.execute('INSERT INTO file_write_receipts VALUES (?,?,?,?,?,?,?)',
                       (key['id'], value.request_id, row['id'], fingerprint, json.dumps(receipt), row['path'], relative))
            self.store.event(db, {'kind': 'access', 'tool': 'write_file', 'key_id': key['id'], 'library_id': row['id'],
                                  'path': value.path, 'request_id': value.request_id, 'transport': transport, 'status': 'ok'})
            return receipt


def add_write_routes(app, registry):
    from .server import bearer

    @app.post('/api/v1/write')
    def write(value: FileWrite, request: Request):
        return FileWrites(registry).write(bearer(request.headers), value)
