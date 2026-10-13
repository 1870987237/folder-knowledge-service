"""Freeze one short query per question before seeing any corpus or retrieval output."""
import argparse
import asyncio
import csv
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

from benchmarks.agent.preflight import check_model
from benchmarks.agent.runner import Recorder, solve
from benchmarks.agent.tasks import fingerprint, load_dataset
from benchmarks.provenance import freeze_source

SYSTEM='''Convert the user question into one concise search query of 1 to 200 characters, preserving important named entities and constraints. Use only words and facts in the question. Do not solve it, invent an answer, add guessed answer entities or use tools. Do not output multiple alternative queries.
Return only JSON: {"answer":"query prepared", "facts":{"query":"your search query"}, "sources":[], "completed":true}.'''


def query_from(execution):
    # Query preparation has no task-completion verdict. Validate its own output
    # contract rather than requiring the answer grader's completed field.
    try:value=json.loads(execution['final'])
    except ValueError:value=None
    if not isinstance(value,dict):value=None
    query=value.get('facts',{}).get('query') if value and isinstance(value.get('facts'),dict) else None
    if (not execution['valid_run'] or execution['returncode']!=0 or execution['timed_out']
            or execution['agent_tool_attempts'] or not isinstance(query,str) or not 1<=len(query.strip())<=200):
        raise ValueError('Query generation failed validation; never truncate or substitute answer keywords')
    return query.strip()


async def run(args):
    _,tasks,metadata=load_dataset(args.dataset)
    if any(not t.reference for t in tasks):raise ValueError('Expected BrowseComp-Plus tasks')
    selected=getattr(args,'tasks',None)
    if selected and (len(selected)!=len(set(selected)) or set(selected)-{t.id for t in tasks}):raise ValueError('Unknown/duplicate task IDs')
    tasks=[t for t in tasks if not selected or t.id in selected]
    root=args.output.resolve()
    if root.exists() and any(root.iterdir()):raise ValueError('Query output must be new/empty')
    root.mkdir(parents=True,exist_ok=True)
    manifest={'query_policy':'one question-only model-generated query; no tools/corpus/answers; frozen before plugin comparison',
        'source_model':args.model,'labels':args.labels,'dataset':metadata,'system_prompt_sha256':fingerprint(SYSTEM),
        'source_snapshot':freeze_source(root),'status':'running','planned_queries':len(tasks),'queries':[]}
    def save():(root/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    save();health=await check_model(args)
    (root/'preflight.json').write_text(json.dumps(health,indent=2),encoding='utf-8')
    if not health['ok']:
        manifest['status']='preflight_failed';save();raise RuntimeError('Query model preflight failed')
    rows=[];qrels={}
    try:
        for task in tasks:
            directory=root/'episodes'/task.id;directory.mkdir(parents=True)
            execution,_=await solve(SimpleNamespace(url='http://127.0.0.1:1/unused'),Recorder(),None,directory,
                task.prompt,args,closed_book=True,system_prompt=SYSTEM)
            query=query_from(execution)
            rows.append({'id':task.id,'query':query})
            qrels[task.id]=[f'documents/{did}.md' for did in task.reference[args.labels+'_docids']]
            manifest['queries'].append({'id':task.id,'query_sha256':fingerprint(query),'execution_sha256':fingerprint(execution)})
            save()
    except Exception as exc:
        manifest.update(status='failed',error_type=type(exc).__name__);save();raise
    with (root/'queries.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=['id','query']);writer.writeheader();writer.writerows(rows)
    (root/'qrels.json').write_text(json.dumps(qrels,indent=2),encoding='utf-8')
    manifest.update(status='completed',queries_sha256=fingerprint(rows),qrels_sha256=fingerprint(qrels));save()
    return manifest


def main():
    import logging
    logging.disable(logging.CRITICAL)
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--tasks',nargs='+');p.add_argument('--labels',choices=['evidence','gold'],default='evidence')
    p.add_argument('--model',default='hyper-op/GLM-5.3-Flash');p.add_argument('--timeout',type=int,default=60)
    p.add_argument('--omp',default=shutil.which('omp') or 'omp')
    p.add_argument('--models-config',type=Path,default=Path.home()/'.omp/agent/models.yml')
    asyncio.run(run(p.parse_args()))


if __name__=='__main__':main()
