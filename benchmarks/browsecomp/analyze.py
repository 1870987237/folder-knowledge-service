"""Redacted task/search diagnostics and guarded comparisons of fixed-corpus runs."""
import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import statistics

from benchmarks.agent.report import percentile
from benchmarks.agent.tasks import load_dataset
from benchmarks.browsecomp.evaluate import returned_documents


def execution_status(row):
    if row.get('timed_out'):
        return 'timeout'
    if not row.get('valid_run') or row.get('returncode') != 0:
        return 'execution_failed'
    if not row['evaluation'].get('protocol_success'):
        return 'incomplete_answer'
    return 'completed'


def search_diagnostics(calls, known, evidence):
    seen, queries, rows = set(), Counter(), []
    for index, call in enumerate(calls, 1):
        found = returned_documents([call], known)
        fresh = found - seen
        if call.get('tool') == 'search':
            # Exact argument equality is intentionally conservative; no model-based inference.
            signature = json.dumps(call.get('arguments', {}), sort_keys=True, ensure_ascii=False)
            queries[signature] += 1
            rows.append({'tool_call_number': index, 'search_number': len(rows) + 1,
                         'error': bool(call.get('is_error')), 'duration_ms': call.get('duration_ms', 0),
                         'returned_documents': len(found), 'new_documents': len(fresh),
                         'new_evidence_documents': len(fresh & evidence),
                         'cumulative_evidence_documents': len((seen | found) & evidence),
                         'repeated_arguments': queries[signature] > 1})
        seen |= found
    return rows


def analyze(run, dataset, judgments=None):
    run = Path(run)
    raw = (run / 'results.json').read_bytes()
    data = json.loads(raw)
    known, tasks, metadata = load_dataset(dataset)
    if metadata != data['dataset']:
        raise ValueError('Analysis dataset does not match the run fingerprint/annotations')
    references = {t.id: t.reference for t in tasks}
    if any(not ref for ref in references.values()):
        raise ValueError('Expected a BrowseComp-Plus dataset')
    judge_by_episode = {}
    judge_metadata = None
    human_review = None
    if judgments:
        judged = json.loads(Path(judgments).read_text(encoding='utf-8'))
        if judged['run_sha256'] != hashlib.sha256(raw).hexdigest():
            raise ValueError('Judgments belong to a different run; refusing to merge scores')
        judge_by_episode = {r['episode']: r for r in judged['episodes']}
        if len(judge_by_episode) != len(judged['episodes']):
            raise ValueError('Duplicate judgments')
        judge_metadata = {k: judged.get(k) for k in ('judge_model', 'same_model_as_solver', 'prompt_sha256')}
        human_review = judged.get('human_review')
    rows, identifiers = [], set()
    for row in data['episodes']:
        if row['episode'] in identifiers:
            raise ValueError('Duplicate episodes')
        identifiers.add(row['episode'])
        calls = json.loads((run / 'episodes' / row['episode'] / 'tool-trace.json').read_text(encoding='utf-8'))
        ref = references[row['task_id']]
        steps = search_diagnostics(calls, known, set(ref['evidence_docids']))
        status = execution_status(row)
        judgment = judge_by_episode.get(row['episode'], {})
        correct = judgment.get('correct') if status in ('completed', 'incomplete_answer') else None
        if correct is not None and type(correct) is not bool:
            raise ValueError('Judge correct must be boolean or null')
        rows.append({'episode': row['episode'], 'task_id': row['task_id'], 'variant': row['variant'],
            'repeat': row['repeat'], 'status': status, 'answer_correct': correct,
            'judge_error': judgment.get('error'), 'answer_exact_match_proxy': row['evaluation']['answer_exact_match'],
            'evidence_recall': row['evaluation']['evidence_recall'], 'gold_recall': row['evaluation']['gold_recall'],
            'unseen_citation_count': len(row['evaluation'].get('unseen_citations', [])),
            'agent_tool_attempts': row['agent_tool_attempts'], 'agent_tool_errors': len(row.get('agent_tool_errors', [])),
            'server_calls': len(calls), 'server_errors': sum(bool(c.get('is_error')) for c in calls),
            'tools_used': dict(Counter(c['tool'] for c in calls)), 'search_steps': steps,
            'tool_sequence': [c['tool'] for c in calls],
            'search_calls': len(steps), 'search_no_new_documents': sum(not s['new_documents'] for s in steps),
            'search_no_new_evidence': sum(not s['new_evidence_documents'] for s in steps),
            'repeated_searches': sum(s['repeated_arguments'] for s in steps),
            'wall_seconds': row['wall_ms'] / 1000, 'index_build_seconds': row.get('index_build_ms', 0) / 1000,
            'tool_p50_ms': percentile([c['duration_ms'] for c in calls], 50),
            'tool_p95_ms': percentile([c['duration_ms'] for c in calls], 95),
            'response_bytes': sum(c.get('response_bytes', 0) for c in calls), 'usage': row.get('usage', {})})
    if set(judge_by_episode) - identifiers:
        raise ValueError('Unknown episode in judgments')
    plan = data.get('plan', {})
    summary = {'scope': 'reduced-corpus-internal-adaptation', 'official_leaderboard_comparable': False,
               'model': data['model'], 'omp_version': data.get('omp_version'),
               'dataset': metadata, 'source_sha256': data.get('source_sha256'),
               'system_prompt_sha256': hashlib.sha256(data.get('system_prompt', '').encode()).hexdigest(),
               'timeout_seconds': plan.get('timeout_seconds'),
               'plugin_sha256': data.get('plugin', {}).get('package_sha256'),
               'plugin_indexing': data.get('plugin', {}).get('definition', {}).get('plugin', {}).get('indexing'),
               'run_sha256': hashlib.sha256(raw).hexdigest(), 'judge': judge_metadata,'human_review':human_review,
               'planned_episodes': plan.get('planned_episodes'), 'executed_episodes': len(rows),
               'missing_episodes': max(0, plan.get('planned_episodes', len(rows)) - len(rows)),
               'episodes': rows}
    return summary


