// Harness boundary only: no task hints, tool recipes, answers, or extra tools.
import { appendFileSync, readFileSync } from 'node:fs';

export default function (pi: any) {
  const allowed = JSON.parse(process.env.FKB_BENCHMARK_TOOLS || '[]') as string[];
  const trace = process.env.FKB_BENCHMARK_POLICY_TRACE!;
  const record = (event: object) => appendFileSync(trace, JSON.stringify(event) + '\n', 'utf8');
  const catalog = JSON.parse(readFileSync(process.env.FKB_BENCHMARK_CATALOG!, 'utf8'));
  const instructions = process.env.FKB_BENCHMARK_INSTRUCTIONS || '';
  let requestId = 0;
  async function rpc(method: string, params: object, signal?: AbortSignal) {
    const response = await fetch(process.env.FKB_BENCHMARK_URL!, {
      method: 'POST', signal,
      headers: {'Content-Type': 'application/json', Accept: 'application/json, text/event-stream',
        Authorization: 'Bearer ' + process.env.FKB_BENCHMARK_KEY,
        'MCP-Protocol-Version': '2025-11-25'},
      body: JSON.stringify({jsonrpc: '2.0', id: ++requestId, method, params}),
    });
    if (!response.ok) throw new Error('MCP HTTP ' + response.status);
    const body = await response.json() as any;
    if (body.error) throw new Error(JSON.stringify(body.error));
    return body.result;
  }
  // Tool definitions are taken verbatim from tools/list. This adapter only
  // avoids OMP's native discovery race; every operation still uses real MCP.
  for (const tool of catalog.tools) {
    pi.registerTool({
      name: 'mcp__folderkb_' + tool.name,
      label: tool.title || tool.name,
      description: tool.description || '',
      parameters: tool.inputSchema,
      async execute(_id: string, args: object, signal?: AbortSignal) {
        return await rpc('tools/call', {name: tool.name, arguments: args}, signal);
      },
    });
  }
  async function activate() {
    record({type: 'registered_tools', names: pi.getAllTools().map((t: any) => t.name)});
    const deadline = Date.now() + 30000;
    while (Date.now() < deadline) {
      const registered = new Set(pi.getAllTools().map((t: any) => t.name));
      if (allowed.length && allowed.every(name => registered.has(name))) {
        const active = pi.getActiveTools();
        if (active.length !== allowed.length || active.some((n: string) => !allowed.includes(n))) {
          await pi.setActiveTools(allowed);
        }
        record({type: 'active_tools', names: pi.getActiveTools()});
        return;
      }
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    record({type: 'activation_failed'});
    throw new Error('Benchmark MCP tools did not become ready');
  }
  pi.on('session_start', async () => {
    await rpc('initialize', {protocolVersion: '2025-11-25', capabilities: {}, clientInfo: {name: 'folder-kb-omp-benchmark', version: '1.0'}});
    await fetch(process.env.FKB_BENCHMARK_URL!, {method: 'POST',
      headers: {'Content-Type': 'application/json', Accept: 'application/json, text/event-stream',
        Authorization: 'Bearer ' + process.env.FKB_BENCHMARK_KEY, 'MCP-Protocol-Version': '2025-11-25'},
      body: JSON.stringify({jsonrpc: '2.0', method: 'notifications/initialized'})});
    await activate();
  });
  pi.on('before_agent_start', async (event: any) => {
    await activate();
    return {systemPrompt: event.systemPrompt + '\n\nMCP Server Instructions (server-provided):\n' + instructions};
  });
  pi.on('before_provider_request', (event: any) => {
    const names = (event.payload.tools || []).map((t: any) => t.function?.name || t.name);
    record({type: 'provider_tools', names});
  });
  pi.on('tool_call', (event: any) => {
    if (!allowed.includes(event.toolName)) {
      record({type: 'blocked_tool', name: event.toolName});
      return {block: true, reason: 'This benchmark permits only the connected knowledge platform tools.'};
    }
  });
}
