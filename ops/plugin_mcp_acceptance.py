"""Actual MCP checks against the isolated synthetic plugin acceptance output."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import uvicorn
from folder_kb.capabilities import Capabilities, Policy
from folder_kb.server import Settings, create_app
from folder_kb.templates import TemplateCatalog
from tools.benchmark_file_tools import Suite


async def run(root):
    report=json.loads((root/'report.json').read_text(encoding='utf-8'))
    if report.get('dataset')!='synthetic-only' or report.get('state')!='completed':
        raise ValueError('Requires a completed synthetic acceptance run')
    app=create_app(Settings(str(root/'pool'),state_dir=str(root/'platform'),admin_token=uuid.uuid4().hex))
    registry=app.state.registry;catalog=TemplateCatalog(registry)
    os.environ['KB_SEARCH_TRUSTED_PYTHON']='1'
    bindings=[catalog.binding(lid)['binding'] for lid in ('demo','bm25')]
    os.environ['KB_SEARCH_PLUGIN_ALLOWLIST']=','.join(catalog.get(b['template_id'],b['template_version'])['package_sha256'] for b in bindings)
    key=registry.create_key('synthetic-mcp-only',['demo','bm25'],None)
    Capabilities(registry).policy(key['key']['id'],Policy(permissions=[
        dict(library_id=lid,grep=False,search=True,write_policy='readonly') for lid in ('demo','bm25')]))
    sock=socket.socket();sock.bind(('127.0.0.1',0))
    server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False))
    thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
    url='http://127.0.0.1:'+str(sock.getsockname()[1])
    class Connection:
        client=Suite.client
    connection=Connection();connection.url=url
    checks=[]
    try:
        for _ in range(100):
            if server.started:break
            await asyncio.sleep(.05)
        if not server.started:raise RuntimeError('Isolated HTTP server failed to start')
        async with connection.client(key) as client:
            tools=[t.name for t in (await client.session.list_tools()).tools]
            checks.append(dict(name='catalog',passed='search' in tools and 'grep' not in tools and 'write_file' not in tools))
            for lid in ('demo','bm25'):
                for query in ('Atlas 超时','Boreal 超时','quartzunfindable998877'):
                    result=await client.call('search',library_id=lid,query=query,limit=5)
                    hits=result.get('results',[])
                    passed='_error' not in result and (not hits if query.startswith('quartz') else bool(hits))
                    checks.append(dict(name='search',library=lid,query=query,passed=passed,hits=len(hits),ms=client.calls[-1]['ms']))
                    if hits:
                        read=await client.call('read_file',library_id=lid,path=hits[0]['path'])
                        checks.append(dict(name='read_evidence',library=lid,passed='_error' not in read,ms=client.calls[-1]['ms']))
            # Paths outside the published knowledge view must not expose metadata.
            result=await client.call('read_file',library_id='demo',path='../metadata/general-knowledge/state.json')
            checks.append(dict(name='metadata_boundary',passed='_error' in result))
            result=await client.call('search',library_id='not-granted',query='Atlas')
            checks.append(dict(name='unauthorized_library',passed='_error' in result))
        output=dict(transport='MCP Streamable HTTP, loopback',tools=tools,checks=checks,
            passed=sum(c['passed'] for c in checks),total=len(checks))
        (root/'mcp-report.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(output,ensure_ascii=False))
    finally:
        registry.revoke_key(key['key']['id'])
        server.should_exit=True;thread.join(timeout=10);sock.close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True)
    asyncio.run(run(p.parse_args().output.resolve()))
