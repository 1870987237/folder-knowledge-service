"""Run, judge and report a fixed BrowseComp-Plus dataset in one command."""
import argparse
import asyncio
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

from benchmarks.agent.runner import run as run_agent
from benchmarks.browsecomp.judge import run as run_judge
from benchmarks.browsecomp.analyze import analyze, render
from benchmarks.browsecomp.evaluate import export


async def run(args):
    root = args.output.resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError('Output must be new/empty; previous experiments are immutable')
    root.mkdir(parents=True, exist_ok=True)
    shared = dict(omp=args.omp, model=args.model, models_config=args.models_config, dataset=args.dataset)
    experiment = SimpleNamespace(**shared, output=root / 'execution', tasks=getattr(args,'tasks',None), variants=args.variants,
        repeats=args.repeats, timeout=args.timeout, fail_fast=False, trust_plugin=args.trust_plugin,
        plugin_definition=args.plugin_definition, plugin_source=args.plugin_source)
    status = {'stage': 'execution', 'completed': False, 'error_type': None}
    try:
        await run_agent(experiment)
        status['stage'] = 'judging'
        if not args.no_judge:
            await run_judge(SimpleNamespace(**dict(shared, model=args.judge_model or args.model),
                run=root / 'execution', output=root / 'judge', timeout=90))
        status['completed'] = True
    except Exception as exc:
        status['error_type'] = type(exc).__name__
        raise
    finally:
        (root / 'status.json').write_text(json.dumps(status, indent=2), encoding='utf-8')
        if (root / 'execution/results.json').exists():
            judge = root / 'judge/judgments.json'
            summary = analyze(root / 'execution', args.dataset, judge if judge.exists() else None)
            (root / 'metrics.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
            (root / 'report.md').write_text(render(summary), encoding='utf-8')
            export(json.loads((root / 'execution/results.json').read_text(encoding='utf-8')), root / 'official-format')
        else:
            (root / 'report.md').write_text('# BrowseComp-Plus 运行未开始\n\n'
                '模型预检或测试库初始化未通过，没有 Agent 成绩。查看 execution/preflight.json、'
                'execution/plan.json 和 execution/episodes/*/setup.json。不要计为答案错误。\n', encoding='utf-8')


def main():
    import logging
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--omp', default=shutil.which('omp') or 'omp')
    parser.add_argument('--model', default='hyper-op/GLM-5.3-Flash')
    parser.add_argument('--models-config', type=Path, default=Path.home() / '.omp/agent/models.yml')
    parser.add_argument('--judge-model', help='Independent judge model; default uses the solver model with a bias notice')
    parser.add_argument('--no-judge', action='store_true')
    parser.add_argument('--variants', nargs='+', choices=['closed_book', 'grep', 'search', 'hybrid'], default=['closed_book', 'grep', 'search', 'hybrid'])
    parser.add_argument('--timeout', type=int, default=180)
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--tasks',nargs='+',help='Frozen subset of task IDs; recorded in the plan')
    parser.add_argument('--plugin-definition', type=Path, default=Path('examples/plugins/simple-rag/definition.json'))
    parser.add_argument('--plugin-source', type=Path, default=Path('examples/plugins/simple-rag/package'))
    parser.add_argument('--trust-plugin', action='store_true')
    args = parser.parse_args()
    if args.timeout < 1 or args.repeats < 1 or len(set(args.variants)) != len(args.variants):
        parser.error('Positive timeout/repeats and unique variants required')
    asyncio.run(run(args))
    print(args.output / 'report.md')


if __name__ == '__main__':
    main()
