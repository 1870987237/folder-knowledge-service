"""Verify REST and real MCP access without writing to the knowledge folder."""
import argparse
import asyncio
import json
import os

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def check(url: str, query: str, library_id: str | None = None):
    token = os.getenv('KB_AGENT_KEY', os.getenv('KB_TOKEN', ''))
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    async with httpx.AsyncClient(headers=headers, timeout=30, trust_env=False) as client:
        params = {'library_id': library_id} if library_id else {}
        response = await client.get(url + '/api/v1/info', params=params)
        response.raise_for_status()
        print('REST:', json.dumps(response.json(), ensure_ascii=False))
        async with streamable_http_client(url + '/mcp', http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                print('MCP tools:', ', '.join(t.name for t in tools.tools))
                found = await session.call_tool('grep', {'query': query, 'limit': 3, **params})
                if found.isError:
                    raise RuntimeError('MCP search failed')
                result = found.structuredContent
                print('MCP grep:', json.dumps(result, ensure_ascii=False, indent=2))
                if result and result['matches']:
                    hit = result['matches'][0]
                    page = await session.call_tool('read_file', {
                        'path': hit['path'], 'start_line': max(1, hit['line'] - 2),
                        'end_line': hit['line'] + 2, 'expected_version': hit['version'], **params})
                    if page.isError:
                        raise RuntimeError('MCP read failed (file may have changed)')
                    print('MCP read:', json.dumps(page.structuredContent, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default=os.getenv('KB_URL', 'http://127.0.0.1:8790'))
    parser.add_argument('--query', default='example')
    parser.add_argument('--library', default=None)
    args = parser.parse_args()
    asyncio.run(check(args.url.rstrip('/'), args.query, args.library))
