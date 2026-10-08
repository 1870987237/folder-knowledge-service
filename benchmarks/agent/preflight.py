"""Check the configured solver without exposing questions, tools or credentials."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time


async def stop_process(process):
    if process.returncode is None:
        if os.name == 'nt':
            killer = await asyncio.create_subprocess_exec(
                'taskkill', '/PID', str(process.pid), '/T', '/F',
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW)
            await killer.wait()
        else:
            process.kill()
    return await process.communicate()


async def check_model(args):
    from benchmarks.agent.runner import parse_events, prepare_profile
    started = time.perf_counter()
    result = {'ok': False, 'model': args.model, 'status': 'not_started'}
    with tempfile.TemporaryDirectory(prefix='fkb-benchmark-preflight-') as temporary:
        root = Path(temporary)
        profile = root / 'profile'
        try:
            prepare_profile(profile, args.models_config, 'http://127.0.0.1:1/unused', 'unused')
            (profile / 'mcp.json').write_text('{"mcpServers":{}}', encoding='utf-8')
            process = await asyncio.create_subprocess_exec(
                args.omp, '--cwd', str(root), '--no-tools', '--no-extensions', '--no-skills',
                '--no-rules', '--no-lsp', '--no-title', '--no-session', '--thinking', 'off',
                '--model', args.model, '--mode', 'json', '--max-time', '45',
                '--system-prompt', 'Reply with only OK. Do not use tools.', '-p', 'Connection check: reply OK.',
                env=dict(os.environ, PI_CODING_AGENT_DIR=str(profile)), cwd=root,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), 55)
                events = parse_events(stdout.decode('utf-8', errors='replace'))
                result.update(ok=process.returncode == 0 and events['model_response_messages'] > 0
                              and not events['agent_tool_attempts'] and bool(events['final'].strip()),
                              status='responded' if process.returncode == 0 else 'provider_or_agent_failed',
                              returncode=process.returncode, usage=events['usage'])
            except asyncio.TimeoutError:
                await stop_process(process)
                result['status'] = 'timeout'
        except (OSError, ValueError):
            result['status'] = 'configuration_or_launch_failed'
        # Raw diagnostics may contain provider secrets; only retain categorical status.
    result['wall_ms'] = round((time.perf_counter() - started) * 1000, 3)
    return result


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--omp', default=shutil.which('omp') or 'omp')
    parser.add_argument('--model', default='hyper-op/GLM-5.3-Flash')
    parser.add_argument('--models-config', type=Path, default=Path.home() / '.omp/agent/models.yml')
    result = asyncio.run(check_model(parser.parse_args()))
    print(json.dumps(result))
    raise SystemExit(0 if result['ok'] else 1)


if __name__ == '__main__':
    main()
