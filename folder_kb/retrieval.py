"""Bounded, per-library lexical RAG. Files remain the source of truth."""
from contextlib import closing
import hashlib
import json
import os
import re
import sqlite3
import time
import unicodedata
import uuid

from fastapi import Query

from .core import matches
from .errors import KnowledgeError
from .storage import now
from .templates import TemplateCatalog

MAX_BYTES = 16 * 1024 * 1024
MAX_FILES = 1000
MAX_CHUNKS = 16000
ENGINE = 'sqlite-fts5-bm25-v1'


def supported(index):
    return bool(index and index.get('mode') == 'fulltext' and
                index.get('splitter') in ('document', 'markdown_sections') and not index.get('entrypoint'))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def tokens(text):
    """English identifiers + Chinese adjacent character pairs; no external model."""
    parts = re.findall(r'[a-z0-9_]+|[\u3400-\u9fff]+', unicodedata.normalize('NFKC', text).lower())
    result = []
    for part in parts:
        if '\u3400' <= part[0] <= '\u9fff' and len(part) > 1:
            result.extend(part[i:i + 2] for i in range(len(part) - 1))
        else:
            result.append(part)
    return result


def chunks(text, splitter):
    """Source-preserving blocks with line citations, bounded to 1200 characters."""
    block, size, start, title, fence = [], 0, 1, '', False
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        heading = re.match(r'^#{1,6}\s+(.+)', line) if not fence and splitter == 'markdown_sections' else None
        if block and (heading or size + len(line) > 1200):
            yield start, number - 1, title, ''.join(block)
            block, size = [], 0
        if heading:
            title = heading.group(1).strip()
        if line.lstrip().startswith(('```', '~~~')):
            fence = not fence
        # Long single lines still fit the result budget; each fragment cites that line.
        for offset in range(0, len(line), 1200):
            part = line[offset:offset + 1200]
            if not block:
                start = number
            block.append(part)
            size += len(part)
            if size >= 1200:
                yield start, number, title, ''.join(block)
                block, size = [], 0
    if block:
        yield start, number, title, ''.join(block)


