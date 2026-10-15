"""Persistent full-corpus MCP evaluation: one shared index, isolated OMP sessions.

Each task is attempted once. Resume skips verified results and records interrupted
attempts as failures, never silently replaces them with a more successful retry.
"""
import argparse
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import uuid

import httpx
import uvicorn

from benchmarks.agent.tasks import DiskCorpus, load_dataset, fingerprint
from benchmarks.agent.runner import Recorder, BCP_SYSTEM, solve, parse_events
from benchmarks.agent.preflight import check_model
from benchmarks.browsecomp.evaluate import grade, export
from benchmarks.browsecomp.judge import judge_one, SYSTEM as JUDGE_SYSTEM
from benchmarks.browsecomp.analyze import analyze, render
from benchmarks.browsecomp.full_prepare import atomic_json, sha256, QUESTION_COUNT, DOCUMENT_COUNT
from benchmarks.browsecomp.campaign import source_hash
from benchmarks.provenance import freeze_source
from folder_kb.server import Settings, create_app
from folder_kb.capabilities import Capabilities, Policy
from folder_kb.plugin_package import package_plugin
from folder_kb.retrieval import Retrieval
from folder_kb.worker_runtime import worker_lock
from tools.benchmark_file_tools import Suite

PLUGIN = Path('examples/plugins/sqlite-corpus')


class FullService:
    client = Suite.client

    def __init__(self, dataset, output, recorder):
        # Short stable paths support Windows and survive runner restarts.
        self.run_root = Path(tempfile.gettempdir()) / ('fkb-full-' + hashlib.sha256(str(output.resolve()).encode()).hexdigest()[:16])
        self.run_root.mkdir(parents=True, exist_ok=True)
        marker = self.run_root/'owner.json'
        owner = dict(output=str(output.resolve()), dataset=str(dataset.resolve()))
        if marker.exists() and json.loads(marker.read_text(encoding='utf-8')) != owner:
            raise ValueError('Persistent fixture belongs to another benchmark')
        atomic_json(marker,owner)
        settings=Settings(str(dataset.resolve()),admin_token=uuid.uuid4().hex,state_dir=str(self.run_root/'state'))
        self.app=create_app(settings); self.registry=self.app.state.registry
        if not any(r['id']=='benchmark-corpus' for r in self.registry.libraries()):
            self.registry.save_library('benchmark-corpus','知识库','knowledge',True,create=True)
        with self.registry.store.transaction(write=True) as db:
            db.execute("UPDATE agent_keys SET revoked_at=? WHERE revoked_at IS NULL",(time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),))
        self.sock=socket.socket();self.sock.bind(('127.0.0.1',0));self.url=f'http://127.0.0.1:{self.sock.getsockname()[1]}'
        self.server=uvicorn.Server(uvicorn.Config(recorder.wrap(self.app),log_level='critical',access_log=False))
        self.thread=threading.Thread(target=self.server.run,kwargs={'sockets':[self.sock]},daemon=True);self.thread.start()
        deadline=time.monotonic()+15
        while not self.server.started and time.monotonic()<deadline:
            time.sleep(.02)
        if not self.server.started:
            raise RuntimeError('Isolated benchmark server did not start')
        self.admin=httpx.Client(base_url=self.url,headers={'Authorization':'Bearer '+settings.admin_token},trust_env=False,timeout=3700)

    def install(self):
        definition=json.loads((PLUGIN/'definition.json').read_text(encoding='utf-8'))
        definition['plugin']['instructions']='根据知识库中当前有效资料完成用户任务。引用来源；缺少证据时如实说明。'
        file=self.run_root/'definition.json';atomic_json(file,definition)
        package_root=self.run_root/'package'
        if not package_root.exists():
            clean=self.run_root/'source';clean.mkdir(exist_ok=True)
            for source in (PLUGIN/'package').rglob('*'):
                if source.is_file() and '__pycache__' not in source.parts and source.suffix!='.pyc':
                    target=clean/source.relative_to(PLUGIN/'package');target.parent.mkdir(parents=True,exist_ok=True)
                    shutil.copyfile(source,target)
            packed=package_plugin(clean,file,package_root)
            atomic_json(self.run_root/'package.json',packed)
        packed=json.loads((self.run_root/'package.json').read_text(encoding='utf-8'))
        manifest=json.loads(Path(packed['manifest']).read_text(encoding='utf-8'))
        response=self.admin.post('/admin/api/plugins',json=manifest)
        if response.status_code not in (201,409):
            response.raise_for_status()
        response=self.admin.put('/admin/api/plugins/sqlite-corpus/versions/1.0.0/package',content=Path(packed['package']).read_bytes())
        response.raise_for_status()
        self.plugin={'definition':definition,'package_sha256':response.json()['package_sha256']}
        os.environ['KB_SEARCH_TRUSTED_PYTHON']='1';os.environ['KB_SEARCH_PLUGIN_ALLOWLIST']=self.plugin['package_sha256']
        bound=self.admin.put('/admin/api/libraries/benchmark-corpus/plugin',json={'plugin_id':'sqlite-corpus','plugin_version':'1.0.0'})
        bound.raise_for_status()

    def close(self):
        self.admin.close();self.server.should_exit=True;self.thread.join(timeout=15);self.sock.close()


