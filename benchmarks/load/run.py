"""Closed-loop concurrent REST load against a disposable HTTP subprocess."""
import argparse
import asyncio
from collections import Counter
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import httpx

from benchmarks.agent.report import percentile
from benchmarks.agent.runner import install_index_plugin
from benchmarks.provenance import freeze_source
from folder_kb.capabilities import Capabilities, Policy
from folder_kb.inbox import Inbox, Material
from folder_kb.template_package import build_package
from folder_kb.templates import TemplateBinding, TemplateCatalog
from folder_kb.updates import UpdatePolicy, Updates
from folder_kb.workspaces import WorkspaceSpec
from tools.benchmark_file_tools import Suite


def summarize(rows, wall_seconds):
    successes=[r for r in rows if r['success']]
    return {'requests':len(rows),'successes':len(successes),'success_rate':len(successes)/len(rows) if rows else 0,
        'wall_seconds':wall_seconds,'throughput':len(rows)/wall_seconds if wall_seconds else 0,
        'successful_throughput':len(successes)/wall_seconds if wall_seconds else 0,
        'statuses':dict(Counter(str(r['status']) for r in rows)),
        'errors':dict(Counter(r['error'] for r in rows if not r['success'])),
        'latency_ms':{f'p{p}':percentile([r['milliseconds'] for r in rows],p) for p in (50,95,99)},
        'success_latency_ms':{f'p{p}':percentile([r['milliseconds'] for r in successes],p) for p in (50,95,99)},
        'operations':dict(Counter(r['operation'] for r in rows))}


def stop(process):
    if process and process.poll() is None:
        if os.name=='nt':
            subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            process.terminate()
        try: process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait()


