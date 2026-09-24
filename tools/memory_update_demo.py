"""Isolated, real HTTP/MCP and real LLM acceptance demo. Never opens live state.

Run from the service checkout with: python -m tools.memory_update_demo --template-repo PATH
Requires an explicitly configured model provider; there is no fake-model fallback.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import uvicorn

from folder_kb.server import Settings, create_app

ADMIN = 'isolated-memory-demo-admin-only'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--template-repo', type=Path, required=True)
    parser.add_argument('--provider', choices=['deepseek', 'glm'], default='deepseek')
    parser.add_argument('--output', type=Path, default=Path('data/memory-update-demo'))
    parser.add_argument('--port', type=int, default=8797)
    parser.add_argument('--serve', action='store_true')
    args = parser.parse_args()
    repo = args.template_repo.resolve()
    subprocess.run([sys.executable, str(repo/'tools/package.py')], check=True, stdout=subprocess.DEVNULL)
    definition = json.loads((repo/'dist/registration.json').read_text(encoding='utf-8'))
    archive = (repo/'dist'/('memory-files-'+definition['version']+'.zip')).read_bytes()
    if args.provider == 'deepseek':
        base, model, key_name = os.environ['DEEPSEEK_API_BASE'], os.getenv('DEEPSEEK_MODEL','deepseek-chat'), 'DEEPSEEK_API_KEY'
    else:
        base, model, key_name = os.environ['GLM_BASE_URL'], os.environ['GLM_MODEL'], 'GLM_KEY'
    if not os.getenv(key_name):
        raise ValueError('Configure provider credentials before running the demo')
    run = (args.output/('run-'+uuid.uuid4().hex[:12])).resolve()
    root, state = run/'knowledge-pool', run/'state'
    (root/'memory/knowledge').mkdir(parents=True)
    (root/'memory/notes.txt').write_text('Auxiliary workspace file: preserve across publications.', encoding='utf-8')
    app = create_app(Settings(str(root), state_dir=str(state), admin_token=ADMIN))
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=args.port, log_level='error', access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic()+15
    while not server.started:
        if time.monotonic()>deadline:
            raise RuntimeError('Demo HTTP server did not start')
        time.sleep(.1)
    env = {k: os.environ[k] for k in ('PATH','SYSTEMROOT','WINDIR','TEMP','TMP') if k in os.environ}
    env.update(KB_ROOT=str(root),KB_STATE_DIR=str(state),KB_TEMPLATE_TRUSTED_PYTHON='1',KB_MODEL_KEY_ALLOWLIST=key_name,PYTHONIOENCODING='utf-8')
    env[key_name]=os.environ[key_name]
    worker = subprocess.Popen([sys.executable,'-m','folder_kb.template_worker'], env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        **({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}))
    url = f'http://127.0.0.1:{args.port}'
    admin = httpx.Client(base_url=url, headers={'Authorization':'Bearer '+ADMIN}, trust_env=False, timeout=30)

    def request(method, path, **kwargs):
        response = admin.request(method, '/admin/api'+path, **kwargs)
        if response.status_code>=400:
            raise RuntimeError('Admin request failed: '+str(response.status_code)+' '+response.text[:300])
        return response.json()

    def wait_job(job_id, expected):
        end=time.monotonic()+240
        while time.monotonic()<end:
            job=request('GET','/update-jobs/'+job_id)
            if job['status']==expected:
                return job
            if job['status']=='failed':
                raise RuntimeError('Update failed: '+str(job['error_code']))
            if worker.poll() is not None:
                raise RuntimeError('Worker exited')
            time.sleep(1)
        raise RuntimeError('Timed out waiting for '+expected)

    async def mcp_call(token, tool, arguments):
        async with httpx.AsyncClient(headers={'Authorization':'Bearer '+token}, trust_env=False) as client:
            async with streamable_http_client(url+'/mcp', http_client=client) as (read,write,_):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    result=await session.call_tool(tool,arguments)
                    if result.isError:
                        raise RuntimeError('MCP tool failed: '+tool)
                    return result.structuredContent

    try:
        request('POST','/libraries',json={'id':'memory','name':'记忆更新联调','path':'memory/knowledge','enabled':True})
        request('POST','/templates',json=definition)
        installed=request('PUT',f"/templates/{definition['id']}/versions/{definition['version']}/package",content=archive)
        request('PUT','/libraries/memory/template',json={'template_id':definition['id'],'template_version':definition['version'],
            'config':{'mode':'llm','default_scope':'demo-user'},'workspace':{'type':'local','root_path':'memory','knowledge_subpath':'knowledge'}})
        request('PUT','/models/demo-model',json={'id':'demo-model','name':args.provider,'base_url':base,'model':model,'api_key_env':key_name})
        request('PUT','/libraries/memory/update-policy',json={'enabled':True,'auto_trigger':True,
            'approved_package_sha256':installed['package_sha256'],'model_connection_id':'demo-model',
            'model_environment':{'MEMORY_LLM_BASE_URL':'base_url','MEMORY_LLM_MODEL':'model','MEMORY_LLM_API_KEY':'api_key'}})
        key=request('POST','/keys',json={'agent_name':'演示 Agent','library_ids':['memory']})
        request('PUT','/keys/'+key['key']['id']+'/inbox-grants',json={'library_ids':['memory']})
        job=request('POST','/libraries/memory/update-jobs',json={'operation':'initialize'})
        wait_job(job['id'],'candidate_ready')
        assert not (root/'memory/knowledge/INDEX.md').exists()
        request('POST','/update-jobs/'+job['id']+'/approve')
        wait_job(job['id'],'published')
        print('Initialization: reviewed and published',flush=True)

        def process(material, transport):
            if transport=='mcp':
                receipt=asyncio.run(mcp_call(key['token'],'submit_material',{'material':material}))
            else:
                with httpx.Client(base_url=url,headers={'Authorization':'Bearer '+key['token']},trust_env=False) as client:
                    response=client.post('/api/v1/inbox/submissions',json=material); response.raise_for_status(); receipt=response.json()
            end=time.monotonic()+30
            while time.monotonic()<end:
                item=asyncio.run(mcp_call(key['token'],'get_submission',{'item_id':receipt['id']}))
                if item.get('update_job'):
                    break
                time.sleep(1)
            job_id=item['update_job']['id']
            candidate=wait_job(job_id,'candidate_ready')
            assert candidate['result']['materials'][0]['outcome'] in ('accepted','partially_accepted')
            preview=request('GET','/update-jobs/'+job_id+'/preview')
            assert preview['changes']
            # Explicit demo approval after protocol checks; production always waits for a human.
            request('POST','/update-jobs/'+job_id+'/approve')
            wait_job(job_id,'published')
            item=asyncio.run(mcp_call(key['token'],'get_submission',{'item_id':receipt['id']}))
            assert item['result']['published'] is True
            return item

        material={'submission_id':'demo-preference','library_id':'memory','kind':'new_content','title':'沟通偏好',
            'content':{'text':'演示用户明确要求：日常工作汇报请使用中文，先给结论，再列出三条以内的要点。'},
            'source':{'reference':'demo:explicit-user-instruction'},'context':{'scope':'demo-user'}}
        first=process(material,'mcp')
        index=asyncio.run(mcp_call(key['token'],'read_file',{'library_id':'memory','path':'INDEX.md'}))
        assert index['lines']
        again=asyncio.run(mcp_call(key['token'],'submit_material',{'material':material}))
        assert again['duplicate'] is True and again['id']==first['id']
        print('MCP submit -> real LLM -> review -> publish -> MCP read: passed',flush=True)
        correction={**material,'submission_id':'demo-correction','kind':'correction','title':'更新汇报偏好',
            'content':{'text':'演示用户明确纠正此前的工作汇报偏好：保留中文且结论在前，但不再要求三条以内的要点，改为只给一句简短结论。'}}
        second=process(correction,'rest')
        print('REST correction -> real LLM -> second publication: passed',flush=True)
        observation={**material,'submission_id':'demo-observation','kind':'observation','title':'待核实观察',
            'content':{'text':'尚未确认演示用户是否希望每周收到汇报，不应作为已确认偏好。'}}
        third=process(observation,'mcp')
        scope=app.state.templates.snapshot_source('memory')
        assert (scope['root']/'notes.txt').read_text(encoding='utf-8').startswith('Auxiliary')
        assert (scope['root']/'metadata/memory-template/review.jsonl').is_file()
        assert not any('每周' in p.read_text(encoding='utf-8') for p in (scope['root']/'knowledge').glob('*.md'))
        report={'provider':args.provider,'real_llm':True,'admin_url':url+'/admin','run_directory':str(run),
            'workspace':str(scope['root']),'verified':['mcp_submission','rest_correction','manual_review_gate','published_mcp_read',
            'duplicate_idempotence','second_incremental_update','observation_deferred','auxiliary_preserved'],
            'receipts':[first,second,third]}
        (run/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        # Leave an additional candidate for interactive review in the isolated demo UI.
        if args.serve:
            pending={**material,'submission_id':'demo-review-in-ui','title':'供管理台审核的材料',
                'content':{'text':'演示项目约定：每次合并代码前必须运行相关测试，并在提交说明中列出验证结果。'}}
            asyncio.run(mcp_call(key['token'],'submit_material',{'material':pending}))
        print(json.dumps({'verified':report['verified'],'report':str(run/'verification.json'),'admin_url':url+'/admin'},ensure_ascii=False),flush=True)
        if args.serve:
            while True:
                time.sleep(1)
    finally:
        admin.close()
        worker.terminate()
        worker.wait(timeout=10)
        server.should_exit=True
        thread.join(10)


if __name__=='__main__':
    main()
