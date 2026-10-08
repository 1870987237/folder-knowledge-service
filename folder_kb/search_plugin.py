"""Protocol runner for administrator-reviewed search packages, not a sandbox.

Plugins own their artifacts, tokenization, retrieval and ordering. Only bounded
snapshots of the mounted knowledge view are supplied; no model credentials or
other libraries are passed. Legacy declarative indexing uses a separate backend.
"""
import json
import hashlib
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile

from .core import is_link
from .errors import KnowledgeError
from .template_protocol import tree_digest
from .templates import relative_path

ARTIFACT_BYTES = 64 * 1024 * 1024


def artifact_digest(root):
    total = count = 0
    entries = []
    if is_link(root.lstat()) or not root.is_dir():
        raise ValueError('Linked artifact directory')
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            count += 1
            path = Path(base) / name
            if is_link(path.lstat()) or not path.resolve().is_relative_to(root.resolve()):
                raise ValueError('Unsafe artifact path')
            if path.is_file():
                total += path.stat().st_size
            elif not path.is_dir():
                raise ValueError('Special artifact file')
            if count > 2000 or total > ARTIFACT_BYTES:
                raise ValueError('Artifact quota exceeded')
            value = 'directory'
            if path.is_file():
                with path.open('rb') as stream:
                    value = hashlib.file_digest(stream, 'sha256').hexdigest()
            entries.append((path.relative_to(root).as_posix(), value))
    return hashlib.sha256(json.dumps(sorted(entries)).encode()).hexdigest()


def approved(index, catalog):
    definition = index['_definition']
    allowed = {x.strip() for x in os.getenv('KB_SEARCH_PLUGIN_ALLOWLIST', '').split(',') if x.strip()}
    if (os.getenv('KB_SEARCH_TRUSTED_PYTHON') != '1' or
            definition['runtime']['type'] != 'trusted-python-demo' or index['_package_sha256'] not in allowed):
        raise KnowledgeError('search_runtime_disabled', 'Enable reviewed Python search and allowlist this installed package digest', 409)
    package = catalog.version_path(definition['id'], definition['version']) / 'package'
    try:
        if tree_digest(package) != index['_package_sha256']:
            raise ValueError('Package changed')
    except (OSError, ValueError):
        raise KnowledgeError('search_package_changed', 'Installed search package is missing or changed', 409) from None
    return package


def run_process(command, cwd, timeout):
    env = {k: os.environ[k] for k in ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'PATH') if k in os.environ}
    env['PYTHONIOENCODING'] = 'utf-8'
    options = {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    with subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False, **options) as process:
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise KnowledgeError('search_plugin_timeout', 'Search plugin exceeded its time budget', 504) from None
        if process.returncode:
            raise KnowledgeError('search_plugin_failed', 'Search plugin failed; no new index was published', 502)


def execute(directory, catalog, index, request, artifacts=None, destination=None):
    package = approved(index, catalog)
    directory.mkdir(parents=True, exist_ok=True)
    if is_link(directory.lstat()):
        raise KnowledgeError('search_storage_unsafe', 'Unsafe search runtime directory', 409)
    try:
        with tempfile.TemporaryDirectory(prefix='search-', dir=directory) as temporary:
            root = Path(temporary).resolve()
            if root.parent != directory.resolve():
                raise ValueError('Runtime escaped its root')
            inputs, output = root / 'input', root / 'output'
            inputs.mkdir(); output.mkdir()
            if artifacts is not None:
                artifact_digest(artifacts)
                shutil.copytree(artifacts, inputs / 'index')
            request = dict(request, protocol_version='1.0', config=index['_config'],
                           indexing={k: v for k, v in index.items() if not k.startswith('_')},
                           index_dir=str(inputs / 'index') if artifacts is not None else None)
            (inputs / 'request.json').write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
            before = artifact_digest(inputs)
            argv = index['entrypoint']
            command = [sys.executable, '-I', '-B', str(package / argv[1]), *argv[2:],
                       '--input', str(inputs / 'request.json'), '--output', str(output)]
            timeout = min(index['_definition']['runtime']['timeout_seconds'], 30 if artifacts else 300)
            run_process(command, root, timeout)
            if artifact_digest(inputs) != before or tree_digest(package) != index['_package_sha256']:
                raise ValueError('Plugin modified its input/package')
            artifact_digest(output)
            result_path = output / 'result.json'
            if result_path.stat().st_size > 1024 * 1024:
                raise ValueError('Response too large')
            result = json.loads(result_path.read_text(encoding='utf-8'))
            if not isinstance(result, dict) or result.get('protocol_version') != '1.0':
                raise ValueError('Invalid response protocol')
            if destination is not None:
                if set(result) != {'protocol_version', 'engine', 'chunks'} or not isinstance(result['engine'], str) or not 1 <= len(result['engine']) <= 100:
                    raise ValueError('Invalid build result')
                if type(result['chunks']) is not int or not 0 <= result['chunks'] <= 1000000:
                    raise ValueError('Invalid chunk count')
                artifact_digest(output / 'index')
                shutil.copytree(output / 'index', destination)
            return result
    except KnowledgeError:
        raise
    except (ValueError, OSError, TypeError, KeyError):
        raise KnowledgeError('search_plugin_invalid', 'Invalid search plugin output, input mutation or storage failure', 502) from None


def validate_hits(result, documents, limit, path):
    if (set(result) != {'protocol_version', 'results', 'has_more'} or type(result['has_more']) is not bool
            or not isinstance(result['results'], list) or len(result['results']) > limit):
        raise ValueError('Invalid query response')
    known = {p: (text, sha, version) for p, text, sha, version in documents}
    hits = []
    for item in result['results']:
        if not isinstance(item, dict) or set(item) != {'path', 'start_line', 'end_line', 'text', 'title', 'score'}:
            raise ValueError('Invalid hit fields')
        relative_path(item['path'])
        if item['path'] not in known or (path != '.' and not item['path'].startswith(path + '/')):
            raise ValueError('Result outside indexed/query scope')
        text, sha, version = known[item['path']]
        start, end = item['start_line'], item['end_line']
        lines = text.splitlines(keepends=True)
        if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines):
            raise ValueError('Invalid citation lines')
        if (not isinstance(item['text'], str) or not 1 <= len(item['text']) <= 8000 or
                item['text'] not in ''.join(lines[start - 1:end])):
            raise ValueError('Evidence must be a verbatim excerpt of the cited lines')
        if not isinstance(item['title'], str) or len(item['title']) > 300:
            raise ValueError('Invalid title')
        if type(item['score']) not in (float, int) or not math.isfinite(item['score']):
            raise ValueError('Invalid score')
        hits.append(dict(item, sha256=sha, version=version))
    return hits
