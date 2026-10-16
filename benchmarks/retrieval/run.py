"""Evaluate unchanged Search responses against frozen document-level qrels."""
import argparse
import asyncio
import csv
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import statistics
import time

from benchmarks.agent.runner import install_index_plugin, seed_knowledge
from benchmarks.agent.tasks import corpus, fingerprint
from benchmarks.provenance import freeze_source
from folder_kb.capabilities import Capabilities, Policy
from tools.benchmark_file_tools import Suite


def ranking_metrics(paths, relevant, k):
    # Multiple chunks from one document earn credit only once, in first-hit order.
    ranked = list(dict.fromkeys(paths))[:k]
    relevant = set(relevant)
    if not relevant:
        return {'recall': None, 'ndcg': None, 'mrr': None, 'precision_at_k': None,
                'retrieved_documents': len(ranked)}
    gains = [int(p in relevant) for p in ranked]
    dcg = sum(gain / math.log2(rank + 2) for rank, gain in enumerate(gains))
    ideal = sum(1 / math.log2(rank + 2) for rank in range(min(k, len(relevant))))
    return {'recall': sum(gains) / len(relevant), 'ndcg': dcg / ideal,
            'mrr': next((1 / (i + 1) for i, gain in enumerate(gains) if gain), 0),
            'precision_at_k': sum(gains) / k, 'retrieved_documents': len(ranked)}


def load_queries(path, qrels_path, documents):
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ['id', 'query']:
            raise ValueError('Queries CSV must contain id,query columns')
        queries = list(reader)
    qrels = json.loads(Path(qrels_path).read_text(encoding='utf-8-sig'))
    seen = set()
    for row in queries:
        if set(row) != {'id', 'query'} or not row['id'] or row['id'] in seen:
            raise ValueError('Duplicate/invalid query identifier')
        if not isinstance(row['query'], str) or not 1 <= len(row['query'].strip()) <= 200:
            raise ValueError(f"{row['id']}: Search accepts 1..200 characters; provide an explicit reviewed query, never truncate")
        seen.add(row['id'])
        labels = qrels.get(row['id'])
        if not isinstance(labels, list) or any(not isinstance(p, str) or p not in documents for p in labels):
            raise ValueError(f"{row['id']}: qrels must reference corpus documents")
        if len(labels) != len(set(labels)):
            raise ValueError('Duplicate qrel document')
    if not queries or seen != set(qrels):
        raise ValueError('Queries and qrels must have identical nonempty identifier sets')
    return queries, qrels


def render(data):
    rows = data['queries']
    lines = ['# 固定查询检索评测', '',
        f"查询数：{len(rows)}；成功：{sum(r['success'] for r in rows)}；语料文件数：{data['corpus_files']}。",
        '同一查询只调用一次真实 MCP Search，取最多 10 个片段，按首次出现顺序去重成文档，再取前 K。',
        '这是当前工具返回预算下的文档级指标，不等同于索引的无限候选排序，更不是官方 Recall@100/1000。', '',
        '| K | Recall@K | nDCG@K（二值相关） | MRR@K | Precision@K |', '|---|---:|---:|---:|---:|']
    for k in data['ks']:
        metrics = [r['metrics'][str(k)] for r in rows if r['metrics'][str(k)]['recall'] is not None]
        averages = [statistics.mean(m[key] for m in metrics) if metrics else 0
                    for key in ('recall', 'ndcg', 'mrr', 'precision_at_k')]
        lines.append(f"| {k} | " + ' | '.join(f'{v:.3f}' for v in averages) + ' |')
    lines += ['', '请求失败按空结果计入有正例查询的均值；无正例查询不计召回和排序均值，单独报告空结果率。',
              '查询文本、qrels、语料、插件和评测源码均记录指纹。建索引时间不计入查询时间。', '',
              '| 查询 ID | 成功 | 返回片段 / 去重文档 | 毫秒 |', '|---|---|---:|---:|']
    for row in rows:
        lines.append(f"| {row['id']} | {row['success']} | {row['chunks']} / {len(row['documents'])} | {row['milliseconds']:.1f} |")
    empty = [r for r in rows if not r['relevant_documents']]
    lines += ['', f"无正例查询：{len(empty)}；成功且空结果：{sum(r['success'] and not r['documents'] for r in empty)}。", '']
    return '\n'.join(lines)