class Retrieval:
    def __init__(self, registry):
        self.registry = registry
        self.catalog = TemplateCatalog(registry)
        self.directory = registry.directory / 'indexes'

    def context(self, library_id):
        with self.registry.store.transaction() as db:
            row = db.execute('SELECT * FROM libraries WHERE id=?', (library_id,)).fetchone()
            if not row:
                raise KnowledgeError('unknown_library', 'Library does not exist', 404)
            row = dict(row)
            if not row['enabled']:
                raise KnowledgeError('library_disabled', 'Library is disabled', 409)
            binding = db.execute('SELECT * FROM library_templates WHERE library_id=?', (library_id,)).fetchone()
            if not binding:
                raise KnowledgeError('index_not_configured', 'Bind an indexing plugin first', 409)
            definition = json.loads(self.catalog.require(db, binding['template_id'], binding['template_version'])['metadata_json'])
            index = (definition.get('plugin') or {}).get('indexing')
            if not index:
                raise KnowledgeError('index_not_configured', 'The bound plugin does not define an index', 409)
            if not supported(index):
                raise KnowledgeError('index_unsupported', 'This engine supports fulltext with document or markdown_sections splitting', 409)
            identity = digest([ENGINE, row['path'], definition, json.loads(binding['config_json'])])
        return row, self.registry.checked_folder(row), index, identity

    def snapshot(self, folder, index, read=True):
        entries, partial = folder._walk('.', True)
        if partial:
            raise KnowledgeError('index_scan_incomplete', 'Directory scan was incomplete; narrow the knowledge mount', 409)
        files = [e for e in entries if e['type'] == 'file' and
                 any(matches(e['path'], p) for p in index['include']) and
                 not any(matches(e['path'], p) for p in index['exclude'])]
        if len(files) > MAX_FILES or sum(e['bytes'] for e in files) > MAX_BYTES:
            raise KnowledgeError('index_limit', 'Demo index supports at most 1000 files and 16 MiB per library', 413)
        contents, fingerprints, total = [], [], 0
        deadline = time.monotonic() + 15
        for entry in files:
            if time.monotonic() > deadline:
                raise KnowledgeError('index_limit', 'Index scan exceeded its time budget', 413)
            with folder._open_text(entry['path']) as (stream, _, version):
                raw = stream.read(MAX_BYTES - total + 1)
            total += len(raw)
            if total > MAX_BYTES:
                raise KnowledgeError('index_limit', 'Knowledge content exceeds 16 MiB', 413)
            sha = hashlib.sha256(raw).hexdigest()
            fingerprints.append([entry['path'], sha, version])
            if read:
                try:
                    text = raw.decode('utf-8-sig')
                    if '\x00' in text:
                        raise UnicodeError()
                except UnicodeError:
                    raise KnowledgeError('index_encoding', 'Index requires UTF-8 text without NUL bytes', 415) from None
                contents.append((entry['path'], text, sha, version))
        return contents, digest(fingerprints)

    def path(self, library_id):
        return self.directory / (hashlib.sha256(library_id.encode()).hexdigest() + '.sqlite3')

    def connect(self, library_id):
        path = self.path(library_id)
        if not path.is_file():
            raise KnowledgeError('index_missing', 'Build the library index first', 409)
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3)
        connection.row_factory = sqlite3.Row
        return connection

    def state(self, library_id):
        try:
            _, folder, index, identity = self.context(library_id)
            with closing(self.connect(library_id)) as db:
                meta = json.loads(db.execute('SELECT value FROM metadata').fetchone()[0])
            _, version = self.snapshot(folder, index, read=False)
            return dict(meta, status='ready' if meta['identity'] == identity and meta['content_version'] == version else 'stale')
        except KnowledgeError as exc:
            return {'status': {'index_missing': 'missing', 'index_not_configured': 'unconfigured',
                               'index_unsupported': 'unsupported'}.get(exc.code, 'unavailable'),
                    'error': exc.code, 'message': exc.message}
        except sqlite3.DatabaseError:
            return {'status': 'unavailable', 'error': 'index_unavailable', 'message': 'Index cannot be read; rebuild it'}

    def build(self, library_id):
        row, folder, index, identity = self.context(library_id)
        files, version = self.snapshot(folder, index)
        self.directory.mkdir(parents=True, exist_ok=True)
        staging = self.directory / (uuid.uuid4().hex + '.tmp')
        try:
            with closing(sqlite3.connect(staging)) as db:
                db.execute('CREATE TABLE metadata(value TEXT NOT NULL)')
                db.execute('CREATE TABLE chunks(id INTEGER PRIMARY KEY,path TEXT,start_line INTEGER,end_line INTEGER,title TEXT,text TEXT,sha256 TEXT,version TEXT)')
                db.execute('CREATE VIRTUAL TABLE terms USING fts5(title,body)')
                count = 0
                for path, text, sha, file_version in files:
                    for first, last, title, body in chunks(text, index['splitter']):
                        if not body.strip():
                            continue
                        count += 1
                        if count > MAX_CHUNKS:
                            raise KnowledgeError('index_limit', 'Too many chunks; narrow the mount', 413)
                        db.execute('INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?)', (count,path,first,last,title,body,sha,file_version))
                        db.execute('INSERT INTO terms(rowid,title,body) VALUES(?,?,?)',
                                   (count, ' '.join(tokens(title)), ' '.join(tokens(body))))
                # Do not replace the last valid generation if inputs changed during build.
                if self.context(library_id)[3] != identity or self.snapshot(folder, index, read=False)[1] != version:
                    raise KnowledgeError('index_source_changed', 'Knowledge or plugin changed; rebuild the index', 409)
                meta = {'engine': ENGINE, 'identity': identity, 'content_version': version,
                        'built_at': now(), 'files': len(files), 'chunks': count}
                db.execute('INSERT INTO metadata VALUES(?)', (json.dumps(meta),))
                db.commit()
            os.replace(staging, self.path(library_id))
            with self.registry.store.transaction(write=True) as db:
                self.registry.store.event(db, {'kind': 'admin', 'action': 'index_build', 'library_id': row['id'], 'status': 'ok'})
            return dict(meta, status='ready')
        except sqlite3.DatabaseError:
            raise KnowledgeError('index_build_failed', 'SQLite FTS5 indexing failed; previous index is retained', 503) from None
        except OSError:
            raise KnowledgeError('index_storage_unavailable', 'Index storage is unavailable or busy; retry', 503) from None
        finally:
            staging.unlink(missing_ok=True)

    def search(self, library_id, query, limit=5):
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200 or not 1 <= limit <= 10:
            raise KnowledgeError('invalid_search', 'Query: 1..200 characters; limit: 1..10', 422)
        terms = list(dict.fromkeys(tokens(query)))
        if not terms:
            raise KnowledgeError('invalid_search', 'Query must contain Chinese characters or English words/numbers', 422)
        _, folder, index, identity = self.context(library_id)
        try:
            with closing(self.connect(library_id)) as db:
                meta = json.loads(db.execute('SELECT value FROM metadata').fetchone()[0])
                if meta['identity'] != identity or self.snapshot(folder, index, read=False)[1] != meta['content_version']:
                    raise KnowledgeError('index_stale', 'Knowledge or plugin changed; rebuild before searching', 409)
                rows = db.execute('SELECT c.*,bm25(terms,2.0,1.0) AS rank FROM terms '
                                  'JOIN chunks c ON c.id=terms.rowid WHERE terms MATCH ? ORDER BY rank,c.id LIMIT ?',
                                  (' OR '.join('"' + term + '"' for term in terms), limit)).fetchall()
                # Validate returned files again, including removed/replaced files and versions.
                for path, sha in {(r['path'], r['sha256']) for r in rows}:
                    with folder._open_text(path) as (stream, _, _):
                        if hashlib.sha256(stream.read(MAX_BYTES + 1)).hexdigest() != sha:
                            raise KnowledgeError('index_stale', 'Source changed during retrieval; rebuild', 409)
                if self.context(library_id)[3] != identity:
                    raise KnowledgeError('index_stale', 'Library configuration changed during retrieval', 409)
                return {'library_id': library_id, 'engine': ENGINE, 'content_version': meta['content_version'],
                        'results': [dict(path=r['path'],start_line=r['start_line'],end_line=r['end_line'],
                                         title=r['title'],text=r['text'],sha256=r['sha256'],version=r['version'],
                                         score=-r['rank']) for r in rows],
                        'notice': 'Evidence only. Cite path and lines; do not invent answers when evidence is insufficient.'}
        except sqlite3.DatabaseError:
            raise KnowledgeError('index_unavailable', 'Index cannot be queried; rebuild it', 503) from None


def add_retrieval_routes(app, registry):
    service = Retrieval(registry)
    app.state.retrieval = service

    @app.get('/admin/api/libraries/{library_id}/index')
    def state(library_id: str):
        return service.state(library_id)

    @app.post('/admin/api/libraries/{library_id}/index/build')
    def build(library_id: str):
        return service.build(library_id)

    @app.get('/admin/api/libraries/{library_id}/search')
    def search(library_id: str, query: str = Query(min_length=1, max_length=200), limit: int = Query(default=5, ge=1, le=10)):
        return service.search(library_id, query, limit)
