"""Export blinded local review packets and apply explicit human decisions separately."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import random

from benchmarks.agent.tasks import load_dataset
from benchmarks.browsecomp.analyze import execution_status


def new_directory(path):
    path=Path(path)
    if path.exists() and any(path.iterdir()):raise ValueError('Review output must be new/empty')
    path.mkdir(parents=True,exist_ok=True)
    return path


def export_review(run,dataset,judgments,output,sample=10,seed=42):
    run=Path(run);raw=(run/'results.json').read_bytes();data=json.loads(raw)
    _,tasks,metadata=load_dataset(dataset)
    if metadata!=data['dataset']:raise ValueError('Review dataset differs from run')
    if type(sample) is not int or sample<0:raise ValueError('Sample count must be nonnegative')
    judged=json.loads(Path(judgments).read_text(encoding='utf-8'))
    run_hash=hashlib.sha256(raw).hexdigest()
    if judged['run_sha256']!=run_hash:raise ValueError('Judgments belong to another run')
    by_id={t.id:t for t in tasks};by_episode={r['episode']:r for r in judged['episodes']}
    if len(by_episode)!=len(judged['episodes']) or set(by_episode)!={r['episode'] for r in data['episodes']}:
        raise ValueError('Review requires one judgment record per executed episode')
    # Include every model-negative/unjudged valid answer, plus a seeded sample of
    # positives per arm. Do not show variant, auto-score or tool counts to reviewer.
    eligible=[r for r in data['episodes'] if execution_status(r) in ('completed','incomplete_answer')]
    selected=[r for r in eligible if by_episode[r['episode']]['correct'] is not True]
    rng=random.Random(seed)
    for variant in sorted({r['variant'] for r in eligible}):
        positives=[r for r in eligible if r['variant']==variant and by_episode[r['episode']]['correct'] is True]
        selected+=rng.sample(positives,min(sample,len(positives)))
    rng.shuffle(selected);root=new_directory(output);mapping={};rows=[]
    for row in selected:
        identifier=hashlib.sha256((run_hash+row['episode']).encode()).hexdigest()[:20]
        mapping[identifier]=row['episode'];task=by_id[row['task_id']]
        rows.append({'review_id':identifier,'question':task.prompt,'reference_answer':task.reference['answer'],
            'candidate_answer':row['evaluation'].get('answer',''),'candidate_short_answer':row['evaluation'].get('prediction',''),
            'decision':'','reviewer':'','reason':''})
    columns=['review_id','question','reference_answer','candidate_answer','candidate_short_answer','decision','reviewer','reason']
    with (root/'review.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=columns);writer.writeheader();writer.writerows(rows)
    manifest={'run_sha256':run_hash,'judgments_sha256':hashlib.sha256(Path(judgments).read_bytes()).hexdigest(),
              'seed':seed,'positive_sample_per_variant':sample,'review_ids':mapping,
              'policy':'all valid wrong/unjudged plus sampled positives; no execution failure overrides',
              'frozen_packets':{r['review_id']:{k:r[k] for k in columns[:5]} for r in rows}}
    (root/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    (root/'machine-judgments.json').write_bytes(Path(judgments).read_bytes())
    (root/'.gitignore').write_text('*\n!.gitignore\n',encoding='utf-8')
    return {'review_items':len(rows),'execution_failures_excluded':len(data['episodes'])-len(eligible)}


def apply_review(queue,decisions,output):
    queue=Path(queue);manifest=json.loads((queue/'manifest.json').read_text(encoding='utf-8'))
    raw=(queue/'machine-judgments.json').read_bytes()
    if hashlib.sha256(raw).hexdigest()!=manifest['judgments_sha256']:raise ValueError('Original judgments changed')
    judged=json.loads(raw);mapping=manifest['review_ids'];seen=set();decided={}
    with Path(decisions).open(encoding='utf-8-sig',newline='') as f:
        for row in csv.DictReader(f):
            identifier=row.get('review_id')
            if identifier not in mapping or identifier in seen:raise ValueError('Unknown or duplicate review ID')
            seen.add(identifier)
            if any(row.get(k)!=v for k,v in manifest['frozen_packets'][identifier].items()):raise ValueError('Review question/reference/candidate was edited')
            verdict=row.get('decision','').strip().lower()
            if not verdict:continue
            if verdict not in ('correct','incorrect','unjudged'):raise ValueError('Decision must be correct, incorrect or unjudged')
            if not row.get('reviewer','').strip() or not row.get('reason','').strip():raise ValueError('Explicit reviewer and reason required')
            decided[mapping[identifier]]={'correct':{'correct':True,'incorrect':False,'unjudged':None}[verdict],
                'reviewer':row['reviewer'],'reason':row['reason']}
    if seen!=set(mapping):raise ValueError('Keep all review rows; leave decisions blank for pending items')
    if not decided:raise ValueError('No human decisions to apply')
    for row in judged['episodes']:
        if row['episode'] in decided:
            decision=decided[row['episode']]
            row['machine_judgment']={k:row.get(k) for k in ('correct','reason','error')}
            row.update(correct=decision['correct'],reason=decision['reason'],error=None,
                       human_review={'reviewer':decision['reviewer'],'reason':decision['reason']})
    judged['human_review']={'reviewed':len(decided),'requested':len(mapping),'pending':len(mapping)-len(decided),
                            'source_judgments_sha256':manifest['judgments_sha256'],
                            'decisions_sha256':hashlib.sha256(Path(decisions).read_bytes()).hexdigest()}
    root=new_directory(output)
    (root/'judgments.json').write_text(json.dumps(judged,ensure_ascii=False,indent=2),encoding='utf-8')
    return judged['human_review']


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='action',required=True)
    prepare=sub.add_parser('export')
    for name in ('run','dataset','judgments','output'):prepare.add_argument('--'+name,type=Path,required=True)
    prepare.add_argument('--sample',type=int,default=10);prepare.add_argument('--seed',type=int,default=42)
    apply=sub.add_parser('apply')
    for name in ('queue','decisions','output'):apply.add_argument('--'+name,type=Path,required=True)
    args=vars(p.parse_args());action=args.pop('action')
    print(json.dumps((export_review if action=='export' else apply_review)(**args)))


if __name__=='__main__':main()