def render(summary):
    lines = ['# BrowseComp-Plus Agent 评测', '',
        '缩小语料的内部测试，不与官方榜单比较。执行失败、未判分和回答错误分别统计。', '',
        f"模型：{summary['model']}；OMP：{summary['omp_version']}；文档：{summary['dataset'].get('corpus_files')}；"
        f"计划执行：{summary['planned_episodes']}；已执行：{summary['executed_episodes']}；未执行：{summary['missing_episodes']}。",
        f"索引包 SHA-256：`{summary['plugin_sha256']}`。", '',
        '| 配置 | 执行数 | 执行失败/超时 | 答对/答错/未判 | 平均证据召回 | 工具调用 | 耗时 P50/P95（秒） |',
        '|---|---:|---:|---|---:|---:|---:|']
    rows = summary['episodes']
    for variant in dict.fromkeys(r['variant'] for r in rows):
        group = [r for r in rows if r['variant'] == variant]
        correct = sum(r['answer_correct'] is True for r in group)
        wrong = sum(r['answer_correct'] is False for r in group)
        failures = sum(r['status'] in ('execution_failed', 'timeout') for r in group)
        lines.append(f"| {variant} | {len(group)} | {failures} | {correct}/{wrong}/{len(group)-correct-wrong} | "
                     f"{statistics.mean(r['evidence_recall'] for r in group):.1%} | {sum(r['agent_tool_attempts'] for r in group)} | "
                     f"{percentile([r['wall_seconds'] for r in group], 50):.2f}/{percentile([r['wall_seconds'] for r in group], 95):.2f} |")
    lines += ['', '召回和耗时包含失败、超时样本；不隐藏失败。答案未判不等于错误；没有语义判分时不报告正确率。',
              '索引构建在任务计时之外；逐题指标另外记录构建耗时、服务端工具延迟和模型 Token。']
    if summary['judge']:
        lines += [f"内部判分模型：{summary['judge']['judge_model']}；与被测模型相同：{summary['judge']['same_model_as_solver']}。同模型判分存在偏差，需要抽检。"]
    if summary.get('human_review'):
        lines += [f"人工复核记录：{summary['human_review']}。原机器判分保留在独立文件中。"]
    lines += ['', '## 每项任务使用了什么工具', '',
              '| 任务 / 配置 / 重复 | 状态 | 工具调用统计 | 服务端工具成功/调用 | 答案 | 证据召回 | 秒 |',
              '|---|---|---|---:|---|---:|---:|']
    for row in rows:
        tools = '、'.join(f'{k}×{v}' for k, v in row['tools_used'].items()) or '无'
        answer = '未判分' if row['answer_correct'] is None else ('正确' if row['answer_correct'] else '错误')
        lines.append(f"| {row['task_id']} / {row['variant']} / {row['repeat']} | {row['status']} | {tools} | "
                     f"{row['server_calls']-row['server_errors']}/{row['server_calls']} | {answer} | "
                     f"{row['evidence_recall']:.1%} | {row['wall_seconds']:.2f} |")
    lines += ['', '## 每次 Search 带来的信息增量', '',
              '| 任务 / 配置 / 重复 | 第几次 Search | 全部工具中的序号 | 返回文档 | 新文档 | 新标准证据 | 完全重复参数 | 错误 |',
              '|---|---:|---:|---:|---:|---:|---|---|']
    for row in rows:
        for step in row['search_steps']:
            lines.append(f"| {row['task_id']} / {row['variant']} / {row['repeat']} | {step['search_number']} | "
                f"{step['tool_call_number']} | {step['returned_documents']} | {step['new_documents']} | "
                f"{step['new_evidence_documents']} | {step['repeated_arguments']} | {step['error']} |")
    lines += ['', '“新”相对于本任务之前所有 search/grep/read_file 已返回正文的文档，按文档去重。'
              '同一文档的其他片段可能有用，因此零新增不等于无用；标准证据以外的文档也可能帮助排除错误答案。',
              '这些数据用于定位重复检索、召回不足和证据确认行为，不能自动证明工具误导了 Agent。'
              '问题、答案、查询文本、正文及路径均不写入本汇总；原始轨迹留在本地 data/。', '']
    return '\n'.join(lines)


