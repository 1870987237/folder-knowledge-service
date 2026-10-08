"""Atomic, versioned file transactions in directly writable native libraries."""
import hashlib
import json
from pathlib import PurePosixPath
import re
import shutil
from typing import Annotated, Literal
import uuid

from fastapi import Request, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .capabilities import Capabilities
from .collaboration import copy_workspace, snapshot, workspace_digest, TEXT_BYTES
from .core import KnowledgeFolder, is_link
from .errors import KnowledgeError
from .storage import now

RequestId = Annotated[str, Field(min_length=1, max_length=128,
    pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]*$',
    description='ASCII letters/digits first, then letters/digits/._:-; 1..128 chars. Use a UUID, not a filename. Reuse only for an identical retry.')]


def valid_path(path):
    pure = PurePosixPath(path)
    if (not path or path == '.' or pure.is_absolute() or pure.as_posix() != path or
            len(pure.parts) > 64 or any(c in path for c in '\\:<>"|?*') or
            any(ord(c) < 32 for c in path) or any(
                p.startswith('.') or p.endswith((' ', '.')) or
                re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', p)
                for p in pure.parts)):
        raise KnowledgeError('invalid_path', 'Use a canonical relative path with /; no hidden, parent, reserved or absolute paths', 400)
    return pure


class Change(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation: Literal['create', 'replace', 'mkdir', 'delete']
    path: str = Field(min_length=1, max_length=1024)
    content: str | None = Field(default=None, max_length=TEXT_BYTES)
    expected_version: str | None = Field(default=None, min_length=1, max_length=200)
    create_parents: bool = False
    recursive: bool = False

    @model_validator(mode='after')
    def validate_change(self):
        try:
            self.path.encode('utf-8')
        except UnicodeError:
            raise ValueError('Path must be valid UTF-8 text') from None
        if self.operation in ('create', 'replace'):
            if self.content is None:
                raise ValueError('create/replace requires content')
            try:
                size = len(self.content.encode('utf-8'))
            except UnicodeError:
                raise ValueError('Content must be valid UTF-8 text') from None
            if size > TEXT_BYTES or '\x00' in self.content:
                raise ValueError('Content must be UTF-8, at most 256 KiB, without NUL')
        elif self.content is not None:
            raise ValueError('mkdir/delete must not include content')
        if self.operation in ('create', 'mkdir') and self.expected_version is not None:
            raise ValueError('create/mkdir must not specify expected_version')
        if self.create_parents and self.operation not in ('create', 'mkdir'):
            raise ValueError('create_parents is only for create/mkdir')
        if self.recursive and self.operation != 'delete':
            raise ValueError('recursive is only for directory deletion')
        return self


class FileWrite(Change):
    operation: Literal['create', 'replace']
    content: str = Field(max_length=TEXT_BYTES)
    library_id: str | None = Field(default=None, min_length=1, max_length=64)
    request_id: RequestId

    @model_validator(mode='after')
    def version_required(self):
        if self.operation == 'replace' and not self.expected_version:
            raise ValueError('Replacing a file requires expected_version from read_file or file_manifest')
        return self


class FileChanges(BaseModel):
    model_config = ConfigDict(extra='forbid')
    library_id: str | None = Field(default=None, min_length=1, max_length=64)
    request_id: RequestId
    changes: list[Change] = Field(min_length=1, max_length=100)
    expected_library_version: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode='after')
    def validate_batch(self):
        if len({c.path.casefold() for c in self.changes}) != len(self.changes):
            raise ValueError('Each path may occur only once, including case variants')
        if sum(len((c.content or '').encode('utf-8')) for c in self.changes) > 1024 * 1024:
            raise ValueError('Batch content exceeds 1 MiB; split into smaller batches')
        for c in self.changes:
            if c.operation in ('replace', 'delete') and not (c.expected_version or self.expected_library_version):
                raise ValueError('replace/delete requires expected_version or expected_library_version from file_manifest')
            if c.operation == 'delete' and any(x.path.casefold().startswith(c.path.casefold() + '/') for x in self.changes):
                raise ValueError('A deleted directory must not contain another operation in the same batch')
        return self


def library_version(row, digest):
    return hashlib.sha256((row['path'] + ':' + digest).encode()).hexdigest()


def remove_release(pool, target):
    """Only a concrete UUID release under the managed release root can be removed."""
    base = pool / 'document-releases'
    if (is_link(base.lstat()) or target.parent != base or not re.fullmatch('[0-9a-f]{32}', target.name)
            or not target.resolve().is_relative_to(base.resolve())):
        raise KnowledgeError('unsafe_path', 'Refusing to remove a path outside managed releases', 409)
    if target.exists():
        if is_link(target.lstat()):
            raise KnowledgeError('unsafe_path', 'Refusing to remove a linked release', 409)
        shutil.rmtree(target)