def interrupted_execution(directory):
    """Recover written execution evidence, or retain a crashed attempt as failed."""
    path=directory/'execution.json'
    trace=directory/'tool-trace.json'
    calls=json.loads(trace.read_text(encoding='utf-8')) if trace.exists() else []
    if path.exists() and trace.exists():
        return json.loads(path.read_text(encoding='utf-8')),calls
    execution=dict(parse_events(''),valid_run=False,timed_out=False,returncode=-1,wall_ms=0,
        tool_calls=len(calls),tool_errors=sum(c.get('is_error',False) for c in calls),tool_ms=0,
        enabled_agent_tools=[],tools_used={},available_tools=[],unexpected_agent_tools=[],provider_tool_sets=[],
        knowledge_available=True,interrupted=True)
    atomic_json(trace,calls)
    return execution,calls


def clear_interrupted_profile(directory):
    profile=directory/'profile'
    if profile.exists():
        if profile.resolve().parent!=directory.resolve() or profile.is_symlink() or getattr(profile,'is_junction',lambda:False)():
            raise ValueError('Unsafe interrupted profile path')
        shutil.rmtree(profile)


def result_row(task,execution,calls,known):
    evaluation=grade(task,known,known,execution['final'],calls)
    if not execution['valid_run'] or execution['timed_out'] or execution['returncode']!=0:
        evaluation['success']=evaluation['outcome_success']=False
    return dict(episode=f'{task.id}-search-1',task_id=task.id,kind=task.kind,variant='search',repeat=1,
        prompt=task.prompt,**execution,index_build_ms=0,evaluation=evaluation,publications=0,repeated_identical_errors=0)


def save_reports(root,data,judgments):
    atomic_json(root/'execution/results.json',data)
    judgments['run_sha256']=sha256(root/'execution/results.json')
    atomic_json(root/'judge/judgments.json',judgments)
    # Analysis uses lazy membership checks; corpus bodies are never loaded here.
    summary=analyze(root/'execution',Path(data['dataset_path']),root/'judge/judgments.json')
    atomic_json(root/'metrics.json',summary)
    report=render(summary)
    (root/'report.md').write_text(report,encoding='utf-8')


