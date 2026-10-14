"""Search protocol 1.1: streamed build input and shared immutable index generations.

Only administrator-reviewed Python plugins are allowed. This is not a sandbox:
the runtime's trust boundary is unchanged. Unlike 1.0, input/artifact contents
are not copied or hashed in full on every query. Source and artifact stat tokens
are checked before/after calls; returned evidence is re-read and verified.
"""
from contextlib import contextmanager
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import time
import uuid

from .core import is_link, matches
from .errors import KnowledgeError
from .filesystem import native_path
from .search_plugin import approved, run_process, validate_hits
from .storage import now
from .streaming import file_version

MAX_FILES = 150000
MAX_BYTES = 8 * 1024 ** 3
MAX_FILE_BYTES = 128 * 1024 ** 2
MAX_ARTIFACT_BYTES = 24 * 1024 ** 3


def guarded(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except KnowledgeError:
            raise
        except (OSError, ValueError, TypeError, KeyError, UnicodeError):
            raise KnowledgeError('search_plugin_invalid','Invalid search plugin output, source or storage failure',502) from None
    return wrapper


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def scan(folder, index):
    """Bounded stat-only scan; link/special-file safety matches KnowledgeFolder."""
    root = folder.root
    if is_link(native_path(root).lstat()):
        raise KnowledgeError('index_source_unsafe', 'Index root must not be linked', 409)
    stack = [root]; files = []; total = count = 0; deadline = time.monotonic() + 300
    while stack:
        base = stack.pop()
        with os.scandir(native_path(base)) as children:
            for child in children:
                count += 1
                if count > MAX_FILES + 50000 or time.monotonic() > deadline:
                    raise KnowledgeError('index_limit', 'Large index scan budget exceeded', 413)
                if child.name.startswith('.'):
                    continue
                path = base / child.name; st = native_path(path).lstat()
                if is_link(st):
                    continue
                if stat.S_ISDIR(st.st_mode):
                    stack.append(path); continue
                name = path.relative_to(root).as_posix()
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or not folder._readable(path):
                    continue
                if not any(matches(name, p) for p in index['include']) or any(matches(name, p) for p in index['exclude']):
                    continue
                total += st.st_size
                if len(files) >= MAX_FILES or total > MAX_BYTES or st.st_size > MAX_FILE_BYTES:
                    raise KnowledgeError('index_limit', 'Large index source exceeds file/byte quota', 413)
                files.append((name, file_version(st), st.st_size))
    files.sort()
    return files, digest(files)


def artifacts(root, *, content=False):
    root = Path(root)
    if not root.is_dir() or is_link(root.lstat()):
        raise ValueError('Unsafe index artifact directory')
    entries = []; size = 0
    for base, dirs, names in os.walk(root, followlinks=False):
        for name in dirs + names:
            path = Path(base) / name; st = path.lstat()
            if is_link(st) or not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)) or (path.is_file() and st.st_nlink != 1):
                raise ValueError('Unsafe index artifact')
            if len(entries) >= 2000:
                raise ValueError('Too many index artifacts')
            value = 'directory'
            if path.is_file():
                size += st.st_size
                if size > MAX_ARTIFACT_BYTES:
                    raise ValueError('Index artifact byte quota exceeded')
                if content:
                    with path.open('rb') as stream:
                        value = hashlib.file_digest(stream, 'sha256').hexdigest()
                else:
                    value = [file_version(st), st.st_size]
            entries.append([path.relative_to(root).as_posix(), value])
    return digest(sorted(entries))


def execute(root, catalog, index, request, *, output, artifact_dir=None):
    """A fresh process per call; large index is opened read-only by the plugin."""
    package = approved(index, catalog)
    output.mkdir()
    request = dict(request, protocol_version='1.1', config=index['_config'],
        indexing={k:v for k,v in index.items() if not k.startswith('_')},
        index_dir=str(artifact_dir) if artifact_dir else None)
    request_file = root / 'request.json'
    request_file.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
    before = artifacts(artifact_dir) if artifact_dir else None
    argv = index['entrypoint']
    run_process([sys.executable, '-I', '-B', str(package/argv[1]), *argv[2:],
                 '--input', str(request_file), '--output', str(output)], root,
                min(index['_definition']['runtime']['timeout_seconds'], 60 if artifact_dir else 3600))
    approved(index, catalog)
    if artifact_dir and artifacts(artifact_dir) != before:
        raise KnowledgeError('search_plugin_invalid', 'Plugin changed shared index artifacts', 502)
    result_file = output / 'result.json'
    if is_link(result_file.lstat()) or result_file.stat().st_size > 1024 * 1024:
        raise ValueError('Unsafe or oversized plugin result')
    result = json.loads(result_file.read_text(encoding='utf-8'))
    if result.get('protocol_version') != '1.1':
        raise ValueError('Invalid search protocol response')
    return result


