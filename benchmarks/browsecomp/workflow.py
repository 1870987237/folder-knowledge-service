"""One full evaluation workflow with child logs, heartbeats and explicit resume.

Run this module from an independent host process for long experiments. It does
not create a daemon or restart answered tasks. The episode runner owns answer
immutability; this module only sequences preparation, MCP check and evaluation.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from benchmarks.agent.tasks import load_dataset
from benchmarks.browsecomp.campaign import source_hash
from benchmarks.browsecomp.full_prepare import atomic_json, QUESTION_COUNT, DOCUMENT_COUNT
from folder_kb.worker_runtime import worker_lock, liveness

ROOT=Path(__file__).resolve().parents[2]


def read_status(path):
    value=json.loads(Path(path).read_text(encoding='utf-8'))
    value['worker_liveness']=liveness(value.get('updated_at'))['status']
    value['effective_state']='interrupted' if value['state']=='running' and value['worker_liveness']=='offline' else value['state']
    return value


class Workflow:
    def __init__(self, root, *, poll_seconds=5):
        self.root=Path(root).resolve()
        self.root.mkdir(parents=True,exist_ok=True)
        self.poll_seconds=poll_seconds
        self.status=dict(state='running',stage='starting',worker_pid=os.getpid(),child_pid=None,child_state=None)

    def save(self):
        self.status['updated_at']=time.time()
        atomic_json(self.root/'workflow.json',self.status)

    def execute(self, stage, command, progress_file=None):
        self.status.update(state='running',stage=stage,child_state=None,error_type=None,returncode=None)
        self.save()
        print(json.dumps(dict(stage=stage,state='starting')),flush=True)
        with (self.root/(stage+'.stdout.log')).open('ab') as out, (self.root/(stage+'.stderr.log')).open('ab') as err:
            process=None
            try:
                process=subprocess.Popen(command,cwd=ROOT,stdout=out,stderr=err,
                    env=dict(os.environ,PYTHONIOENCODING='utf-8'),
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                self.status['child_pid']=process.pid
                while True:
                    if progress_file and Path(progress_file).is_file():
                        try:self.status['child_state']=json.loads(Path(progress_file).read_text(encoding='utf-8'))
                        except (OSError,ValueError):pass
                    self.save()
                    try:
                        code=process.wait(timeout=self.poll_seconds)
                        break
                    except subprocess.TimeoutExpired:
                        continue
                self.status.update(child_pid=None,returncode=code)
                if code!=0:
                    raise RuntimeError(f'{stage} exited with status {code}; inspect stage logs')
                self.save()
            except BaseException as exc:
                if process and process.poll() is None:
                    process.terminate()
                    try:process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill();process.wait()
                self.status.update(state='interrupted',error_type=type(exc).__name__,child_pid=None)
                self.save()
                raise


async def probe(dataset, run):
    from benchmarks.agent.runner import Recorder
    from benchmarks.browsecomp.full_run import FullService
    from folder_kb.capabilities import Capabilities, Policy
    service=None
    try:
        service=FullService(dataset,run,Recorder());service.install()
        key=service.registry.create_key('full-corpus-mcp-check',['benchmark-corpus'],None)
        Capabilities(service.registry).policy(key['key']['id'],Policy(permissions=[
            dict(library_id='benchmark-corpus',grep=False,search=True,write_policy='readonly')]))
        try:
            async with service.client(key) as client:
                catalog=await client.session.list_tools();names=[t.name for t in catalog.tools]
                if 'search' not in names or 'grep' in names:
                    raise ValueError('Wrong MCP tool configuration')
                started=time.perf_counter()
                result=await client.call('search',query='orchard',library_id='benchmark-corpus',limit=3)
                seconds=time.perf_counter()-started
                if '_error' in result or not result.get('results'):
                    raise ValueError('Full-corpus MCP Search failed')
                atomic_json(run/'mcp-smoke.json',dict(success=True,tools=names,query='orchard',
                    returned_results=len(result['results']),search_seconds=seconds,
                    purpose='neutral MCP integration check; not an Agent answer'))
        finally:service.registry.revoke_key(key['key']['id'])
    except Exception as exc:
        atomic_json(run/'mcp-smoke.json',dict(success=False,error_type=type(exc).__name__))
        raise
    finally:
        if service:service.close()


def run(args):
    root=args.output.resolve();root.mkdir(parents=True,exist_ok=True)
    with worker_lock(root,'workflow.lock'):
        corpus,tasks,metadata=load_dataset(args.dataset)
        if not args.allow_fixture and (len(corpus)!=DOCUMENT_COUNT or len(tasks)!=QUESTION_COUNT):
            raise ValueError('Full workflow requires all official documents and questions')
        planned=len(tasks)
        del corpus,tasks
        plan=dict(dataset=metadata,model=args.model,judge_model=args.judge_model,timeout=args.timeout,
            source_sha256=source_hash(),planned_agent_episodes=planned,planned_retrieval_queries=planned,
            phases=['preflight','prepare','mcp_check','retrieval','agent'])
        plan_file=root/'workflow-plan.json'
        if plan_file.exists():
            if not args.resume or json.loads(plan_file.read_text(encoding='utf-8'))!=plan:
                raise ValueError('Resume requires unchanged workflow inputs and source')
        else:atomic_json(plan_file,plan)
        worker=Workflow(root);worker.status.update(planned_agent_episodes=planned,planned_retrieval_queries=planned)
        output=root/'agent';ranking=root/'retrieval'
        shared=['--dataset',str(args.dataset.resolve()),'--model',args.model,'--judge-model',args.judge_model,
            '--timeout',str(args.timeout),'--omp',args.omp,'--models-config',str(args.models_config.resolve())]
        base=[sys.executable,'-B','-m','benchmarks.browsecomp.full_run','--output',str(output),*shared]
        if args.allow_fixture:base.append('--allow-fixture')
        if (output/'plan.json').exists():base.append('--resume')
        try:
            worker.execute('preflight',[sys.executable,'-B','-m','benchmarks.agent.preflight',
                '--omp',args.omp,'--model',args.model,'--models-config',str(args.models_config.resolve())])
            worker.execute('prepare',[*base,'--prepare-only'],output/'status.json')
            worker.execute('mcp_check',[sys.executable,'-B','-m','benchmarks.browsecomp.workflow',
                '--dataset',str(args.dataset.resolve()),'--probe-run',str(output)])
            retrieve=[sys.executable,'-B','-m','benchmarks.browsecomp.full_retrieval',
                '--dataset',str(args.dataset.resolve()),'--run',str(output),'--output',str(ranking)]
            if (ranking/'plan.json').exists():retrieve.append('--resume')
            worker.execute('retrieval',retrieve,ranking/'status.json')
            if '--resume' not in base:base.append('--resume')
            worker.execute('agent',base,output/'status.json')
            worker.status.update(state='completed',stage='completed',child_pid=None)
            worker.save()
        except BaseException as exc:
            worker.status.update(state='interrupted',error_type=type(exc).__name__)
            worker.save()
            raise


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset',type=Path,default=Path('data/browsecomp-plus-full'))
    p.add_argument('--output',type=Path)
    p.add_argument('--model',default='hyper-op/GLM-5.3-Flash');p.add_argument('--judge-model',default='hyper-op/GLM-5.3-Flash')
    p.add_argument('--timeout',type=int,default=300)
    p.add_argument('--omp',default=shutil.which('omp') or 'omp')
    p.add_argument('--models-config',type=Path,default=Path.home()/'.omp/agent/models.yml')
    p.add_argument('--resume',action='store_true');p.add_argument('--status',action='store_true')
    p.add_argument('--allow-fixture',action='store_true');p.add_argument('--probe-run',type=Path)
    args=p.parse_args()
    if args.probe_run:asyncio.run(probe(args.dataset,args.probe_run.resolve()))
    elif not args.output:p.error('--output is required')
    elif args.status:print(json.dumps(read_status(args.output/'workflow.json'),ensure_ascii=False))
    elif args.timeout<1:p.error('Timeout must be positive')
    else:run(args)


if __name__=='__main__':main()