def comparison(summaries):
    if len(summaries) < 2:
        raise ValueError('Comparison needs at least two runs')
    baseline = summaries[0]
    keys = ('model', 'omp_version', 'dataset', 'system_prompt_sha256', 'timeout_seconds', 'source_sha256', 'judge')
    matrix = lambda s: sorted((r['task_id'], r['variant'], r['repeat']) for r in s['episodes'])
    if any(s['timeout_seconds'] is None or s['missing_episodes'] for s in summaries):
        raise ValueError('Comparison requires complete planned runs with recorded budgets')
    for candidate in summaries[1:]:
        mismatch = [k for k in keys if candidate[k] != baseline[k]]
        if matrix(candidate) != matrix(baseline):
            mismatch.append('task/variant/repeat matrix')
        if mismatch:
            raise ValueError('Runs are not comparable: ' + ', '.join(mismatch))
    return '\n\n---\n\n'.join(render(s) for s in summaries)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runs', nargs='+', type=Path)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--judgments', nargs='+', type=Path, help='One judgments.json per run, in the same order')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.judgments and len(args.judgments) != len(args.runs):
        parser.error('Provide one judgment file per run')
    summaries = [analyze(r, args.dataset, args.judgments[i] if args.judgments else None) for i, r in enumerate(args.runs)]
    content = comparison(summaries) if len(summaries) > 1 else render(summaries[0])
    if args.output.exists() and any(args.output.iterdir()):
        parser.error('Output must be new or empty')
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'report.md').write_text(content, encoding='utf-8')
    (args.output / 'metrics.json').write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding='utf-8')
    columns = ('run', 'episode', 'status', 'answer_correct', 'evidence_recall', 'gold_recall',
               'agent_tool_attempts', 'agent_tool_errors', 'server_calls', 'server_errors',
               'search_calls', 'repeated_searches', 'wall_seconds', 'index_build_seconds', 'response_bytes')
    with (args.output / 'tasks.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for number, summary in enumerate(summaries, 1):
            for row in summary['episodes']:
                writer.writerow({'run': number, **{key: row[key] for key in columns[1:]}})
    print(args.output)


if __name__ == '__main__':
    main()