class FileWrites:
    def __init__(self, registry):
        self.registry, self.store = registry, registry.store

    def manifest(self, token, lid=None, path='.', offset=0, limit=100, expected_library_version=None):
        _, selected = self.registry.resolve(token, lid)
        with self.store.transaction() as db:
            key = self.registry._authenticate(token, db)
            row = db.execute('SELECT * FROM libraries WHERE id=?', (selected['id'],)).fetchone()
            Capabilities(self.registry).require(db, key, row, 'file_manifest')
            folder = self.registry.checked_folder(row)
            if path != '.':
                valid_path(path)
                if not folder._path(path).is_dir():
                    raise KnowledgeError('not_directory', 'Manifest path must be a directory', 400)
            captured = snapshot(folder.root)
            version = library_version(row, captured[0])
            if expected_library_version and version != expected_library_version:
                raise KnowledgeError('version_changed', 'Library changed; restart manifest pagination', 409)
            entries = []
            names = sorted([*captured[1], *captured[2]])
            names = [p for p in names if path == '.' or p.startswith(path + '/')]
            for p in names[offset:offset + limit]:
                item = {'path': p, 'type': 'directory'}
                if p in captured[1]:
                    with folder._open_text(p) as (stream, _, file_version):
                        raw = stream.read(len(captured[1][p]) + 1)
                        if raw != captured[1][p]:
                            raise KnowledgeError('version_changed', 'File changed during manifest read', 409)
                    item.update(type='file', version=file_version, sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))
                entries.append(item)
            if workspace_digest(folder.root) != captured[0]:
                raise KnowledgeError('version_changed', 'Library changed during manifest read', 409)
            return {'library_id': row['id'], 'library_version': version, 'entries': entries,
                    'next_offset': offset + limit if offset + limit < len(names) else None}

    def write(self, token, value: FileWrite, transport='rest'):
        # Preserve fingerprints of pre-batch write_file receipts for identical retries.
        body = value.model_dump(exclude={'recursive', 'create_parents'})
        if value.create_parents:
            body['create_parents'] = True
        change = Change(**value.model_dump(exclude={'library_id', 'request_id'}))
        batch = FileChanges(library_id=value.library_id, request_id=value.request_id, changes=[change])
        return self._publish(token, batch, body, transport, single=True)

    def apply(self, token, value: FileChanges, transport='rest'):
        return self._publish(token, value, {'api': 'apply_changes', **value.model_dump()}, transport)

    def _publish(self, token, value, body, transport, single=False):
        _, selected = self.registry.resolve(token, value.library_id)
        body = dict(body, library_id=selected['id'])
        try:
            fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        except UnicodeError:
            raise KnowledgeError('invalid_text', 'Request fields must be valid UTF-8 text', 400) from None
        target, committed = None, False
        try:
            with self.store.transaction(write=True) as db:
                key = self.registry._authenticate(token, db)
                row = db.execute('SELECT * FROM libraries WHERE id=?', (selected['id'],)).fetchone()
                Capabilities(self.registry).require(db, key, row, 'write_file')
                old = db.execute('SELECT * FROM file_write_receipts WHERE key_id=? AND request_id=?', (key['id'], value.request_id)).fetchone()
                if old:
                    if old['digest'] != fingerprint:
                        raise KnowledgeError('request_conflict', 'request_id was used for a different mutation', 409)
                    return dict(json.loads(old['receipt']), duplicate=True)
                folder = self.registry.checked_folder(row)
                captured = snapshot(folder.root)
                before = captured[0]
                if value.expected_library_version and value.expected_library_version != library_version(row, before):
                    raise KnowledgeError('version_changed', 'Library changed; fetch a new manifest before retrying', 409)
                versions = {}
                for c in value.changes:
                    valid_path(c.path)
                    if c.operation in ('create', 'replace') and PurePosixPath(c.path).suffix.lower() not in ('.md', '.txt'):
                        raise KnowledgeError('unsupported_document', 'Only UTF-8 Markdown / txt files can be written', 400)
                    if c.expected_version:
                        if c.path not in captured[1]:
                            raise KnowledgeError('version_changed', 'Expected file is missing or is a directory: ' + c.path, 409)
                        with folder._open_text(c.path) as (_, _, v):
                            if v != c.expected_version:
                                raise KnowledgeError('version_changed', 'File changed: ' + c.path, 409)
                        versions[c.path] = v
                    if c.operation == 'delete' and c.path in captured[2] and not value.expected_library_version:
                        raise KnowledgeError('directory_version_required', 'Directory deletion requires expected_library_version from file_manifest', 400)
                release = self.registry.pool.root / 'document-releases'
                if is_link(release.lstat()):
                    raise KnowledgeError('unsafe_path', 'Publication directory is unsafe', 409)
                candidate = release / uuid.uuid4().hex
                if candidate.exists() or candidate.is_symlink():
                    raise KnowledgeError('document_storage_unavailable', 'Publication identifier already exists; retry', 503)
                target = candidate
                # Reuse the validated snapshot: one tree copy for the entire batch.
                copy_workspace(folder.root, target, captured=captured)
                for c in value.changes:
                    self._change(target, c)
                after = workspace_digest(target)  # also enforces total size/entry limits
                for p, expected in versions.items():
                    with folder._open_text(p) as (_, _, v):
                        if v != expected:
                            raise KnowledgeError('version_changed', 'File changed during publication: ' + p, 409)
                if workspace_digest(folder.root) != before:
                    raise KnowledgeError('version_changed', 'Source changed during publication', 409)
                relative = target.relative_to(self.registry.pool.root).as_posix()
                published = KnowledgeFolder(target)
                results = []
                for c in value.changes:
                    item = {'path': c.path, 'operation': c.operation}
                    if c.operation in ('create', 'replace'):
                        with published._open_text(c.path) as (_, _, v):
                            item['version'] = v
                    results.append(item)
                receipt = {'request_id': value.request_id, 'library_id': row['id'], 'results': results,
                           'library_version': library_version({'path': relative}, after), 'published_at': now(),
                           'knowledge_updated': True, 'duplicate': False}
                if single:
                    receipt.update(results[0])
                db.execute('UPDATE libraries SET path=?,updated_at=? WHERE id=?', (relative, receipt['published_at'], row['id']))
                db.execute('INSERT INTO file_write_receipts VALUES (?,?,?,?,?,?,?)',
                           (key['id'], value.request_id, row['id'], fingerprint, json.dumps(receipt), row['path'], relative))
                self.store.event(db, {'kind': 'access', 'tool': 'write_file' if single else 'apply_changes',
                                     'key_id': key['id'], 'library_id': row['id'], 'request_id': value.request_id,
                                     'transport': transport, 'status': 'ok',
                                     **({'path': value.changes[0].path} if single else {})})
            committed = True
            return receipt
        except OSError:
            raise KnowledgeError('document_storage_unavailable', 'Publication storage unavailable', 503) from None
        finally:
            if target is not None and not committed:
                remove_release(self.registry.pool.root, target)

    @staticmethod
    def _change(root, c):
        p = root / PurePosixPath(c.path)
        parent = p.parent
        if c.create_parents:
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except (FileExistsError, NotADirectoryError):
                raise KnowledgeError('parent_not_directory', 'A parent component is not a directory: ' + c.path, 409) from None
        # Walk components separately: missing parent and file-as-parent are different errors.
        current = root
        for part in PurePosixPath(c.path).parts[:-1]:
            current = current / part
            if not current.exists():
                raise KnowledgeError('parent_not_found', 'Parent directory does not exist: ' + str(PurePosixPath(c.path).parent), 404)
            if not current.is_dir():
                raise KnowledgeError('parent_not_directory', 'Parent path is not a directory: ' + c.path, 409)
        if c.operation == 'mkdir':
            if p.exists() and not p.is_dir():
                raise KnowledgeError('file_exists', 'A file occupies the directory path: ' + c.path, 409)
            p.mkdir(exist_ok=True)
        elif c.operation == 'create':
            if p.exists():
                raise KnowledgeError('file_exists', 'Path already exists: ' + c.path, 409)
            p.write_text(c.content, encoding='utf-8', newline='\n')
        else:
            if not p.exists():
                raise KnowledgeError('file_not_found', 'Path to replace/delete does not exist: ' + c.path, 404)
            if c.operation == 'replace':
                if not p.is_file():
                    raise KnowledgeError('not_file', 'Cannot replace a directory with a file: ' + c.path, 409)
                p.write_text(c.content, encoding='utf-8', newline='\n')
            elif p.is_dir():
                if not c.recursive and any(p.iterdir()):
                    raise KnowledgeError('directory_not_empty', 'Directory is not empty; recursive=true requires a library version', 409)
                # p is a validated path inside this transaction's newly copied snapshot.
                if not p.resolve().is_relative_to(root.resolve()) or p == root:
                    raise KnowledgeError('unsafe_path', 'Directory is outside the staged release', 409)
                shutil.rmtree(p)
            else:
                if c.recursive:
                    raise KnowledgeError('not_directory', 'recursive applies only to directories', 400)
                p.unlink()


def add_write_routes(app, registry):
    from .server import bearer
    service = FileWrites(registry)

    @app.post('/api/v1/write')
    def write(value: FileWrite, request: Request):
        return service.write(bearer(request.headers), value)

    @app.post('/api/v1/files/changes')
    def apply_changes(value: FileChanges, request: Request):
        return service.apply(bearer(request.headers), value)

    @app.get('/api/v1/files/manifest')
    def manifest(request: Request, library_id: str | None = None, path: str = '.',
                 offset: int = Query(default=0, ge=0, le=2000), limit: int = Query(default=100, ge=1, le=200),
                 expected_library_version: str | None = None):
        return service.manifest(bearer(request.headers), library_id, path, offset, limit, expected_library_version)
