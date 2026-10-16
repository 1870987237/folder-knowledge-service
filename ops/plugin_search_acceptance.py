"""Retest retrieval on a frozen synthetic output without calling the model again."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'ops'))
from plugin_acceptance import install,save
from folder_kb.management import Registry
from folder_kb.retrieval import Retrieval
from folder_kb.templates import TemplateBinding
from folder_kb.updates import Updates
from folder_kb.workspaces import workspace_digest


def run(source,output):
    prior=json.loads((source/'report.json').read_text(encoding='utf-8'))
    if prior.get('state')!='completed' or prior.get('dataset')!='synthetic-only':
        raise ValueError('Only completed synthetic acceptance output is supported')
    previous=Registry(str(source/'pool'),str(source/'platform'))
    scope=Updates(previous).catalog.snapshot_source('demo')
    visible=scope['root']/scope['spec'].knowledge_subpath
    output.mkdir(parents=True,exist_ok=False)
    pool,state=output/'pool',output/'platform'
    for lid in ('demo','bm25'):shutil.copytree(visible,pool/lid)
    registry=Registry(str(pool),str(state));updates=Updates(registry)
    packages=[]
    for lid,name in (('demo','general-knowledge'),('bm25','bm25-markdown')):
        registry.save_library(lid,'Synthetic search retest',lid,True,create=True)
        installed=install(updates,name,output/'packages');packages.append(installed['package_sha256'])
        updates.catalog.bind(lid,TemplateBinding(template_id=name,template_version=installed['definition']['version']))
    os.environ['KB_SEARCH_TRUSTED_PYTHON']='1';os.environ['KB_SEARCH_PLUGIN_ALLOWLIST']=','.join(packages)
    retrieval=Retrieval(registry);rows=[]
    for lid in ('demo','bm25'):
        started=time.perf_counter();built=retrieval.build(lid)
        row=dict(library=lid,build_seconds=round(time.perf_counter()-started,3),files=built['files'],engine=built['engine'],queries=[])
        for query in ('Atlas 超时','Boreal 超时','HTTPS','quartzunfindable998877'):
            started=time.perf_counter();result=retrieval.search(lid,query,limit=5)
            hits=result['results']
            row['queries'].append(dict(query=query,seconds=round(time.perf_counter()-started,3),hits=len(hits),
                paths=[h['path'] for h in hits],snippets=[h['text'] for h in hits],
                no_raw_metadata=all('sources:' not in h['text'] and 'record_id' not in h['text'] for h in hits)))
        rows.append(row)
    report=dict(dataset='synthetic-only',state='completed',source_run=str(source),source_sha256=workspace_digest(visible),
        model_calls=0,search=rows,packages=packages)
    save(output/'report.json',report)
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();run(a.source.resolve(),a.output.resolve())
