"""Search routing, source boundaries and atomic publication.

Executable plugins own retrieval. Declarative v1 packages retain their legacy
BM25 backend for compatibility; its policy is not applied to custom plugins.
"""
from contextlib import closing
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid

from fastapi import Query, Request

from .core import matches
from .errors import KnowledgeError
from .storage import now
from .templates import TemplateCatalog
from .templates import relative_path
from . import search_plugin, legacy_search
from .legacy_search import ENGINE, chunks  # compatibility for benchmark imports

MAX_BYTES = 16 * 1024 * 1024
MAX_FILES = 1000
MAX_CHUNKS = 16000


def supported(index):
    return bool(index and (index.get('search_protocol') in ('1.0', '1.1') and index.get('entrypoint') or
                index.get('mode') == 'fulltext' and index.get('splitter') in ('document', 'markdown_sections') and not index.get('entrypoint')))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


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
            from .plugin_bindings import search_binding
            binding = search_binding(db, library_id)
            if not binding:
                raise KnowledgeError('index_not_configured', 'Bind an indexing plugin first', 409)
            from .resource_access import check_binding_access
            check_binding_access(db, library_id, binding)
            installed = self.catalog.require(db, binding['template_id'], binding['template_version'])
            definition = json.loads(installed['metadata_json'])
            index = (definition.get('plugin') or {}).get('indexing')
            if not index:
                raise KnowledgeError('index_not_configured', 'The bound plugin does not define an index', 409)
            if not supported(index):
                raise KnowledgeError('index_unsupported', 'Plugin must declare search_protocol 1.0/1.1 or legacy fulltext indexing', 409)
            identity = digest([ENGINE, row['path'], definition, json.loads(binding['config_json'])])
            if index.get('search_protocol'):
                identity = digest([row['path'], definition, installed['package_sha256'], json.loads(binding['config_json'])])
                index = dict(index, _definition=definition, _package_sha256=installed['package_sha256'],
                             _config=json.loads(binding['config_json']))
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
            if index.get('search_protocol') == '1.1':
                from .search_large import LargeSearch
                return LargeSearch(self).state(library_id, folder, index, identity)
            if index.get('search_protocol'):
                search_plugin.approved(index, self.catalog)
                meta, _ = self.plugin_generation(library_id)
                _, version = self.snapshot(folder, index, read=False)
                return dict(meta, status='ready' if meta['identity'] == identity and meta['content_version'] == version else 'stale',
                            supports_path_filter=index.get('supports_path_filter', False))
            with closing(self.connect(library_id)) as db:
                meta = json.loads(db.execute('SELECT value FROM metadata').fetchone()[0])
            _, version = self.snapshot(folder, index, read=False)
            return dict(meta, status='ready' if meta['identity'] == identity and meta['content_version'] == version else 'stale', supports_path_filter=True)
        except KnowledgeError as exc:
            return {'status': {'index_missing': 'missing', 'index_not_configured': 'unconfigured',
                               'index_unsupported': 'unsupported'}.get(exc.code, 'unavailable'),
                    'error': exc.code, 'message': exc.message}
        except sqlite3.DatabaseError:
            return {'status': 'unavailable', 'error': 'index_unavailable', 'message': 'Index cannot be read; rebuild it'}

    def build(self, library_id, *, progress=None):
        row, folder, index, identity = self.context(library_id)
        if index.get('search_protocol') == '1.1':
            from .search_large import LargeSearch
            return LargeSearch(self).build(library_id, folder, index, identity, progress=progress)
        files, version = self.snapshot(folder, index)
        if index.get('search_protocol'):
            return self.build_plugin(library_id, index, identity, files, version)
        self.directory.mkdir(parents=True, exist_ok=True)
        staging = self.directory / (uuid.uuid4().hex + '.tmp')
        try:
            with closing(sqlite3.connect(staging)) as db:
                db.execute('CREATE TABLE metadata(value TEXT NOT NULL)')
                count = legacy_search.build(db, files, index, MAX_CHUNKS)
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

    def plugin_pointer(self, library_id):
        return self.path(library_id).with_suffix('.plugin.json')

    def plugin_generation(self, library_id):
        pointer = self.plugin_pointer(library_id)
        if not pointer.is_file():
            raise KnowledgeError('index_missing', 'Build the library index first', 409)
        try:
            meta = json.loads(pointer.read_text(encoding='utf-8'))
            generation = meta['generation']
            if not isinstance(generation, str) or not re.fullmatch('[0-9a-f]{32}', generation):
                raise ValueError('Invalid generation')
            artifact = self.directory / (self.path(library_id).stem + '-generations') / generation
            if search_plugin.artifact_digest(artifact) != meta['artifact_sha256']:
                raise ValueError('Artifact changed')
            return meta, artifact
        except (OSError, ValueError, KeyError):
            raise KnowledgeError('index_unavailable', 'Plugin index is missing or changed; rebuild it', 503) from None

    def build_plugin(self, library_id, index, identity, files, version):
        search_plugin.approved(index, self.catalog)
        self.directory.mkdir(parents=True, exist_ok=True)
        generations = self.directory / (self.path(library_id).stem + '-generations')
        generations.mkdir(exist_ok=True)
        from .core import is_link
        if is_link(generations.lstat()):
            raise KnowledgeError('search_storage_unsafe', 'Unsafe index generation directory', 409)
        generation = uuid.uuid4().hex
        destination = generations / generation
        temporary = self.directory / (generation + '.tmp')
        published = False
        try:
            output = search_plugin.execute(self.directory / 'runtime', self.catalog, index,
                {'operation': 'build', 'library_id': library_id, 'content_version': version,
                 'documents': [{'path': p, 'text': text, 'sha256': sha, 'version': v} for p, text, sha, v in files]},
                destination=destination)
            _, current_folder, current_index, current_identity = self.context(library_id)
            if current_identity != identity or self.snapshot(current_folder, current_index, read=False)[1] != version:
                raise KnowledgeError('index_source_changed', 'Knowledge or plugin changed during build; retry', 409)
            meta = {'engine': output['engine'], 'identity': identity, 'content_version': version,
                    'built_at': now(), 'files': len(files), 'chunks': output['chunks'], 'generation': generation,
                    'artifact_sha256': search_plugin.artifact_digest(destination)}
            temporary.write_text(json.dumps(meta), encoding='utf-8')
            os.replace(temporary, self.plugin_pointer(library_id))
            published = True
            with self.registry.store.transaction(write=True) as db:
                self.registry.store.event(db, {'kind': 'admin', 'action': 'index_build', 'library_id': library_id, 'status': 'ok'})
            return dict(meta, status='ready')
        except OSError:
            raise KnowledgeError('index_storage_unavailable', 'Index storage unavailable; previous index retained', 503) from None
        finally:
            temporary.unlink(missing_ok=True)
            if not published and destination.exists() and destination.resolve().parent == generations.resolve():
                import shutil
                shutil.rmtree(destination)

    def search_plugin(self, library_id, query, limit, path, folder, index, identity):
        search_plugin.approved(index, self.catalog)
        if path != '.' and not index.get('supports_path_filter'):
            raise KnowledgeError('search_filter_unsupported', 'This plugin does not support a directory filter', 422)
        meta, artifacts = self.plugin_generation(library_id)
        documents, version = self.snapshot(folder, index)
        if meta['identity'] != identity or meta['content_version'] != version:
            raise KnowledgeError('index_stale', 'Knowledge or plugin changed; rebuild before searching', 409)
        output = search_plugin.execute(self.directory / 'runtime', self.catalog, index,
            {'operation': 'search', 'library_id': library_id, 'content_version': version,
             'query': query, 'limit': limit, 'path': path}, artifacts=artifacts)
        try:
            hits = search_plugin.validate_hits(output, documents, limit, path)
        except (ValueError, TypeError, KeyError):
            raise KnowledgeError('search_plugin_invalid', 'Plugin returned invalid evidence or results outside the requested scope', 502) from None
        _, current_folder, current_index, current_identity = self.context(library_id)
        if current_identity != identity or self.snapshot(current_folder, current_index, read=False)[1] != version:
            raise KnowledgeError('index_stale', 'Source changed during retrieval; retry after rebuilding', 409)
        return self.response(library_id, index, meta, hits, output['has_more'], path)

    @staticmethod
    def response(library_id, index, meta, hits, has_more, path):
        return {'library_id': library_id, 'engine': meta['engine'], 'content_version': meta['content_version'],
                'results': hits, 'has_more': has_more, 'index_status': 'ready',
                'scope': {'path': path, 'include': index['include'], 'exclude': index['exclude'], 'indexed_files': meta['files']},
                'notice': 'Ranked evidence, not an exhaustive answer. No hits do not prove absence. Scores are plugin-specific. Cite paths and lines; read_file for more context.'}

    def search(self, library_id, query, limit=5, path='.'):
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200 or not 1 <= limit <= 10:
            raise KnowledgeError('invalid_search', 'Query: 1..200 characters; limit: 1..10', 422)
        if path != '.':
            try:
                relative_path(path)
            except (TypeError, ValueError):
                raise KnowledgeError('invalid_path', 'Search path must be a canonical relative directory', 422) from None
        _, folder, index, identity = self.context(library_id)
        if index.get('search_protocol') == '1.1':
            from .search_large import LargeSearch
            return LargeSearch(self).search(library_id, query, limit, path, folder, index, identity)
        if index.get('search_protocol'):
            return self.search_plugin(library_id, query, limit, path, folder, index, identity)
        try:
            with closing(self.connect(library_id)) as db:
                meta = json.loads(db.execute('SELECT value FROM metadata').fetchone()[0])
                if meta['identity'] != identity or self.snapshot(folder, index, read=False)[1] != meta['content_version']:
                    raise KnowledgeError('index_stale', 'Knowledge or plugin changed; rebuild before searching', 409)
                rows, has_more = legacy_search.search(db, query, limit, path)
                # Validate returned files again, including removed/replaced files and versions.
                for source_path, sha in {(r['path'], r['sha256']) for r in rows}:
                    with folder._open_text(source_path) as (stream, _, _):
                        if hashlib.sha256(stream.read(MAX_BYTES + 1)).hexdigest() != sha:
                            raise KnowledgeError('index_stale', 'Source changed during retrieval; rebuild', 409)
                if self.context(library_id)[3] != identity:
                    raise KnowledgeError('index_stale', 'Library configuration changed during retrieval', 409)
                return self.response(library_id, index, meta,
                        [dict(path=r['path'],start_line=r['start_line'],end_line=r['end_line'],
                                         title=r['title'],text=r['text'],sha256=r['sha256'],version=r['version'],
                                         score=-r['rank']) for r in rows], has_more, path)
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
    def search(library_id: str, query: str = Query(min_length=1, max_length=200), limit: int = Query(default=5, ge=1, le=10), path: str = '.'):
        return service.search(library_id, query, limit, path)

    def authorize_configuration(request, library_id):
        from .collaboration import Collaboration
        with registry.store.transaction() as db:
            Collaboration(registry).check_configuration(db, request.state.human, library_id)

    @app.get('/portal/api/libraries/{library_id}/index')
    def portal_state(library_id: str, request: Request):
        authorize_configuration(request, library_id)
        return service.state(library_id)

    @app.post('/portal/api/libraries/{library_id}/index/build')
    def portal_build(library_id: str, request: Request):
        authorize_configuration(request, library_id)
        return service.build(library_id)

    @app.get('/portal/api/libraries/{library_id}/index/search')
    def portal_search(library_id: str, request: Request, query: str = Query(min_length=1, max_length=200),
                      limit: int = Query(default=5, ge=1, le=10), path: str = '.'):
        authorize_configuration(request, library_id)
        return service.search(library_id, query, limit, path)