class LargeSearch:
    def __init__(self, retrieval):
        self.service = retrieval

    @contextmanager
    def runtime(self):
        root = self.service.directory / 'runtime'
        root.mkdir(parents=True, exist_ok=True)
        if is_link(root.lstat()):
            raise KnowledgeError('search_storage_unsafe', 'Unsafe runtime directory', 409)
        with tempfile.TemporaryDirectory(prefix='large-', dir=root) as temporary:
            yield Path(temporary).resolve()

    def generation(self, lid):
        pointer = self.service.plugin_pointer(lid)
        if not pointer.is_file():
            raise KnowledgeError('index_missing', 'Build the library index first', 409)
        meta = json.loads(pointer.read_text(encoding='utf-8'))
        if meta.get('search_protocol') != '1.1' or not re.fullmatch('[0-9a-f]{32}', meta.get('generation', '')):
            raise ValueError('Invalid generation metadata')
        root = self.service.directory / (self.service.path(lid).stem + '-generations') / meta['generation']
        if artifacts(root) != meta['artifact_stat_sha256']:
            raise KnowledgeError('index_unavailable', 'Index artifacts changed; rebuild', 409)
        return meta, root

    @guarded
    def state(self, lid, folder, index, identity):
        approved(index, self.service.catalog)
        meta, _ = self.generation(lid)
        _, version = scan(folder, index)
        return dict(meta, status='ready' if meta['identity']==identity and meta['content_version']==version else 'stale',
                    supports_path_filter=index.get('supports_path_filter', False))

    @guarded
    def build(self, lid, folder, index, identity):
        approved(index, self.service.catalog)
        files, version = scan(folder, index)
        self.service.directory.mkdir(parents=True, exist_ok=True)
        generations = self.service.directory / (self.service.path(lid).stem + '-generations')
        generations.mkdir(exist_ok=True)
        if is_link(generations.lstat()):
            raise KnowledgeError('search_storage_unsafe', 'Unsafe generation directory', 409)
        generation = uuid.uuid4().hex; target = generations/generation
        with self.runtime() as root:
            documents = root/'documents.jsonl'
            with documents.open('w', encoding='utf-8', newline='\n') as out:
                for name, expected, _ in files:
                    with folder._open_text(name) as (stream, _, token):
                        raw = stream.read(MAX_FILE_BYTES+1)
                    if token != expected or len(raw)>MAX_FILE_BYTES:
                        raise KnowledgeError('index_source_changed', 'Source changed during index preparation', 409)
                    text = raw.decode('utf-8-sig')
                    if '\x00' in text:
                        raise KnowledgeError('index_encoding', 'Index requires UTF-8 without NUL', 415)
                    out.write(json.dumps(dict(path=name, text=text, sha256=hashlib.sha256(raw).hexdigest(), version=token),ensure_ascii=False)+'\n')
            output = root/'output'
            result = execute(root, self.service.catalog, index,
                dict(operation='build', library_id=lid, content_version=version, documents_path=str(documents)), output=output)
            if (set(result) != {'protocol_version','engine','chunks'} or not isinstance(result['engine'],str)
                    or not 1 <= len(result['engine']) <= 100 or type(result['chunks']) is not int or result['chunks'] < 0):
                raise ValueError('Invalid build result')
            artifact_sha = artifacts(output/'index', content=True)
            if self.service.context(lid)[3] != identity or scan(folder,index)[1] != version:
                raise KnowledgeError('index_source_changed', 'Source or binding changed during index build', 409)
            if target.resolve().parent != generations.resolve() or not (output/'index').resolve().is_relative_to(root):
                raise ValueError('Unsafe artifact publication path')
            shutil.move(str(output/'index'), str(target))
            meta = dict(search_protocol='1.1',engine=result['engine'],identity=identity,content_version=version,
                built_at=now(),files=len(files),chunks=result['chunks'],generation=generation,
                artifact_sha256=artifact_sha,artifact_stat_sha256=artifacts(target),source_validation='stat-manifest-and-returned-evidence')
            pointer = self.service.plugin_pointer(lid); temp=pointer.with_suffix('.'+generation+'.tmp')
            temp.write_text(json.dumps(meta),encoding='utf-8'); os.replace(temp,pointer)
        with self.service.registry.store.transaction(write=True) as db:
            self.service.registry.store.event(db,{'kind':'admin','action':'index_build','library_id':lid,'status':'ok'})
        return dict(meta,status='ready')

    @guarded
    def search(self, lid, query, limit, path, folder, index, identity):
        approved(index, self.service.catalog)
        if path != '.' and not index.get('supports_path_filter'):
            raise KnowledgeError('search_filter_unsupported','Plugin does not support directory filters',422)
        meta, artifacts_path = self.generation(lid)
        files, version = scan(folder,index)
        if meta['identity'] != identity or meta['content_version'] != version:
            raise KnowledgeError('index_stale','Source or plugin changed; rebuild before searching',409)
        with self.runtime() as root:
            result = execute(root,self.service.catalog,index,
                dict(operation='search',library_id=lid,content_version=version,query=query,limit=limit,path=path),
                output=root/'output',artifact_dir=artifacts_path)
        known = {name:token for name,token,_ in files}; documents=[]
        hits=result.get('results',[])
        if not isinstance(hits,list) or len(hits)>limit:
            raise ValueError('Invalid hits')
        for name in {h['path'] for h in hits}:
            if name not in known:
                raise ValueError('Hit outside indexed corpus')
            with folder._open_text(name) as (stream,_,token):
                raw=stream.read(MAX_FILE_BYTES+1)
            if len(raw)>MAX_FILE_BYTES or token!=known[name]:
                raise KnowledgeError('index_stale','Returned source changed; rebuild',409)
            documents.append((name,raw.decode('utf-8-sig'),hashlib.sha256(raw).hexdigest(),token))
        validated=validate_hits(result,documents,limit,path)
        if self.service.context(lid)[3]!=identity or scan(folder,index)[1]!=version:
            raise KnowledgeError('index_stale','Source changed during search',409)
        return self.service.response(lid,index,meta,validated,result['has_more'],path)