async def run(args):
    root=args.output.resolve();root.mkdir(parents=True,exist_ok=True)
    corpus,tasks,metadata=load_dataset(args.dataset)
    if not isinstance(corpus,DiskCorpus):
        raise ValueError('Import the official full corpus before using full_run')
    if not args.allow_fixture and (len(tasks)!=QUESTION_COUNT or len(corpus)!=DOCUMENT_COUNT):
        raise ValueError('Full run requires ALL 830 questions and 100195 documents')
    if args.tasks and set(args.tasks)-{t.id for t in tasks}:
        raise ValueError('Unknown task IDs')
    selected=[t for t in tasks if not args.tasks or t.id in args.tasks]
    plan=dict(dataset=metadata,task_ids=[t.id for t in selected],variants=['search'],repeats=1,
        planned_episodes=len(selected),model=args.model,judge_model=args.judge_model,timeout_seconds=args.timeout,
        source_sha256=source_hash(),plugin_sha256=fingerprint({p.name:sha256(p) for p in (PLUGIN/'package').glob('*') if p.is_file()}),
        plugin_definition_sha256=sha256(PLUGIN/'definition.json'),omp_version=subprocess.check_output([args.omp,'--version'],text=True).strip(),
        scope='all-questions' if len(selected)==QUESTION_COUNT else 'full-corpus-integration-subset',
        resume_policy='completed results immutable; interrupted attempts retained as failures, never re-solved')
    planfile=root/'plan.json'
    if planfile.exists():
        if not args.resume or json.loads(planfile.read_text(encoding='utf-8'))!=plan:
            raise ValueError('Use --resume with identical data, code, plugin, model and task matrix')
    else:
        if any(root.iterdir()):
            raise ValueError('Output must be new/empty')
        atomic_json(planfile,plan);freeze_source(root)
    (root/'execution/episodes').mkdir(parents=True,exist_ok=True)
    (root/'judge').mkdir(exist_ok=True)
    print(json.dumps({'stage':'verify_corpus','documents':len(corpus),'questions':len(selected)}),flush=True)
    recorder=Recorder();service=None
    saved={k:os.environ.get(k) for k in ('KB_SEARCH_TRUSTED_PYTHON','KB_SEARCH_PLUGIN_ALLOWLIST')}
    stage='verify_corpus'
    def preparing(progress=None):
        atomic_json(root/'status.json',dict(state='preparing',stage=stage,planned=len(selected),completed=0,
            progress=progress,worker_pid=os.getpid(),updated_at=time.time()))
    try:
        preparing()
        corpus.verify(progress=preparing)
        stage='prepare_service'
        preparing()
        service=FullService(args.dataset,root,recorder);service.install()
        atomic_json(root/'service.json',dict(state_dir=str(service.registry.directory),url=service.url))
        retrieval=Retrieval(service.registry)
        state=retrieval.state('benchmark-corpus')
        if state['status']!='ready':
            if any((root/'execution/episodes').iterdir()):
                raise ValueError('Index unavailable after answers started; refusing to rebuild within this run')
            stage='build_full_index'
            preparing()
            print(json.dumps({'stage':'build_full_index'}),flush=True)
            started=time.perf_counter();state=await asyncio.to_thread(retrieval.build,'benchmark-corpus',progress=preparing)
            atomic_json(root/'index.json',dict(state,build_seconds=time.perf_counter()-started))
        elif (root/'index.json').exists():
            recorded_index=json.loads((root/'index.json').read_text(encoding='utf-8'))
            if any(recorded_index[k]!=state[k] for k in ('generation','artifact_sha256','content_version','identity')):
                raise ValueError('Prepared index generation differs from this run')
        else:
            if any((root/'execution/episodes').iterdir()):
                raise ValueError('Missing index record after answers started')
            atomic_json(root/'index.json',dict(state,build_seconds=None))
        if args.prepare_only:
            atomic_json(root/'status.json',dict(state='index_ready',planned=len(selected),completed=0))
            print(json.dumps({'stage':'index_ready','files':state['files']}),flush=True);return
        stage='model_preflight'
        preparing()
        health=await check_model(args);atomic_json(root/'preflight.json',health)
        if not health['ok']:
            raise RuntimeError('Model preflight failed; no answers attempted')
        results_file=root/'execution/results.json'
        data=json.loads(results_file.read_text(encoding='utf-8')) if results_file.exists() else dict(
            environment={'os':platform.platform(),'python':platform.python_version(),'transport':'MCP Streamable HTTP, loopback'},
            benchmark_version='2.0-full-corpus',grader_version='1.3',plan=plan,dataset=metadata,dataset_path=str(args.dataset.resolve()),
            model=args.model,omp_version=plan['omp_version'],source_sha256=plan['source_sha256'],
            system_prompt=BCP_SYSTEM,plugin=service.plugin,episodes=[])
        judgefile=root/'judge/judgments.json'
        judgments=json.loads(judgefile.read_text(encoding='utf-8')) if judgefile.exists() else dict(
            metric='internal-semantic-judgment',official_leaderboard_comparable=False,judge_model=args.judge_model,
            solver_model=args.model,same_model_as_solver=args.model==args.judge_model,
            prompt_sha256=hashlib.sha256(JUDGE_SYSTEM.encode()).hexdigest(),episodes=[])
        rows={e['episode']:e for e in data['episodes']};judged={e['episode'] for e in judgments['episodes']}
        known={name:entry['sha256'] for name,entry in corpus.entries.items()}
        stage='evaluate'
        for task in selected:
            if source_hash()!=plan['source_sha256']:
                raise ValueError('Source changed during execution; refusing to mix versions')
            name=f'{task.id}-search-1';directory=root/'execution/episodes'/name
            if name in rows:
                saved_row=json.loads((directory/'result.json').read_text(encoding='utf-8'))
                if saved_row!=rows[name]:
                    raise ValueError('Completed result changed')
                record=json.loads((directory/'checkpoint.json').read_text(encoding='utf-8'))
                if record!={f:sha256(directory/f) for f in ('result.json','tool-trace.json')}:
                    raise ValueError('Completed episode evidence changed')
                row=rows[name]
            else:
                atomic_json(root/'status.json',dict(state='running',planned=len(selected),completed=len(rows),current=name))
                if directory.exists():
                    clear_interrupted_profile(directory)
                    execution,calls=interrupted_execution(directory)
                else:
                    directory.mkdir();atomic_json(directory/'started.json',dict(task_id=task.id,time=time.time()))
                    key=service.registry.create_key('benchmark-'+task.id,['benchmark-corpus'],None)
                    Capabilities(service.registry).policy(key['key']['id'],Policy(permissions=[
                        dict(library_id='benchmark-corpus',grep=False,search=True,write_policy='readonly')]))
                    execution,calls=await solve(service,recorder,key,directory,task.prompt,args,system_prompt=BCP_SYSTEM)
                row=result_row(task,execution,calls,known);atomic_json(directory/'result.json',row)
                atomic_json(directory/'checkpoint.json',{f:sha256(directory/f) for f in ('result.json','tool-trace.json')})
                data['episodes'].append(row);rows[name]=row
                save_reports(root,data,judgments)
            if name not in judged:
                if not row['valid_run'] or row['timed_out'] or row['returncode']!=0:
                    judgment=dict(episode=name,correct=None,error='solver_execution_failed')
                elif (root/'judge'/name).exists():
                    clear_interrupted_profile(root/'judge'/name)
                    saved_judge=root/'judge'/name/'judgment.json'
                    judgment=dict(episode=name,**json.loads(saved_judge.read_text(encoding='utf-8'))) if saved_judge.exists() else dict(episode=name,correct=None,error='judge_interrupted')
                else:
                    judge_args=SimpleNamespace(omp=args.omp,models_config=args.models_config,model=args.judge_model,timeout=90)
                    judgment=await judge_one(row,task,judge_args,root/'judge')
                judgments['episodes'].append(dict(judgment,variant='search'));judged.add(name)
                save_reports(root,data,judgments)
            print(json.dumps({'episode':name,'completed':len(rows),'planned':len(selected),'calls':row['tool_calls'],
                'seconds':round(row['wall_ms']/1000,2),'correct':next(j['correct'] for j in judgments['episodes'] if j['episode']==name)}),flush=True)
            recorder.calls.clear();recorder.catalogs.clear();recorder.instructions.clear()
        stage='verify_corpus_after'
        corpus.verify()
        if not (root/'official-format').exists():
            export(data,root/'official-format')
        atomic_json(root/'status.json',dict(state='completed',planned=len(selected),completed=len(rows),corpus_verified_after=True))
    except Exception as exc:
        atomic_json(root/'status.json',dict(state='interrupted',stage=stage,error_type=type(exc).__name__,planned=len(selected)))
        raise
    finally:
        if service:
            service.close()
        for k,v in saved.items():
            if v is None:os.environ.pop(k,None)
            else:os.environ[k]=v


def main():
    logging.disable(logging.CRITICAL)
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset',type=Path,default=Path('data/browsecomp-plus-full'))
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--model',default='hyper-op/GLM-5.3-Flash');p.add_argument('--judge-model',default='hyper-op/GLM-5.3-Flash')
    p.add_argument('--omp',default=shutil.which('omp') or 'omp');p.add_argument('--models-config',type=Path,default=Path.home()/'.omp/agent/models.yml')
    p.add_argument('--timeout',type=int,default=300);p.add_argument('--resume',action='store_true')
    p.add_argument('--prepare-only',action='store_true');p.add_argument('--tasks',nargs='+')
    p.add_argument('--allow-fixture',action='store_true',help='For synthetic tests only; never labels these as full official data')
    args=p.parse_args()
    if args.timeout<1:p.error('Timeout must be positive')
    lock=hashlib.sha256(str(args.output.resolve()).encode()).hexdigest()[:16]
    with worker_lock(Path(tempfile.gettempdir()),'fkb-full-'+lock+'.lock'):
        asyncio.run(run(args))


if __name__=='__main__':
    main()