async def run(args):
    documents = corpus(args.dataset)
    queries, qrels = load_queries(args.queries, args.qrels, documents)
    if not args.ks or len(set(args.ks)) != len(args.ks) or any(k < 1 or k > 10 for k in args.ks):
        raise ValueError('K must be unique and in 1..10 (Search chunk budget)')
    if args.output.exists() and any(args.output.iterdir()):
        raise ValueError('Output must be empty; do not overwrite earlier runs')
    args.output.mkdir(parents=True, exist_ok=True)
    plan = {'queries_sha256': fingerprint(queries), 'qrels_sha256': fingerprint(qrels),
            'corpus_sha256': fingerprint(documents), 'corpus_files': len(documents), 'ks': args.ks,
            'chunk_budget': 10, 'relevance': 'binary-document',
            'ranking': 'first-occurrence deduplication within top 10 returned chunks',
            'source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    query_manifest=args.queries.parent/'manifest.json'
    if query_manifest.is_file():
        plan['query_provenance']=json.loads(query_manifest.read_text(encoding='utf-8'))
    plan['source_snapshot']=freeze_source(args.output)
    (args.output / 'plan.json').write_text(json.dumps(plan, indent=2), encoding='utf-8')
    suite = Suite(args.output, short_fixture=True)
    saved_env = {k: os.environ.get(k) for k in ('KB_SEARCH_TRUSTED_PYTHON', 'KB_SEARCH_PLUGIN_ALLOWLIST')}
    admin = None
    try:
        admin = install_index_plugin(suite, args)
        lid, key, root = suite.setup()
        seed_knowledge(root, documents)
        response = admin.put(f'/admin/api/libraries/{lid}/plugin', json={'plugin_id': 'benchmark-rag', 'plugin_version': args.plugin_version})
        response.raise_for_status()
        started = time.perf_counter()
        built = admin.post(f'/admin/api/libraries/{lid}/index/build')
        (args.output / 'setup.json').write_text(json.dumps({'http_status': built.status_code, 'index': built.json()}, ensure_ascii=False, indent=2), encoding='utf-8')
        built.raise_for_status()
        data = dict(plan, environment=suite.result['environment'], plugin=suite.result['plugin'],
                    index_build_seconds=time.perf_counter()-started, queries=[])
        Capabilities(suite.registry).policy(key['key']['id'], Policy(permissions=[{'library_id': lid, 'grep': False, 'search': True}]))
        async with suite.client(key) as probe:
            for query in queries:
                start = time.perf_counter()
                try:
                    result = await probe.call('search', query=query['query'], limit=10, library_id=lid)
                    success = '_error' not in result
                except Exception as exc:
                    result, success = {'_error': type(exc).__name__}, False
                hits = result.get('results', []) if success else []
                paths = list(dict.fromkeys(h['path'] for h in hits))
                data['queries'].append({'id': query['id'], 'query': query['query'], 'success': success,
                    'error': result.get('_error'), 'documents': paths, 'chunks': len(hits),
                    'relevant_documents': qrels[query['id']], 'has_more_chunks': result.get('has_more'),
                    'milliseconds': (time.perf_counter()-start)*1000,
                    'metrics': {str(k): ranking_metrics(paths, qrels[query['id']], k) for k in args.ks}})
                (args.output / 'results.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        suite.registry.revoke_key(key['key']['id'])
        (args.output / 'report.md').write_text(render(data), encoding='utf-8')
        print(json.dumps({'queries': len(queries), 'success': sum(r['success'] for r in data['queries']), 'output': str(args.output)}))
        return data
    finally:
        if admin:
            admin.close()
        suite.close()
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def main():
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, default=Path('benchmarks/datasets/basic'))
    parser.add_argument('--queries', type=Path, default=Path('benchmarks/datasets/basic/retrieval-queries.csv'))
    parser.add_argument('--qrels', type=Path, default=Path('benchmarks/datasets/basic/retrieval-qrels.json'))
    parser.add_argument('--ks', type=int, nargs='+', default=[1, 3, 5, 10])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--plugin-definition', type=Path, default=Path('examples/plugins/bm25-markdown/definition.json'))
    parser.add_argument('--plugin-source', type=Path, default=Path('examples/plugins/bm25-markdown/package'))
    parser.add_argument('--trust-plugin', action='store_true')
    args = parser.parse_args()
    args.output = args.output.resolve()
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
