"""Freeze and execute a main benchmark matrix with immutable stage checkpoints."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import re
import shutil
from types import SimpleNamespace

from benchmarks.agent.tasks import fingerprint, load_dataset
from benchmarks.browsecomp.run import run as run_experiment
from benchmarks.browsecomp.scorecard import report, render
from benchmarks.provenance import freeze_source
from benchmarks.retrieval.queries import run as generate_queries
from benchmarks.retrieval.run import run as run_retrieval

ROOT=Path(__file__).resolve().parents[2]


def read_config(path, dataset):
    config=json.loads(Path(path).read_text(encoding='utf-8'))
    allowed={'version','model','judge_model','repeats','timeout','tasks','plugins','retrieval'}
    if set(config)-allowed or config.get('version')!=1:raise ValueError('Unknown campaign config version/fields')
    if not isinstance(config.get('model'),str) or not config['model']:raise ValueError('A solver model is required')
    if any(type(config.get(k)) is not int or config[k]<1 for k in ('repeats','timeout')):raise ValueError('Positive repeats/timeout required')
    documents,tasks,metadata=load_dataset(dataset)
    if any(not t.reference for t in tasks):raise ValueError('Campaign requires BrowseComp-Plus data')
    selected=config.get('tasks') or [t.id for t in tasks]
    if not isinstance(selected,list) or len(set(selected))!=len(selected) or set(selected)-{t.id for t in tasks}:
        raise ValueError('Unknown or duplicate campaign task IDs')
    plugins=config.get('plugins',[]);names=set()
    if not plugins:raise ValueError('At least one plugin is required')
    for item in plugins:
        if set(item)!={'name','definition','source','trust_plugin','variants'}:raise ValueError('Invalid plugin configuration')
        name=item['name']
        if not re.fullmatch(r'[a-z][a-z0-9_-]*',name) or name=='control' or name in names:raise ValueError('Unique safe plugin names required')
        names.add(name)
        variants=item['variants']
        if not isinstance(variants,list) or not variants or len(set(variants))!=len(variants) or set(variants)-{'grep','search','hybrid'}:raise ValueError('Invalid tool variants')
        if type(item['trust_plugin']) is not bool:raise ValueError('Explicit plugin trust boolean required')
        definition=(ROOT/item['definition']).resolve();source=(ROOT/item['source']).resolve()
        if not definition.is_file() or not source.is_dir():raise ValueError('Plugin source/definition not found')
        item['definition']=str(definition);item['source']=str(source)
        item['source_sha256']=fingerprint({p.relative_to(source).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob('*') if p.is_file() and '__pycache__' not in p.parts})
        item['definition_sha256']=hashlib.sha256(definition.read_bytes()).hexdigest()
    if type(config.get('retrieval',True)) is not bool:raise ValueError('retrieval must be boolean')
    config.update(tasks=selected,judge_model=config.get('judge_model') or config['model'],retrieval=config.get('retrieval',True))
    jobs=[{'kind':'agent','name':'control','plugin':plugins[0]['name'],'variants':['closed_book']}]
    jobs += [{'kind':'agent','name':p['name'],'plugin':p['name'],'variants':p['variants']} for p in plugins]
    if config['retrieval']:
        jobs=[{'kind':'queries','name':'queries','variants':[]}] + [
            {'kind':'retrieval','name':'retrieval-'+p['name'],'plugin':p['name'],'variants':[]} for p in plugins] + jobs
    if len({j['name'] for j in jobs})!=len(jobs):raise ValueError('Plugin name collides with a reserved stage name')
    return {'configuration':config,'dataset':metadata,'jobs':jobs,
        'planned_agent_episodes':len(selected)*config['repeats']*sum(len(j['variants']) for j in jobs),
        'planned_judge_attempts_at_most':len(selected)*config['repeats']*sum(len(j['variants']) for j in jobs),
        'planned_query_generations':len(selected) if config['retrieval'] else 0,
        'question_selection':'frozen task IDs; no result-based retries or filtering',
        'execution_order':'control then configured plugins; tool variants rotate within task/repeat',
        'data_bytes':sum(len(v.encode()) for v in documents.values())}


def source_hash():
    # Detect evaluation or platform edits before resuming; ignore docs/results.
    files={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
           for folder in ('benchmarks','folder_kb','tools') for p in (ROOT/folder).rglob('*')
           if p.is_file() and p.suffix in ('.py','.ts') and not any(x in p.parts for x in ('node_modules','__pycache__','web'))}
    return fingerprint(files)


def save(root,status):
    (root/'status.json').write_text(json.dumps(status,indent=2),encoding='utf-8')


def outputs_hash(destination,kind):
    paths={'agent':('metrics.json','execution/results.json','judge/judgments.json'),
           'queries':('manifest.json','queries.csv','qrels.json'),
           'retrieval':('results.json',)}[kind]
    return fingerprint({p:hashlib.sha256((destination/p).read_bytes()).hexdigest() for p in paths})


async def run(args):
    root=args.output.resolve()
    plan=read_config(args.config,args.dataset)
    plan['execution_source_sha256']=source_hash()
    plan['id']=fingerprint(plan)
    if args.resume:
        original=json.loads((root/'plan.json').read_text(encoding='utf-8'))
        if original!=plan:raise ValueError('Campaign inputs/source changed; start a new output instead of resuming')
        status=json.loads((root/'status.json').read_text(encoding='utf-8'))
    else:
        if root.exists() and any(root.iterdir()):raise ValueError('Campaign output must be new/empty; use --resume for an unchanged plan')
        root.mkdir(parents=True,exist_ok=True)
        (root/'plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8')
        freeze_source(root)
        status={'state':'planned','jobs':{},'plan_id':plan['id']};save(root,status)
    if not args.execute:
        print(json.dumps({'state':status['state'],'agent_episodes':plan['planned_agent_episodes'],'output':str(root)}),flush=True)
        return
    config=plan['configuration'];plugins={p['name']:p for p in config['plugins']}
    for job in plan['jobs']:
        if source_hash()!=plan['execution_source_sha256']:
            raise ValueError('Execution source changed during campaign; stop instead of mixing versions')
        name=job['name'];destination=root/name
        if status['jobs'].get(name,{}).get('state')=='completed':
            if outputs_hash(destination,job['kind'])!=status['jobs'][name]['outputs_sha256']:
                raise ValueError('Completed stage artifacts changed; cannot resume')
            continue
        if destination.exists():
            raise ValueError('Partial stage retained. Do not overwrite/retry it silently; analyze partial results or create a new campaign.')
        plugin=plugins.get(job.get('plugin'))
        status['state']='running';status['jobs'][name]={'state':'running'};save(root,status)
        try:
            common=dict(dataset=args.dataset,output=destination,omp=args.omp,models_config=args.models_config,
                        model=config['model'],tasks=config['tasks'],timeout=config['timeout'])
            if job['kind']=='queries':
                await generate_queries(SimpleNamespace(**dict(common,timeout=60),labels='evidence'))
            else:
                common.update(plugin_definition=Path(plugin['definition']),plugin_source=Path(plugin['source']),trust_plugin=plugin['trust_plugin'])
                if job['kind']=='retrieval':
                    await run_retrieval(SimpleNamespace(**common,queries=root/'queries/queries.csv',qrels=root/'queries/qrels.json',ks=[1,3,5,10]))
                else:
                    await run_experiment(SimpleNamespace(**common,judge_model=config['judge_model'],no_judge=False,
                        variants=job['variants'],repeats=config['repeats']))
            if source_hash()!=plan['execution_source_sha256']:
                raise ValueError('Execution source changed during stage; start a frozen run')
            artifact_hash=outputs_hash(destination,job['kind'])
            status['jobs'][name]={'state':'completed','outputs_sha256':artifact_hash};save(root,status)
        except Exception as exc:
            status['state']='interrupted';status['jobs'][name]={'state':'interrupted','error_type':type(exc).__name__};save(root,status)
            raise
    summaries={j['name']:json.loads((root/j['name']/'metrics.json').read_text(encoding='utf-8')) for j in plan['jobs'] if j['kind']=='agent'}
    data=report(summaries)
    data['retrieval']={}
    for job in plan['jobs']:
        if job['kind']!='retrieval':continue
        import statistics
        raw=json.loads((root/job['name']/'results.json').read_text(encoding='utf-8'))
        data['retrieval'][job['plugin']]={'queries':len(raw['queries']),'successful_queries':sum(r['success'] for r in raw['queries']),
            'queries_sha256':raw['queries_sha256'],'qrels_sha256':raw['qrels_sha256'],'chunk_budget':raw['chunk_budget'],
            'index_build_seconds':raw['index_build_seconds'],'ks':{str(k):{metric:statistics.mean(
                q['metrics'][str(k)][metric] for q in raw['queries'] if q['metrics'][str(k)][metric] is not None)
                for metric in ('recall','ndcg','mrr','precision_at_k')} for k in raw['ks']}}
    (root/'metrics.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    (root/'report.md').write_text(render(data),encoding='utf-8')
    status['state']='completed';save(root,status)
    print(json.dumps({'state':'completed','output':str(root)}),flush=True)


def main():
    import logging
    logging.disable(logging.CRITICAL)
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,default=Path('benchmarks/browsecomp/configs/main.json'))
    p.add_argument('--dataset',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--execute',action='store_true',help='Without this flag only freeze and show the plan; no model calls')
    p.add_argument('--resume',action='store_true',help='Continue an unchanged plan, preserving completed stages')
    p.add_argument('--omp',default=shutil.which('omp') or 'omp')
    p.add_argument('--models-config',type=Path,default=Path.home()/'.omp/agent/models.yml')
    asyncio.run(run(p.parse_args()))


if __name__=='__main__':main()
