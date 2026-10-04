"""Reproducible loopback MCP benchmark; only synthetic, isolated knowledge is used.

Run from the repository: python -m tools.benchmark_file_tools --output data/write-benchmark
Results contain no tokens or document bodies. Setup and verification are outside timings.
"""
import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import platform
import shutil
import socket
import statistics
import subprocess
import threading
import time
import uuid

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import uvicorn

from folder_kb.capabilities import Capabilities, LibraryAgentSettings, Policy
from folder_kb.collaboration import Collaboration
import folder_kb.file_writes as writes
from folder_kb.server import create_app, Settings
from folder_kb.storage import now


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def text(size, number=0, changed=False):
    header = f'# 文档 {number} {"新" if changed else "旧"}\n'.encode()
    return (header + (b'new data.\n' if changed else b'old data.\n') * (size // 10 + 1))[:size].decode()


def disk(root):
    return sum(p.stat().st_size for p in root.rglob('*') if p.is_file())


class Probe:
    def __init__(self, session):
        self.session, self.calls = session, []

    async def call(self, tool, **args):
        start = time.perf_counter()
        result = await self.session.call_tool(tool, args)
        elapsed = (time.perf_counter() - start) * 1000
        content = '\n'.join(x.text for x in result.content if x.type == 'text')
        self.calls.append(dict(tool=tool, ms=round(elapsed, 3), error=bool(result.isError),
                               request_bytes=len(encoded({'name': tool, 'arguments': args})),
                               response_bytes=len(encoded(result.model_dump(exclude_none=True))),
                               error_message=content[:700] if result.isError else None))
        if result.isError:
            return {'_error': content}
        return json.loads(content)


class Suite:
    def __init__(self, output, app_wrapper=None):
        self.output = output.resolve()
        self.run_root = self.output / ('fixture-' + uuid.uuid4().hex)
        pool = self.run_root / 'knowledge'
        pool.mkdir(parents=True)
        self.app = create_app(Settings(str(pool), admin_token=uuid.uuid4().hex,
                                       state_dir=str(self.run_root / 'state')))
        self.registry = self.app.state.registry
        self.actor = {'id': 'benchmark-user', 'username': 'benchmark'}
        with self.registry.store.transaction(write=True) as db:
            db.execute('INSERT INTO users VALUES (?,?,?,?,?)',
                       (self.actor['id'], self.actor['username'], 'login-disabled-test-fixture', 'active', now()))
        self.copies = []
        self.original_copy = writes.copy_workspace
        def measured(source, target, **kwargs):
            capture = kwargs.get('captured')
            start = time.perf_counter()
            try:
                return self.original_copy(source, target, **kwargs)
            finally:
                self.copies.append({'bytes': sum(len(v) for v in capture[1].values()) if capture else None,
                                    'ms': (time.perf_counter() - start) * 1000})
        writes.copy_workspace = measured
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.port = self.sock.getsockname()[1]
        self.url = f'http://127.0.0.1:{self.port}'
        self.server = uvicorn.Server(uvicorn.Config(app_wrapper(self.app) if app_wrapper else self.app, log_level='critical', access_log=False,
                                                   limit_concurrency=32, timeout_keep_alive=10))
        self.thread = threading.Thread(target=self.server.run, kwargs={'sockets': [self.sock]}, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(.02)
        assert self.server.started, 'Benchmark server did not start'
        self.result = {'timestamp': datetime.now(timezone.utc).isoformat(),
                       'environment': {'os': platform.platform(), 'python': platform.python_version(),
                                       'logical_cpus': os.cpu_count(), 'transport': 'MCP Streamable HTTP, loopback',
                                       'server_concurrency_limit': 32,
                                       'packages': {x: importlib.metadata.version(x) for x in ('mcp', 'fastapi', 'uvicorn')},
                                       'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()},
                       'performance': [], 'functional': [], 'observations': {}, 'tool_catalog': {}}

    def setup(self, count=0, size=8192):
        lid = Collaboration(self.registry).create_library(self.actor, '基准测试', None)['id']
        key = self.registry.create_key('benchmark', [lid], None)
        with self.registry.store.transaction(write=True) as db:
            db.execute('INSERT INTO key_owners VALUES (?,?)', (key['key']['id'], self.actor['id']))
        caps = Capabilities(self.registry)
        caps.library_settings(lid, LibraryAgentSettings(direct_enabled=True))
        caps.policy(key['key']['id'], Policy(permissions=[{'library_id': lid, 'write_policy': 'direct'}]))
        root = self.current(lid)
        for i in range(count):
            path = root / self.path(i)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text(size, i), encoding='utf-8', newline='\n')
        return lid, key, root

    @staticmethod
    def path(i):
        return f'指南/分组{i % 4}/{i:03}.md'

    def current(self, lid):
        with self.registry.store.transaction() as db:
            relative = db.execute('SELECT path FROM libraries WHERE id=?', (lid,)).fetchone()[0]
        return self.registry.pool.root / relative

    def footprint(self):
        base = self.registry.pool.root / 'document-releases'
        with self.registry.store.transaction() as db:
            receipts = db.execute('SELECT count(*) FROM file_write_receipts').fetchone()[0]
        return {'snapshots': sum(p.is_dir() for p in base.iterdir()), 'bytes': disk(base), 'receipts': receipts}

    @asynccontextmanager
    async def client(self, key):
        async with httpx.AsyncClient(headers={'Authorization': 'Bearer ' + key['token']}, trust_env=False,
                                     timeout=120) as http:
            async with streamable_http_client(self.url + '/mcp', http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield Probe(session)

    async def performance(self, scenario, count, size, mode, repeat):
        lid, key, root = self.setup(count, size)
        before, copy_start = self.footprint(), len(self.copies)
        initial_bytes = disk(root)
        async with self.client(key) as p:
            # Warm the connection; setup, MCP initialize and this call are not timed.
            await p.session.list_tools()
            start = time.perf_counter()
            changes = []
            if mode == 'batch' and count:
                manifest = await p.call('file_manifest', limit=200)
                assert '_error' not in manifest
            for i in range(28):
                change = {'operation': 'replace' if count else 'create', 'path': self.path(i),
                          'content': text(size, i, changed=True)}
                if not count:
                    change['create_parents'] = True
                if mode == 'single':
                    if count:
                        read = await p.call('read_file', path=self.path(i))
                        change['expected_version'] = read['version']
                    answer = await p.call('write_file', request_id=f'single-{i}', **change)
                    assert '_error' not in answer, answer
                else:
                    changes.append(change)
            if mode == 'batch':
                params = {'changes': changes, 'request_id': 'batch'}
                if count:
                    params['expected_library_version'] = manifest['library_version']
                answer = await p.call('apply_changes', **params)
                assert '_error' not in answer, answer
            elapsed = (time.perf_counter() - start) * 1000
        active = self.current(lid)
        for i in range(28):
            assert (active / self.path(i)).read_text(encoding='utf-8') == text(size, i, changed=True)
        assert len(list(active.rglob('*.md'))) == max(count, 28) + 1
        after = self.footprint()
        row = dict(scenario=scenario, strategy=mode, repeat=repeat, initial_files=count + 1,
                   initial_bytes=initial_bytes, changed_files=28, content_bytes_each=size,
                   elapsed_ms=round(elapsed, 3), calls=len(p.calls),
                   request_bytes=sum(c['request_bytes'] for c in p.calls),
                   response_bytes=sum(c['response_bytes'] for c in p.calls),
                   snapshot_copies=len(self.copies) - copy_start,
                   bytes_copied=sum(c['bytes'] for c in self.copies[copy_start:]),
                   storage_growth_bytes=after['bytes'] - before['bytes'],
                   new_snapshots=after['snapshots'] - before['snapshots'],
                   new_receipts=after['receipts'] - before['receipts'], verified=True, call_log=p.calls)
        self.result['performance'].append(row)
        self.save()
        print(json.dumps({k: row[k] for k in ('scenario', 'strategy', 'repeat', 'elapsed_ms', 'calls',
                                             'snapshot_copies', 'storage_growth_bytes')}, ensure_ascii=False), flush=True)

    async def functional(self):
        lid, key, root = self.setup()
        async with self.client(key) as p:
            catalog = (await p.session.list_tools()).tools
            self.result['tool_catalog']['direct'] = {
                'names': [t.name for t in catalog],
                'tools': [{'name': t.name, 'schema_and_description_bytes': len(encoded(t.model_dump(exclude_none=True)))} for t in catalog]}
            async def check(name, tool, params, expected=None, no_publish=False):
                before = self.footprint()
                old = self.current(lid)
                out = await p.call(tool, **params)
                error = out.get('_error', '')
                ok = bool(expected in error) if expected else not bool(error)
                if no_publish:
                    ok = ok and self.footprint() == before and self.current(lid) == old
                self.result['functional'].append({'name': name, 'passed': ok, 'expected_error': expected,
                                                  'actual_error': error[:700], **p.calls[-1],
                                                  'publication_unchanged_checked': no_publish})
                assert ok, (name, out)
                return out
            await check('nested create with parents', 'write_file', dict(path='资料/项目/a.md', content='# A\n',
                        operation='create', create_parents=True, request_id='nested'))
            await check('missing parent is explicit', 'write_file', dict(path='none/a.md', content='a', operation='create',
                        request_id='missing'), 'parent_not_found', True)
            await check('parent is file', 'write_file', dict(path='README.md/a.md', content='a', operation='create',
                        request_id='parent-file'), 'parent_not_directory', True)
            await check('standalone mkdir', 'apply_changes', dict(changes=[{'operation': 'mkdir', 'path': '空目录'}], request_id='mkdir'))
            await check('late error rolls back all', 'apply_changes', dict(changes=[
                {'operation': 'create', 'path': 'must-not-exist.md', 'content': 'a'},
                {'operation': 'create', 'path': 'README.md', 'content': 'duplicate'}], request_id='rollback'), 'file_exists', True)
            await check('Chinese request id rejected', 'write_file', dict(path='中文.md', content='a', operation='create',
                        request_id='中文文件名'), 'pattern', True)
            await check('batch over 100 rejected', 'apply_changes', dict(changes=[{'operation': 'mkdir', 'path': f'd{i}'} for i in range(101)],
                        request_id='too-many'), '100', True)
            await check('path escape rejected', 'write_file', dict(path='../out.md', content='a', operation='create',
                        request_id='escape'), 'invalid_path', True)
            await check('binary extension rejected', 'write_file', dict(path='code.py', content='print(1)', operation='create',
                        request_id='extension'), 'unsupported_document', True)
            params = dict(changes=[{'operation': 'create', 'path': 'retry.md', 'content': 'retry'}], request_id='idempotent')
            original = await check('initial idempotent write', 'apply_changes', params)
            retry = await check('identical retry adds no snapshot', 'apply_changes', params, no_publish=True)
            assert retry['duplicate'] and retry['library_version'] == original['library_version']
            await check('changed retry rejected', 'apply_changes', dict(params, changes=[{'operation': 'mkdir', 'path': 'other'}]),
                        'request_conflict', True)
            m = await p.call('file_manifest', limit=200)
            await check('nonempty deletion requires recursive', 'apply_changes', dict(request_id='nonempty', expected_library_version=m['library_version'],
                        changes=[{'operation': 'delete', 'path': '资料'}]), 'directory_not_empty', True)
            await check('delete empty directory', 'apply_changes', dict(request_id='empty-delete', expected_library_version=m['library_version'],
                        changes=[{'operation': 'delete', 'path': '空目录'}]))
            await check('stale library version rejected', 'apply_changes', dict(request_id='stale', expected_library_version=m['library_version'],
                        changes=[{'operation': 'delete', 'path': 'retry.md'}]), 'version_changed', True)
            m = await p.call('file_manifest', limit=200)
            await check('recursive deletion', 'apply_changes', dict(request_id='recursive', expected_library_version=m['library_version'],
                        changes=[{'operation': 'delete', 'path': '资料', 'recursive': True}]))
            read = await p.call('read_file', path='retry.md')
            await check('delete file with CAS', 'apply_changes', dict(request_id='delete-file', changes=[
                {'operation': 'delete', 'path': 'retry.md', 'expected_version': read['version']}]))
            # Unchanged file metadata tokens are invalidated by snapshot publication too.
            before = await p.call('read_file', path='README.md')
            await p.call('write_file', path='unrelated.md', content='other', operation='create', request_id='unrelated')
            after = await p.call('read_file', path='README.md')
            self.result['observations']['unchanged_file_version_changed'] = before['version'] != after['version']
            await check('unchanged file old version rejected', 'write_file', dict(path='README.md', content='update', operation='replace',
                        expected_version=before['version'], request_id='unchanged-cas'), 'version_changed', True)
            # Whole-tree CAS: five independently submitted writes based on the same version.
            m = await p.call('file_manifest')
            before = self.footprint()
            started = time.perf_counter()
            outcomes = await asyncio.gather(*(p.call('apply_changes', request_id=f'race-{i}', expected_library_version=m['library_version'],
                changes=[{'operation': 'create', 'path': f'race-{i}.md', 'content': str(i)}]) for i in range(5)))
            self.result['observations']['concurrent_same_version'] = {
                'requests': 5, 'successes': sum('_error' not in r for r in outcomes),
                'conflicts': sum('version_changed' in r.get('_error', '') for r in outcomes),
                'elapsed_ms': round((time.perf_counter() - started) * 1000, 3),
                'new_snapshots': self.footprint()['snapshots'] - before['snapshots']}
            assert self.result['observations']['concurrent_same_version']['successes'] == 1
            assert self.result['observations']['concurrent_same_version']['conflicts'] == 4
            Capabilities(self.registry).policy(key['key']['id'], Policy(permissions=[{'library_id': lid}]))
            self.result['tool_catalog']['readonly_names'] = [t.name for t in (await p.session.list_tools()).tools]
            await check('revoked direct permission blocks retry', 'apply_changes', params, 'capability_forbidden', True)
        # 128 files force pagination with the tool's default 100-entry page.
        lid2, key2, _ = self.setup(128, 8192)
        async with self.client(key2) as p2:
            a = await p2.call('file_manifest')
            b = await p2.call('file_manifest', offset=a['next_offset'], expected_library_version=a['library_version'])
            self.result['observations']['default_manifest_pagination'] = {'calls': len(p2.calls),
                'first_page': len(a['entries']), 'second_page': len(b['entries']), 'next_offset': b['next_offset']}
            assert b['next_offset'] is None
        self.save()

    def save(self):
        self.output.mkdir(parents=True, exist_ok=True)
        target = self.output / 'results.json'
        target.write_text(json.dumps(self.result, ensure_ascii=False, indent=2), encoding='utf-8')

    def close(self):
        self.server.should_exit = True
        self.thread.join(15)
        self.sock.close()
        writes.copy_workspace = self.original_copy
        # Only this run's generated fixture can be removed; never a user knowledge root.
        target = self.run_root.resolve()
        assert target.parent == self.output and target.name.startswith('fixture-') and not self.thread.is_alive()
        shutil.rmtree(target)


async def run(suite, repeats, scenarios):
    await suite.functional()
    for repeat in range(1, repeats + 1):
        for scenario, count, size in [('import-28', 0, 8192), ('replace-small', 32, 8192), ('replace-medium', 128, 32768)]:
            if scenario not in scenarios:
                continue
            # Alternate order to reduce a consistent warm-cache/order advantage.
            for mode in (('single', 'batch') if repeat % 2 else ('batch', 'single')):
                await suite.performance(scenario, count, size, mode, repeat)
    groups = {}
    for row in suite.result['performance']:
        groups.setdefault((row['scenario'], row['strategy']), []).append(row)
    suite.result['summary'] = [dict(scenario=k[0], strategy=k[1], samples=len(rows),
        median_ms=round(statistics.median(r['elapsed_ms'] for r in rows), 3),
        min_ms=min(r['elapsed_ms'] for r in rows), max_ms=max(r['elapsed_ms'] for r in rows)) for k, rows in groups.items()]
    suite.save()


def main():
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('data/write-benchmark'))
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--scenarios', nargs='+', choices=['import-28', 'replace-small', 'replace-medium'],
                        default=['import-28', 'replace-small', 'replace-medium'])
    args = parser.parse_args()
    assert 1 <= args.repeats <= 10
    suite = Suite(args.output)
    try:
        asyncio.run(run(suite, args.repeats, args.scenarios))
    finally:
        suite.save()
        suite.close()
    print('Saved ' + str(args.output / 'results.json'), flush=True)


if __name__ == '__main__':
    main()
