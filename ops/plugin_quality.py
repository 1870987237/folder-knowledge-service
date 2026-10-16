"""Real-model synthetic quality evaluation through the plugin file protocol.

Frozen fixtures and expected behavior live in ops/fixtures/knowledge-quality.json.
No production registry or knowledge is opened. Outputs are candidates, not published.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Lock, Thread
import time
from urllib import request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from folder_kb.inbox import Material
from folder_kb.template_protocol import JobRequest, Record, TemplateRef, prepare_job, validate_result
from ops.plugin_acceptance import NoRedirect, save

PROGRAM = ROOT/'examples/plugins/general-knowledge/package/program.py'
spec = importlib.util.spec_from_file_location('quality_program', PROGRAM)
program = importlib.util.module_from_spec(spec)
spec.loader.exec_module(program)


def record(ident, text, title='Atlas API 材料', scope='Atlas', kind='new_content'):
    return Record(id=ident, material=Material(library_id='quality', submission_id=ident,
        kind=kind, title=title, content={'text': text}, context={'scope': scope},
        source={'reference': 'synthetic:'+ident}))


def job_at(path, source, records, mode='llm', operation=None):
    req = JobRequest(job_id=path.name, operation=operation or ('update' if records else 'initialize'),
        library_id='quality', base_version=program.workspace_digest(source),
        template=TemplateRef(id='general-knowledge', version=program.VERSION,
            package_sha256='0'*64), knowledge_subpath='knowledge', workspace_key='quality', config={'mode':mode})
    prepare_job(path, req, records, source)
    return req


def documents(source):
    # Reconcile real Markdown exactly as production does; do not use stale cached bodies.
    probe = source.parent/('inspect-'+source.name)
    req = job_at(probe, source, [], 'structured', operation='update')
    program.run('update', probe/'input', probe/'work', probe/'output')
    validate_result(probe, req, [])
    state = json.loads((probe/'output/workspace/metadata/general-knowledge/state.json').read_text(encoding='utf-8'))
    return {k:{f:v[f] for f in ('title','scope','body')} for k,v in state['documents'].items()}


def execute(args):
    sys.path.insert(0, str(ROOT/'data/plugin-test-deps'))
    import yaml
    provider = yaml.safe_load(args.models_config.read_text(encoding='utf-8'))['providers'][args.provider]
    if provider.get('api') != 'openai-completions':
        raise ValueError('OpenAI-compatible provider required')
    key = provider.get('apiKey','')
    if not isinstance(key,str) or key.startswith(('$','!')):
        raise ValueError('Existing literal credential required')
    base = provider['baseUrl'].rstrip('/')
    endpoint = base if base.endswith('/chat/completions') else base+'/chat/completions'
    root = args.output.resolve(); root.mkdir(parents=True, exist_ok=False)
    fixture = json.loads(args.fixtures.read_text(encoding='utf-8'))
    if args.baseline_run:
        baseline_report = json.loads((args.baseline_run/'report.json').read_text(encoding='utf-8'))
        if baseline_report['fixtures_sha256'] != hashlib.sha256(args.fixtures.read_bytes()).hexdigest():
            raise ValueError('Baseline fixtures differ')
        if (baseline_report['model'] != args.provider+'/'+args.model
                or baseline_report['repeats'] != args.repeats):
            raise ValueError('Baseline model/repeats differ')
    save(root/'fixtures.json', fixture)
    shutil.copy2(PROGRAM, root/'program-under-test.py')
    calls=[]; lock=Lock()

    class Relay(BaseHTTPRequestHandler):
        def log_message(self,*a): pass
        def do_POST(self):
            parts=self.path.strip('/').split('/')
            if len(parts)!=3 or parts[1:]!=['chat','completions']:
                self.send_error(404); return
            label=parts[0]; start=time.perf_counter()
            metric={'run':label,'status':'started'}
            try:
                payload=self.rfile.read(int(self.headers['Content-Length']))
                opener=request.build_opener(request.ProxyHandler({}),NoRedirect())
                with opener.open(request.Request(endpoint,data=payload,
                    headers={'Content-Type':'application/json','Authorization':'Bearer '+key}),timeout=43) as res:
                    data=res.read(1024*1024+1)
                if len(data)>1024*1024: raise ValueError('Large response')
                parsed=json.loads(data)
                content=parsed.get('choices',[{}])[0].get('message',{}).get('content','')
                metric.update(status='ok',usage=parsed.get('usage',{}),
                    response=content.replace(key,'[REDACTED]') if key else content)
                self.send_response(200); self.send_header('Content-Length',str(len(data))); self.end_headers()
                self.wfile.write(data)
            except Exception as exc:
                metric.update(status='failed',error_type=type(exc).__name__,http_status=getattr(exc,'code',None))
                try: self.send_error(502,'Upstream failure')
                except OSError: pass
            finally:
                metric['seconds']=round(time.perf_counter()-start,3)
                with lock:
                    calls.append(metric); save(root/'model-calls.json',calls)

    relay=ThreadingHTTPServer(('127.0.0.1',0),Relay)
    thread=Thread(target=relay.serve_forever,daemon=True); thread.start()
    summary={'model':args.provider+'/'+args.model,'plugin_version':program.VERSION,
        'program_sha256':hashlib.sha256(PROGRAM.read_bytes()).hexdigest(),
        'fixtures_sha256':hashlib.sha256(args.fixtures.read_bytes()).hexdigest(),
        'repeats':args.repeats,'parallelism':args.parallelism,'started_at':time.time(),
        'baseline_run':str(args.baseline_run) if args.baseline_run else None,
        'grading':'Fixed expectations; inspect evidence and grade separately. Transport success is not quality.',
        'runs':[]}
    save(root/'report.json',summary)
    try:
        empty=root/'empty'; empty.mkdir()
        init=root/'initialize'; req=job_at(init,empty,[],'structured')
        program.run('initialize',init/'input',init/'work',init/'output'); validate_result(init,req,[])
        seeds=[record(s['id'],s['text'],s['title'],s['scope']) for s in fixture['seeds']]
        seedjob=root/'seed'; req=job_at(seedjob,init/'output/workspace',seeds,'structured')
        def seed_plan(mat,*_):
            return [dict(action='create',type='reference',title=mat['title'],summary=mat['title'],
                body=mat['content']['text'],reason='Controlled baseline',evidence=[mat['content']['text']])]
        program.run('update',seedjob/'input',seedjob/'work',seedjob/'output',planner=seed_plan)
        validate_result(seedjob,req,seeds)
        baseline=seedjob/'output/workspace'

        def run_case(case,repeat):
            label=f"{case['id']}-r{repeat}"; folder=root/label; folder.mkdir()
            source=folder/'source'
            frozen_source=args.baseline_run/label/'source' if args.baseline_run else baseline
            shutil.copytree(frozen_source,source)
            if not args.baseline_run and case.get('edit')=='api':
                p=next(p for p in (source/'knowledge').rglob('reference_*.md') if 'Atlas生产API' in p.read_text(encoding='utf-8'))
                p.write_text(p.read_text(encoding='utf-8').replace('30秒','45秒')+'\n仅允许幂等请求重试。\n',encoding='utf-8')
            elif not args.baseline_run and case.get('edit')=='delete':
                next(p for p in (source/'knowledge').rglob('reference_*.md') if '每天02:00' in p.read_text(encoding='utf-8')).unlink()
            elif not args.baseline_run and case.get('edit')=='new':
                (source/'knowledge/release.md').write_text('---\nscope: Atlas\ntitle: Atlas发布流程\n---\n\nAtlas每周三发布。\n',encoding='utf-8')
            if case.get('copy_seed'):
                orig=next(s for s in seeds if s.id==case['copy_seed'])
                mat=orig.material.model_copy(update={'submission_id':label})
                rec=Record(id=label,material=mat)
            else:
                rec=record(label,case['text'],case.get('title','Atlas API 材料'),case.get('scope','Atlas'),case.get('kind','new_content'))
            before=documents(source)
            job=folder/'update'; req=job_at(job,source,[rec])
            env=dict(os.environ,KNOWLEDGE_LLM_BASE_URL=f'http://127.0.0.1:{relay.server_port}/{label}',
                KNOWLEDGE_LLM_MODEL=args.model,PYTHONIOENCODING='utf-8')
            env.pop('KNOWLEDGE_LLM_API_KEY',None)
            row={'id':label,'case':case['name'],'expected':case['expected'], 'input':rec.material.model_dump(),
                'before':before,'status':'started','source_digest':req.base_version}
            if args.baseline_run:
                prior=json.loads((args.baseline_run/label/'evidence.json').read_text(encoding='utf-8'))
                assert row['input']==prior['input']
                assert req.base_version==program.workspace_digest(frozen_source)
                row['baseline_input_identical']=True
            started=time.perf_counter()
            try:
                result=subprocess.run([sys.executable,'-B',str(root/'program-under-test.py'),'update',
                    '--input',str(job/'input'),'--work',str(job/'work'),'--output',str(job/'output')],
                    env=env,capture_output=True,timeout=155,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                if result.returncode:
                    # Plugin sanitizes diagnostics; never save provider credentials/headers.
                    row.update(status='failed',error=result.stderr.decode('utf-8',errors='replace').strip())
                else:
                    validate_result(job,req,[rec]); candidate=job/'output/workspace'
                    row.update(status='candidate_ready',after=documents(candidate))
                    ledger=[json.loads(l) for l in (candidate/'metadata/general-knowledge/processed.jsonl').read_text(encoding='utf-8').splitlines()]
                    row['changes']=next(x['changes'] for x in ledger if x['record_id']==label)
                    # Replay uses the real subprocess too. All prior decisions must be reused.
                    replay=folder/'replay'; rr=job_at(replay,candidate,[rec])
                    r=subprocess.run([sys.executable,'-B',str(root/'program-under-test.py'),'update',
                        '--input',str(replay/'input'),'--work',str(replay/'work'),'--output',str(replay/'output')],
                        env=env,capture_output=True,timeout=155,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                    row['replay_identical']=r.returncode==0 and program.workspace_digest(candidate)==program.workspace_digest(replay/'output/workspace')
                    if r.returncode==0: validate_result(replay,rr,[rec])
                row['input_unchanged']=program.workspace_digest(source)==req.base_version
            except Exception as exc:
                row.update(status='failed',error_type=type(exc).__name__)
            row['seconds']=round(time.perf_counter()-started,3)
            trace=job/'work/model-attempts.jsonl'
            row['model_attempts']=[json.loads(line) for line in trace.read_text(encoding='utf-8').splitlines()] if trace.exists() else []
            with lock:
                run_calls=[c for c in calls if c['run']==label]
            row['model_calls']=len(run_calls)
            row['tokens']=sum(c.get('usage',{}).get('total_tokens',0) for c in run_calls)
            save(folder/'evidence.json',row)
            return row

        with ThreadPoolExecutor(max_workers=args.parallelism) as pool:
            futures=[pool.submit(run_case,c,r) for c in fixture['cases'] for r in range(1,args.repeats+1)]
            for future in as_completed(futures):
                row=future.result(); summary['runs'].append(row); save(root/'report.json',summary)
                print(json.dumps({k:row[k] for k in ('id','status','seconds','model_calls')},ensure_ascii=False),flush=True)
        summary.update(finished_at=time.time(),state='completed',model_calls=len(calls))
        save(root/'report.json',summary)
    finally:
        relay.shutdown(); thread.join(); relay.server_close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--fixtures',type=Path,default=ROOT/'ops/fixtures/knowledge-quality.json')
    parser.add_argument('--models-config',type=Path,default=Path.home()/'.omp/agent/models.yml')
    parser.add_argument('--baseline-run',type=Path,help='Reuse this prior run\'s exact source snapshots and validate identical materials/model.')
    parser.add_argument('--provider',default='hyper-op'); parser.add_argument('--model',default='GLM-5.3-Flash')
    parser.add_argument('--repeats',type=int,default=2); parser.add_argument('--parallelism',type=int,choices=(1,2),default=2)
    execute(parser.parse_args())
