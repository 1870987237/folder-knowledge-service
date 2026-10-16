"""Search benchmark: synthetic quality checks and privacy-filtered live probes.

Live mode never prints/saves snippets, titles, paths, credentials or index contents.
It does not read knowledge files or rebuild/change the live index. Temporary scoped
search keys expire in one hour and are revoked in finally. Only aggregate results survive.
"""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import statistics
import time
import uuid

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from folder_kb.capabilities import Capabilities, Policy
from folder_kb.plugin_package import package_plugin
from tools.benchmark_file_tools import Suite


def summary(values):
    return {'samples': len(values), 'median_ms': round(statistics.median(values), 3),
            'min_ms': round(min(values), 3), 'max_ms': round(max(values), 3)}


async def synthetic(output):
    output.mkdir(parents=True, exist_ok=True)
    suite = Suite(output, short_fixture=True)
    saved_env = {key: os.environ.get(key) for key in (
        "KB_SEARCH_TRUSTED_PYTHON", "KB_SEARCH_PLUGIN_ALLOWLIST")}
    result = {'environment': suite.result['environment'], 'datasets': [], 'lifecycle': {}}
    try:
        admin = suite.app.user_middleware[0].kwargs['settings'].admin_token
        with httpx.Client(base_url=suite.url, headers={'Authorization': 'Bearer ' + admin}, trust_env=False, timeout=120) as c:
            pkg = package_plugin(Path('examples/plugins/bm25-markdown/package'),
                                 Path('examples/plugins/bm25-markdown/definition.json'), suite.run_root / 'package')
            response = c.post('/admin/api/plugins', json=json.loads(Path(pkg['manifest']).read_text(encoding='utf-8')))
            assert response.status_code == 201
            installed = c.put('/admin/api/plugins/bm25-markdown/versions/1.0.1/package', content=Path(pkg['package']).read_bytes())
            assert installed.status_code == 200
            # This harness executes only the bundled plugin in its isolated service.
            os.environ['KB_SEARCH_TRUSTED_PYTHON'] = '1'
            os.environ['KB_SEARCH_PLUGIN_ALLOWLIST'] = installed.json()['package_sha256']
            result['plugin'] = {'id': 'bm25-markdown', 'version': '1.0.1',
                                'package_sha256': installed.json()['package_sha256']}
            for count, size in [(32, 8192), (128, 32768)]:
                lid, key, root = suite.setup(count, size)
                docs = {
                    'security.md': '# 访问密钥\n管理员生成访问密钥。请勿跨组织共享。\n',
                    'deployment.md': '# Deployment\nRollback restores the previous release.\n',
                    'models.md': '# 模型配置\n设置模型服务地址和模型名称。\n'}
                for path, content in docs.items():
                    (root / path).write_text(content, encoding='utf-8')
                assert c.put(f'/admin/api/libraries/{lid}/plugin', json={'plugin_id': 'bm25-markdown', 'plugin_version': '1.0.1'}).status_code == 200
                Capabilities(suite.registry).policy(key['key']['id'], Policy(permissions=[{'library_id': lid, 'grep': True, 'search': True, 'write_policy': 'direct'}]))
                missing = c.get('/api/v1/search', headers={'Authorization': 'Bearer ' + key['token']}, params={'query': 'rollback'})
                result['lifecycle']['before_build'] = {'http_status': missing.status_code, 'error': missing.json().get('error')}
                started = time.perf_counter()
                build = c.post(f'/admin/api/libraries/{lid}/index/build')
                assert build.status_code == 200, build.text
                row = {'seed_files': count, 'seed_bytes_each': size, 'build_ms': round((time.perf_counter() - started) * 1000, 3),
                       'indexed_files': build.json()['files'], 'chunks': build.json()['chunks'], 'queries': []}
                cases = [('访问密钥', 'security.md', 'lexical'), ('rollback', 'deployment.md', 'lexical'),
                         ('模型配置', 'models.md', 'lexical'), ('如何生成访问密钥', 'security.md', 'lexical'),
                         ('凭证', 'security.md', 'synonym'), ('回退版本', 'deployment.md', 'cross_language'),
                         ('fkbnomatch4c21a393e6', None, 'negative')]
                async with suite.client(key) as p:
                    row['tools'] = [t.name for t in (await p.session.list_tools()).tools]
                    for query, expected, kind in cases:
                        samples, top_hits, hits = [], [], []
                        for _ in range(3):
                            response = await p.call('search', query=query, limit=5)
                            assert '_error' not in response
                            samples.append(p.calls[-1]['ms'])
                            results = response['results']
                            hits.append(len(results))
                            top_hits.append(bool(results and results[0]['path'] == expected))
                            for hit in results:
                                source = (root / hit['path']).read_text(encoding='utf-8').splitlines(keepends=True)
                                quoted = ''.join(source[hit['start_line'] - 1:hit['end_line']])
                                assert hit['text'].replace('\r\n', '\n') in quoted and hit['version'] and hit['sha256']
                        row['queries'].append({'query': query, 'kind': kind, 'expected_top_file': expected,
                                               'top1_hits': top_hits, 'result_counts': hits, **summary(samples)})
                    invalid = await p.call('search', query='***')
                    result['lifecycle']['invalid_query_rejected'] = 'invalid_search' in invalid.get('_error', '')
                    edit = await p.call('write_file', path='fresh.md', content='# Fresh\nnew fact', operation='create', request_id='fresh')
                    assert '_error' not in edit
                    stale = await p.call('search', query='rollback')
                    result['lifecycle']['after_write_stale'] = 'index_stale' in stale.get('_error', '')
                    assert result['lifecycle']['after_write_stale']
                    assert c.post(f'/admin/api/libraries/{lid}/index/build').status_code == 200
                    restored = await p.call('search', query='rollback')
                    result['lifecycle']['after_rebuild_works'] = '_error' not in restored and bool(restored['results'])
                result['datasets'].append(row)
                (output / 'search-synthetic.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps({'search_dataset': count, 'build_ms': row['build_ms'], 'files': row['indexed_files'], 'chunks': row['chunks']}, ensure_ascii=False), flush=True)
    finally:
        suite.close()
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    return result


async def private_probe(output, base, token_path, name):
    """Only aggregate values leave this function; no exception may echo a response."""
    admin = token_path.read_text(encoding='utf-8').strip()
    report = {'name': name, 'private': True, 'timestamp': datetime.now(timezone.utc).isoformat(), 'probes': []}
    key_id = None
    async with httpx.AsyncClient(base_url=base, headers={'Authorization': 'Bearer ' + admin}, trust_env=False, timeout=120) as c:
        try:
            overview = await c.get('/admin/api/overview')
            if overview.status_code != 200:
                raise RuntimeError('Could not retrieve configuration metadata')
            found = [v for v in overview.json()['libraries'] if v['name'] == name]
            if len(found) != 1:
                raise RuntimeError('Library name must identify exactly one library')
            lid = found[0]['id']
            state = await c.get(f'/admin/api/libraries/{lid}/index')
            meta = state.json()
            report['index'] = {k: meta[k] for k in ('status', 'engine', 'files', 'chunks', 'error') if k in meta}
            # Grant only this library, with no direct writes or inbox permission.
            created = await c.post('/admin/api/keys', json={'agent_name': '临时检索基准测试（自动撤销）',
                'library_ids': [lid], 'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
            if created.status_code != 201:
                raise RuntimeError('Could not create scoped test key')
            key = created.json()
            key_id = key['key']['id']
            policy = await c.put(f'/admin/api/keys/{key_id}/policy', json={'permissions': [{'library_id': lid, 'search': True}]})
            if policy.status_code != 200:
                raise RuntimeError('Could not configure test key')
            agent = {'Authorization': 'Bearer ' + key['token']}
            query = 'fkbnomatch' + uuid.uuid4().hex
            # On unconfigured libraries this verifies errors, not search performance.
            for _ in range(5):
                started = time.perf_counter()
                response = await c.get('/api/v1/search', headers=agent, params={'library_id': lid, 'query': query, 'limit': 5})
                elapsed = (time.perf_counter() - started) * 1000
                body = response.json()
                report['probes'].append({'transport': 'rest', 'http_status': response.status_code,
                                        'ms': round(elapsed, 3), 'error_code': body.get('error'),
                                        'result_count': len(body.get('results', []))})
                del body, response
            async with httpx.AsyncClient(headers=agent, trust_env=False, timeout=120) as http:
                async with streamable_http_client(base + '/mcp', http_client=http) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        report['search_exposed'] = any(t.name == 'search' for t in (await session.list_tools()).tools)
                        for _ in range(3):
                            started = time.perf_counter()
                            response = await session.call_tool('search', {'library_id': lid, 'query': query, 'limit': 5})
                            elapsed = (time.perf_counter() - started) * 1000
                            # Never retain arbitrary error messages (could contain paths).
                            content = '\n'.join(x.text for x in response.content if x.type == 'text')
                            if response.isError:
                                code = next((v for v in ('capability_forbidden', 'index_not_configured', 'index_missing', 'index_stale', 'library_unavailable') if v in content), 'other_error')
                                hits = 0
                            else:
                                code = None
                                hits = len(json.loads(content).get('results', []))
                            report['probes'].append({'transport': 'mcp', 'ms': round(elapsed, 3), 'is_error': bool(response.isError),
                                                    'error_code': code, 'result_count': hits})
                            del content, response
        finally:
            if key_id:
                revoked = await c.post(f'/admin/api/keys/{key_id}/revoke')
                report['temporary_key_revoked'] = revoked.status_code == 200
                if not report['temporary_key_revoked']:
                    raise RuntimeError('Temporary key revocation failed; expires in one hour')
    output.mkdir(parents=True, exist_ok=True)
    (output / 'search-private-aggregate.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)


def main():
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('data/search-benchmark'))
    parser.add_argument('--private', action='store_true')
    parser.add_argument('--base', default='http://127.0.0.1:8791')
    parser.add_argument('--admin-token-file', type=Path, default=Path('data/local-admin-token.txt'))
    parser.add_argument('--library-name', default='个人知识测试知识库')
    args = parser.parse_args()
    if args.private:
        try:
            asyncio.run(private_probe(args.output, args.base, args.admin_token_file, args.library_name))
        except Exception as exc:
            # Deliberately omit traceback/response text for the private target.
            raise SystemExit('Private probe failed: ' + type(exc).__name__) from None
    else:
        asyncio.run(synthetic(args.output))


if __name__ == '__main__':
    main()