def start_http(suite, log):
    # Set up with the shared fixture helper, then use the real CLI in a separate
    # process so server/client do not share the Python GIL during measurements.
    suite.server.should_exit=True; suite.thread.join(15)
    if suite.thread.is_alive(): raise RuntimeError('Fixture server did not stop')
    suite.sock.close()
    settings=suite.app.user_middleware[0].kwargs['settings']
    env={k:v for k,v in os.environ.items() if k in ('SYSTEMROOT','WINDIR','TEMP','TMP','PATH')}
    env.update(PYTHONIOENCODING='utf-8',KB_ROOT=str(suite.registry.pool.root),KB_STATE_DIR=str(suite.registry.directory),
               KB_ADMIN_TOKEN=settings.admin_token,KB_SEARCH_TRUSTED_PYTHON=os.getenv('KB_SEARCH_TRUSTED_PYTHON',''),
               KB_SEARCH_PLUGIN_ALLOWLIST=os.getenv('KB_SEARCH_PLUGIN_ALLOWLIST',''),KB_TEMPLATE_TRUSTED_PYTHON='1')
    process=subprocess.Popen([sys.executable,'-m','folder_kb','--host','127.0.0.1','--port',str(suite.port)],env=env,
        stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    try:
        with httpx.Client(trust_env=False,timeout=1) as client:
            deadline=time.monotonic()+20
            while time.monotonic()<deadline and process.poll() is None:
                try:
                    if client.get(suite.url+'/healthz').status_code==200:return process,env
                except httpx.HTTPError: pass
                time.sleep(.1)
        raise RuntimeError('Isolated HTTP subprocess failed to start')
    except BaseException:
        stop(process); raise


def background_jobs(suite, number, file_count, file_bytes):
    catalog=TemplateCatalog(suite.registry)
    definition=json.loads(Path('examples/template-platform-config.json').read_text(encoding='utf-8'))
    # Use public admin API for template installation; metadata and seed creation
    # are setup, never included in transport timings.
    settings=suite.app.user_middleware[0].kwargs['settings']
    with httpx.Client(base_url=suite.url,headers={'Authorization':'Bearer '+settings.admin_token},trust_env=False,timeout=60) as admin:
        if not getattr(suite,'load_template_installed',False):
            archive=build_package(Path('examples/templates/reference-docs'))
            definition['source']={'archive_sha256':hashlib.sha256(archive).hexdigest()}
            admin.post('/admin/api/templates',json=definition).raise_for_status()
            admin.put('/admin/api/templates/reference-docs/versions/0.2.0/package',content=archive).raise_for_status()
            suite.load_template_installed=True
    jobs=[]
    for _ in range(number):
        lid='bg-'+uuid.uuid4().hex[:16]; root=suite.registry.pool.root/lid; root.mkdir()
        for i in range(file_count):
            (root/f'{i}.md').write_text('# synthetic\n'+'x' * file_bytes,encoding='utf-8')
        suite.registry.save_library(lid,'Load background',lid,True,create=True)
        catalog.bind(lid,TemplateBinding(template_id='reference-docs',template_version='0.2.0',workspace=WorkspaceSpec(type='local',root_path=lid)))
        key=suite.registry.create_key('load-background',[lid],None)
        Inbox(suite.registry).grants(key['key']['id'],[lid])
        Inbox(suite.registry).submit(key['token'],Material(library_id=lid,submission_id='synthetic',content={'text':'SYNTHETIC background material'}))
        with suite.registry.store.transaction() as db:
            approved=catalog.require(db,'reference-docs','0.2.0')['package_sha256']
        updates=Updates(suite.registry)
        updates.save_policy(lid,UpdatePolicy(enabled=True,auto_trigger=False,approved_package_sha256=approved))
        jobs.append(updates.enqueue(lid)['id'])
    return jobs


async def clients(suite, keys, lid, profile, requests_per_client, timeout, on_start=None):
    ready=asyncio.Event()
    async def client_loop(index,key):
        rows=[]
        async with httpx.AsyncClient(base_url=suite.url,headers={'Authorization':'Bearer '+key['token']},
                                    trust_env=False,timeout=timeout,limits=httpx.Limits(max_connections=1,max_keepalive_connections=1)) as client:
            await ready.wait()
            for sequence in range(requests_per_client):
                operation='search' if profile=='search' else 'write' if profile=='mixed' and (index*requests_per_client+sequence)%5==0 else 'read'
                start=time.perf_counter(); status=0; error=''; success=False
                try:
                    if operation=='write':
                        response=await client.post('/api/v1/write',json={'library_id':lid,'operation':'create','path':f'load/{index}-{sequence}.md',
                            'content':'# synthetic write\n','create_parents':True,'request_id':f'load-{index}-{sequence}'})
                    else:
                        params={'library_id':lid,'query':'synthetic'} if operation=='search' else {'library_id':lid,'path':suite.path(0)}
                        response=await client.get('/api/v1/'+operation,params=params)
                    status=response.status_code; success=200<=status<300
                    if not success:
                        try:error=response.json().get('error',f'http_{status}')
                        except ValueError:error=f'http_{status}'
                except httpx.TimeoutException:error='client_timeout'
                except httpx.HTTPError as exc:error=type(exc).__name__
                rows.append({'client':index,'sequence':sequence,'operation':operation,'success':success,
                             'status':status,'error':error,'milliseconds':(time.perf_counter()-start)*1000})
        return rows
    tasks=[asyncio.create_task(client_loop(i,k)) for i,k in enumerate(keys)]
    await asyncio.sleep(0)
    try:
        context=await on_start() if on_start else None
    except BaseException:
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        raise
    start=time.perf_counter(); ready.set()
    groups=await asyncio.gather(*tasks)
    return [r for group in groups for r in group],time.perf_counter()-start,context


def render(data):
    lines=['# 隔离并发负载评测','',
        '真实 HTTP CLI 独立进程，默认并发限制 32；客户端在同机闭环施压，无思考间隔、不自动重试。并发客户端数不等于在线用户数。',
        f"每客户端 {data['requests_per_client']} 次请求；每库 {data['files']} 个文件，每文件约 {data['file_bytes']} 字节；后台任务使用确定性测试模板。",
        '整体延迟包含失败请求；另列成功请求 P95，避免大量快速 503 把延迟数字变好。Search 与混合写入使用不同场景，避免把索引过期当成容量失败。','',
        '| 场景 | 并发客户端 | 成功/请求 | 成功 RPS | 全部 P95 ms | 成功 P95 ms | 错误 |',
        '|---|---:|---:|---:|---:|---:|---|']
    for r in data['runs']:
        s=r['summary']
        lines.append(f"| {r['profile']} | {r['clients']} | {s['successes']}/{s['requests']} | {s['successful_throughput']:.2f} | {s['latency_ms']['p95']:.1f} | {s['success_latency_ms']['p95']:.1f} | {s['errors']} |")
    lines+=['','后台场景记录任务状态及总等待时间；不自动审核发布。此为本机短时负载，不能承诺生产 SLA 或百人持续容量。','']
    return '\n'.join(lines)


async def run(args):
    if args.output.exists() and any(args.output.iterdir()):raise ValueError('Use an empty output directory')
    if not args.clients or len(set(args.clients))!=len(args.clients) or any(n<1 or n>200 for n in args.clients) or not 1<=args.requests<=100 or not 1<=args.files<=256 or not 128<=args.file_bytes<=32768 or args.timeout<=0:
        raise ValueError('Bounded synthetic load only: clients 1..200, requests 1..100, files 1..256, bytes 128..32768')
    args.output.mkdir(parents=True,exist_ok=True)
    suite=Suite(args.output,short_fixture=True); admin=None; process=None; worker=None
    saved={k:os.environ.get(k) for k in ('KB_SEARCH_TRUSTED_PYTHON','KB_SEARCH_PLUGIN_ALLOWLIST')}
    data={'environment':suite.result['environment'],'source_snapshot':freeze_source(args.output),'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
          'transport':'REST loopback; separate HTTP process','requests_per_client':args.requests,'files':args.files,
          'file_bytes':args.file_bytes,'planned_profiles':args.profiles,'planned_clients':args.clients,'runs':[],'status':'running'}
    try:
        admin=install_index_plugin(suite,args); data['plugin']=suite.result['plugin']
        with (args.output/'http.log').open('ab') as log, (args.output/'worker.log').open('ab') as worker_log:
            process,env=start_http(suite,log)
            for profile in args.profiles:
                for number in args.clients:
                    lid,key,root=suite.setup(count=args.files,size=args.file_bytes)
                    # Ensure the fixed search term occurs in each synthetic document.
                    for path in root.rglob('*.md'):
                        with path.open('a',encoding='utf-8') as stream:stream.write('\nsynthetic load marker\n')
                    if profile=='search':
                        admin.put(f'/admin/api/libraries/{lid}/plugin',json={'plugin_id':'benchmark-rag','plugin_version':args.plugin_version}).raise_for_status()
                        admin.post(f'/admin/api/libraries/{lid}/index/build').raise_for_status()
                    keys=[]
                    for i in range(number):
                        k=suite.registry.create_key(f'load-{i}',[lid],None)
                        Capabilities(suite.registry).policy(k['key']['id'],Policy(permissions=[{'library_id':lid,'grep':True,
                            'search':profile=='search','write_policy':'direct' if profile=='mixed' else 'readonly'}]))
                        keys.append(k)
                    jobs=background_jobs(suite,3,args.files,args.file_bytes) if profile=='background' else []
                    async def begin_background():
                        nonlocal worker
                        worker=subprocess.Popen([sys.executable,'-m','folder_kb.template_worker'],env=env,stdin=subprocess.DEVNULL,
                            stdout=worker_log,stderr=worker_log,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                        # Startup time must not silently turn this into an idle
                        # read test. Wait until actual candidate work begins.
                        updates=Updates(suite.registry); deadline=time.monotonic()+20
                        while time.monotonic()<deadline and worker.poll() is None:
                            start_states=[updates.get(j)['status'] for j in jobs]
                            if 'running' in start_states or all(s in ('candidate_ready','failed') for s in start_states):break
                            await asyncio.sleep(.05)
                        start_states=[updates.get(j)['status'] for j in jobs]
                        return start_states
                    rows,elapsed,start_states=await clients(suite,keys,lid,profile,args.requests,args.timeout,begin_background if jobs else None)
                    row={'profile':profile,'clients':number,'summary':summarize(rows,elapsed),'requests':rows}
                    if profile=='mixed':
                        published=sum(r['success'] and r['operation']=='write' for r in rows)
                        created=list((suite.current(lid)/'load').glob('*.md'))
                        row['write_verification']={'successful_responses':published,'created_files':len(created),'matched':published==len(created)}
                    if jobs:
                        start=time.monotonic(); updates=Updates(suite.registry)
                        while time.monotonic()-start<60:
                            states=[updates.get(j)['status'] for j in jobs]
                            if all(s in ('candidate_ready','failed') for s in states):break
                            if worker.poll() is not None:break
                            await asyncio.sleep(.2)
                        row['background']={'states':[updates.get(j)['status'] for j in jobs],
                                           'states_at_load_start':start_states,'running_at_load_start':'running' in start_states,
                                           'post_load_wait_seconds':time.monotonic()-start,'worker_exit_code':worker.poll()}
                        stop(worker); worker=None
                    data['runs'].append(row)
                    for k in keys+[key]:suite.registry.revoke_key(k['key']['id'])
                    (args.output/'results.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
                    (args.output/'report.md').write_text(render(data),encoding='utf-8')
                    print(json.dumps({'profile':profile,'clients':number,'successes':row['summary']['successes'],'requests':len(rows)}),flush=True)
            data['status']='completed'
            (args.output/'results.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    except Exception as exc:
        data.update(status='failed',error_type=type(exc).__name__)
        (args.output/'results.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        raise
    finally:
        stop(worker);stop(process)
        if admin:admin.close()
        suite.close()
        for name,value in saved.items():
            if value is None:os.environ.pop(name,None)
            else:os.environ[name]=value


def main():
    logging.disable(logging.CRITICAL)
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--clients',type=int,nargs='+',default=[1,5,10,25,50,100])
    p.add_argument('--profiles',nargs='+',choices=['read','search','mixed','background'],default=['read','search','mixed','background'])
    p.add_argument('--requests',type=int,default=10)
    p.add_argument('--files',type=int,default=32)
    p.add_argument('--file-bytes',type=int,default=4096)
    p.add_argument('--timeout',type=float,default=30)
    p.add_argument('--plugin-definition',type=Path,default=Path('examples/plugins/simple-rag/definition.json'))
    p.add_argument('--plugin-source',type=Path,default=Path('examples/plugins/simple-rag/package'))
    p.add_argument('--trust-plugin',action='store_true')
    args=p.parse_args();args.output=args.output.resolve();asyncio.run(run(args))


if __name__=='__main__':main()
