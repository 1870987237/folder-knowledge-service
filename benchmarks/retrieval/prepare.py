"""Freeze first, pre-evidence Agent queries from a recorded BrowseComp run."""
import argparse
import csv
import hashlib
import json
from pathlib import Path

from benchmarks.agent.tasks import load_dataset
from benchmarks.retrieval.run import load_queries


def first_query(calls):
    for call in calls:
        if call.get('tool')=='search':
            args=call.get('arguments',{})
            query=args.get('query')
            if not isinstance(query,str) or not 1<=len(query.strip())<=200 or args.get('path','.')!='.':
                raise ValueError('First query is incompatible with fixed-query evaluation')
            return query
        if call.get('tool') in ('read_file','grep') and not call.get('is_error'):
            raise ValueError('Query was chosen after seeing evidence; do not silently substitute another query')
    raise ValueError('No Search query in the selected episode; do not pick a later successful repeat')


def prepare(dataset, run, output, repeat=1, labels='evidence'):
    documents,tasks,metadata=load_dataset(dataset)
    raw=(run/'results.json').read_bytes();data=json.loads(raw)
    if data['dataset']!=metadata:raise ValueError('Run and dataset fingerprints differ')
    if output.exists() and any(output.iterdir()):raise ValueError('Use an empty output directory')
    selected={r['task_id']:r for r in data['episodes'] if r['variant']=='search' and r['repeat']==repeat}
    if set(selected)!={t.id for t in tasks}:raise ValueError('Every task must have the preselected search repeat')
    queries=[];qrels={};trace_hashes={}
    for task in tasks:
        if not task.reference:raise ValueError('Expected BrowseComp references')
        trace=(run/'episodes'/selected[task.id]['episode']/'tool-trace.json').read_bytes()
        queries.append({'id':task.id,'query':first_query(json.loads(trace))})
        qrels[task.id]=[f'documents/{did}.md' for did in task.reference[labels+'_docids']]
        trace_hashes[task.id]=hashlib.sha256(trace).hexdigest()
    output.mkdir(parents=True,exist_ok=True)
    (output/'.gitignore').write_text('*\n',encoding='utf-8')
    with (output/'queries.csv').open('w',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=['id','query']);writer.writeheader();writer.writerows(queries)
    (output/'qrels.json').write_text(json.dumps(qrels,indent=2),encoding='utf-8')
    load_queries(output/'queries.csv',output/'qrels.json',documents)
    provenance={'query_policy':'first search in fixed repeat; before any successful read_file/grep; discovery/browse allowed',
        'source_variant':'search','source_repeat':repeat,'labels':labels,'source_model':data['model'],
        'source_run_sha256':hashlib.sha256(raw).hexdigest(),'trace_sha256':trace_hashes,'dataset':metadata,
        'answer_based_selection':False,'scope':'internal fixed Agent-generated queries, not official original-question retrieval'}
    (output/'manifest.json').write_text(json.dumps(provenance,indent=2),encoding='utf-8')
    print(json.dumps({'queries':len(queries),'labels':labels,'output':str(output)}))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset',type=Path,required=True);p.add_argument('--run',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--repeat',type=int,default=1)
    p.add_argument('--labels',choices=['evidence','gold'],default='evidence')
    args=p.parse_args();prepare(args.dataset,args.run,args.output,args.repeat,args.labels)


if __name__=='__main__':main()
