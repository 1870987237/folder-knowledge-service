"""Original full queries -> document ranking -> official evidence/gold metrics.

This is the retrieval-only layer. It calls the reviewed plugin's optional rank
entrypoint, not the Agent MCP Search tool (which has a different output budget).
"""
import argparse
import json
from pathlib import Path
import statistics
import time

from benchmarks.agent.tasks import DiskCorpus, load_dataset, fingerprint
from benchmarks.browsecomp.full_prepare import atomic_json, sha256
from benchmarks.retrieval.run import ranking_metrics
from benchmarks.browsecomp.campaign import source_hash
from benchmarks.provenance import freeze_source
from folder_kb.search_large import artifacts


def render(data):
    rows=data['queries']
    lines=['# BrowseComp-Plus 全量文档检索评测','',
        f"原始问题：{len(rows)}/{data['planned_queries']}；完整语料：{data['dataset']['corpus_files']} 篇；引擎：{data['engine']}。",
        '原题不经模型改写，插件返回文档 Top-1000。标签分别统计；本报告不测 Agent 答题，也不采用 MCP 的片段返回上限。','',
        '| 标签 | Recall@5 | Recall@100 | Recall@1000 | nDCG@10 |','|---|---:|---:|---:|---:|']
    for label in ('evidence','gold'):
        values=[statistics.mean(r['metrics'][label][str(k)][metric] for r in rows) if rows else 0
                for k,metric in ((5,'recall'),(100,'recall'),(1000,'recall'),(10,'ndcg'))]
        lines.append('| '+label+' | '+' | '.join(f'{v:.4f}' for v in values)+' |')
    lines+=['',f"成功查询：{sum(r['success'] for r in rows)}/{len(rows)}；失败按空排名计入均值。",
        'SQLite FTS5 Porter/BM25 是本平台插件基线，不等同于上游 Pyserini 的 BM25 实现。查询分词、停用词与排序由插件定义。',
        '每题排名已导出 TREC 格式；题目和答案保持在被忽略的本地数据目录。','']
    return '\n'.join(lines)


def run(args):
    corpus,tasks,metadata=load_dataset(args.dataset)
    if not isinstance(corpus,DiskCorpus):
        raise ValueError('Full-corpus input required')
    service=json.loads((args.run/'service.json').read_text(encoding='utf-8'))
    agent_plan=json.loads((args.run/'plan.json').read_text(encoding='utf-8'))
    if agent_plan['dataset']!=metadata:
        raise ValueError('Index was built for a different dataset')
    index=json.loads((args.run/'index.json').read_text(encoding='utf-8'))
    if index['files']!=len(corpus):
        raise ValueError('Index document count differs from corpus')
    # The library ID and installed package are fixed by full_run, never user supplied SQL.
    import hashlib
    state=Path(service['state_dir'])
    artifact=state/'indexes'/(hashlib.sha256(b'benchmark-corpus').hexdigest()+'-generations')/index['generation']
    if artifacts(artifact,content=True)!=index['artifact_sha256']:
        raise ValueError('Index content differs from build record')
    package=state/'templates'/'sqlite-corpus'/'1.0.0'/'package'/'search.py'
    # Resolve the installed location through the registry-owned template catalogue.
    if not package.is_file():
        from folder_kb.management import Registry
        from folder_kb.templates import TemplateCatalog
        package=TemplateCatalog(Registry(str(args.dataset.resolve()),str(state))).version_path('sqlite-corpus','1.0.0')/'package/search.py'
    namespace={'__name__':'reviewed_corpus_plugin','__file__':str(package)}
    exec(compile(package.read_bytes(),str(package),'exec'),namespace)
    root=args.output.resolve();root.mkdir(parents=True,exist_ok=True)
    plan=dict(dataset=metadata,index_sha256=index['artifact_sha256'],plugin_sha256=sha256(package),
        query_policy='all original questions; no rewriting, answer-derived expansion or truncation',
        task_ids=[t.id for t in tasks],source_sha256=source_hash(),ks=[5,10,100,1000])
    if (root/'plan.json').exists():
        if not args.resume or json.loads((root/'plan.json').read_text(encoding='utf-8'))!=plan:
            raise ValueError('Resume requires unchanged full retrieval inputs')
    else:
        if any(root.iterdir()):raise ValueError('Output must be new/empty')
        atomic_json(root/'plan.json',plan);freeze_source(root)
    data=json.loads((root/'results.json').read_text(encoding='utf-8')) if (root/'results.json').exists() else dict(
        dataset=metadata,engine=index['engine'],planned_queries=len(tasks),query_policy=plan['query_policy'],queries=[])
    (root/'episodes').mkdir(exist_ok=True)
    completed={r['id'] for r in data['queries']}
    for task in tasks:
        if source_hash()!=plan['source_sha256']:
            raise ValueError('Source changed during retrieval experiment')
        file=root/'episodes'/(task.id+'.json')
        if task.id in completed:
            if json.loads(file.read_text(encoding='utf-8'))!=next(r for r in data['queries'] if r['id']==task.id):
                raise ValueError('Saved ranking changed')
            continue
        start=time.perf_counter()
        error=None
        try:
            result=namespace['search'](dict(operation='rank',query=task.prompt,index_dir=str(artifact),path='.',limit=1000))
            paths=[r['path'] for r in result['results']]
            if len(paths)!=len(set(paths)) or any(p not in corpus for p in paths):
                raise ValueError('Invalid ranking')
        except Exception as exc:
            result={'results':[]};paths=[];error=type(exc).__name__
        row=dict(id=task.id,query_id=task.reference['query_id'],success=error is None,error=error,
            seconds=time.perf_counter()-start,ranking=result['results'],metrics={})
        for label in ('evidence','gold'):
            relevant=[f'documents/{d}.md' for d in task.reference[label+'_docids']]
            row['metrics'][label]={str(k):ranking_metrics(paths,relevant,k) for k in (5,10,100,1000)}
        atomic_json(file,row);data['queries'].append(row);atomic_json(root/'results.json',data)
        if len(data['queries'])%10==0:
            print(json.dumps({'ranked':len(data['queries']),'planned':len(tasks)}),flush=True)
            (root/'report.md').write_text(render(data),encoding='utf-8')
    with (root/'run.trec').open('w',encoding='utf-8',newline='\n') as stream:
        for row in data['queries']:
            for number,hit in enumerate(row['ranking'],1):
                did=Path(hit['path']).stem
                stream.write(f"{row['query_id']} Q0 {did} {number} {hit['score']:.15g} sqlite-corpus\n")
    (root/'report.md').write_text(render(data),encoding='utf-8')
    atomic_json(root/'status.json',dict(state='completed',queries=len(data['queries']),success=sum(r['success'] for r in data['queries'])))


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--dataset',type=Path,default=Path('data/browsecomp-plus-full'))
    p.add_argument('--run',type=Path,required=True,help='Full run with an already built platform index')
    p.add_argument('--output',type=Path,required=True);p.add_argument('--resume',action='store_true')
    run(p.parse_args())


if __name__=='__main__':main()
