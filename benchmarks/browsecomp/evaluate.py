"""Evidence metrics, explicitly labelled answer proxy, and official-format export."""
import argparse
import json
from pathlib import Path
import re
import statistics
import unicodedata

from benchmarks.agent.evaluate import final_payload


def normalize(value):
    # Deliberately conservative: do not make substring/number matches count as correctness.
    return ' '.join(unicodedata.normalize('NFKC', str(value)).casefold().strip().rstrip('.。').split())


def path_id(path):
    match = re.fullmatch(r'documents/([0-9]+)\.md', str(path))
    return match[1] if match else None


def returned_documents(calls, known_paths):
    found = set()
    for call in calls:
        if call.get('is_error') or call.get('tool') not in ('read_file', 'search', 'grep'):
            continue
        value = call.get('result', {}).get('structuredContent')
        if value is None:
            try:
                value = json.loads('\n'.join(x.get('text', '') for x in call.get('result', {}).get('content', []) if x.get('type') == 'text'))
            except ValueError:
                continue
        if not isinstance(value, dict):
            continue
        if call['tool'] == 'read_file':
            items = [value] if value.get('lines') else []
        else:
            items = value.get('results' if call['tool'] == 'search' else 'matches', [])
            items = [item for item in items if isinstance(item, dict) and item.get('text')]
        for item in items:
            path = item.get('path')
            if path in known_paths and path_id(path):
                found.add(path_id(path))
    return found


def grade(task, before, after, final, calls):
    ref = task.reference
    payload = final_payload(final)
    fields = payload.get('facts', {}) if payload else {}
    prediction = fields.get('final_answer', '') if isinstance(fields, dict) else ''
    prediction = prediction if isinstance(prediction, str) else ''
    exact = bool(isinstance(prediction, str) and prediction.strip() and normalize(prediction) == normalize(ref['answer']))
    retrieved = returned_documents(calls, before)
    evidence, gold = set(ref['evidence_docids']), set(ref['gold_docids'])
    sources = payload.get('sources', []) if payload else []
    cited = {path_id(v) for v in sources if isinstance(v, str) and v in before} if isinstance(sources, list) else set()
    cited.discard(None)
    changed = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
    checks = {'structured_final': bool(payload and isinstance(payload.get('completed'), bool)),
              'completion_claim': bool(payload and payload.get('completed') is True),
              'no_unintended_changes': not changed, 'used_evidence_tool': bool(retrieved),
              'exact_answer_proxy': exact}
    outcome = exact and not changed and bool(retrieved)
    success = all(checks.values())
    return {'success': success, 'outcome_success': outcome,
        'protocol_success': checks['structured_final'] and checks['completion_claim'],
        'checks': checks, 'failed_checks': [k for k, v in checks.items() if not v],
        'changed_files': changed, 'unintended_changes': changed,
        'false_completion': False,  # An exact-match miss is NOT evidence of a false claim.
        'error_recovered': success and any(c.get('is_error') for c in calls),
        'answer': payload.get('answer', '') if payload else final,
        'claimed_completed': payload.get('completed') if payload else None,
        'benchmark': 'browsecomp-plus', 'query_id': ref['query_id'],
        'prediction': prediction, 'answer_exact_match': exact, 'answer_correct': None,
        'judge_status': 'not_run', 'retrieved_docids': sorted(retrieved),
        'evidence_recall': len(retrieved & evidence) / len(evidence),
        'gold_recall': len(retrieved & gold) / len(gold),
        'cited_docids': sorted(cited), 'cited_evidence_recall': len(cited & evidence) / len(evidence),
        'unseen_citations': sorted(cited - retrieved),
        'grading_note': 'Normalized exact match is a diagnostic proxy only. Semantic answer correctness requires a judge; not an official leaderboard score.'}


def report(data):
    rows = data['episodes']
    metadata = data.get('dataset', {})
    provenance = metadata.get('provenance', {})
    lines = ['# BrowseComp-Plus 平台接入测试', '',
        '**缩小语料的内部适配测试，不是官方全量成绩。**答案精确匹配仅用于排查链路；未做语义判分时，不能据此报告正式正确率。', '',
        f"模型：{data['model']}；问题数：{metadata.get('task_count')}；文档数：{metadata.get('corpus_files')}；范围：{provenance.get('scope', 'unspecified')}。",
        '原始问题不翻译、不添加答案；标准答案和证据标签不挂载。各配置使用相同完整文档子集、独立只读 Key 和会话。', '',
        '| 配置 | 执行数 | 有效运行 | 答案精确匹配代理 | 平均证据召回 | 工具调用数 | 任务中位耗时 |',
        '|---|---:|---:|---:|---:|---:|---:|']
    for variant in dict.fromkeys(r['variant'] for r in rows):
        group = [r for r in rows if r['variant'] == variant]
        valid = lambda r: r['valid_run'] and not r['timed_out'] and r['returncode'] == 0
        exact = sum(valid(r) and r['evaluation']['answer_exact_match'] for r in group)
        recall = statistics.mean(r['evaluation']['evidence_recall'] for r in group)
        lines.append(f"| {variant} | {len(group)} | {sum(valid(r) for r in group)} | {exact}/{len(group)} | {recall:.1%} | {sum(r['agent_tool_attempts'] for r in group)} | {statistics.median(r['wall_ms'] for r in group)/1000:.2f} 秒 |")
    lines += ['', '证据召回 = 实际通过 search / grep / read_file 返回过正文的标注文档数 ÷ 全部证据文档数。目录列出文件名不算召回；按文档去重，不代表已经读取整篇或利用全部证据。失败/超时仍计入执行数。', '',
              '| 任务 | 配置 | 调用 | 错误 | 证据召回 | 精确匹配代理 | 秒 |', '|---|---|---|---:|---:|---|---:|']
    for row in rows:
        ev = row['evaluation']
        tools = '、'.join(f'{k}×{v}' for k, v in row['tools_used'].items())
        answer_status = ('匹配' if ev['answer_exact_match'] else '未匹配，需语义判分') if row['valid_run'] and not row['timed_out'] and row['returncode'] == 0 else '执行失败，未判分'
        lines.append(f"| {row['task_id']} | {row['variant']} | {tools or '无'} | {row['tool_errors']} | {ev['evidence_recall']:.1%} | {answer_status} | {row['wall_ms']/1000:.2f} |")
    lines += ['', 'Search 为当前平台 SQLite FTS5 / BM25 插件；保持其原始工具和结果格式，不采用官方工具的 Top-5/512-token 格式。因此此报告用于平台内部配置对比。',
              '原始答复、标准答案、文档和轨迹只保存在被 Git 忽略的 data/ 中，不应公开上传。',
              '可导出官方 run 格式，再使用固定版本的上游 Qwen3-32B 判分；或使用独立 OpenAI 兼容模型进行明确标记的内部语义判分。', '']
    return '\n'.join(lines)


def export(data, destination):
    destination = Path(destination)
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Export directory must be new/empty')
    for row in data['episodes']:
        ev = row['evaluation']
        if ev.get('benchmark') != 'browsecomp-plus':
            raise ValueError('Not a BrowseComp-Plus run')
        folder = destination / f"{row['variant']}-repeat{row['repeat']}"
        folder.mkdir(parents=True, exist_ok=True)
        response = str(ev.get('answer', ''))
        if ev.get('prediction'):
            response += '\nFinal answer: ' + ev['prediction']
        response += '\n' + ' '.join(f'[{did}]' for did in ev['cited_docids'])
        complete = row['valid_run'] and not row['timed_out'] and row['returncode'] == 0 and ev['claimed_completed'] is True
        value = {'query_id': ev['query_id'], 'tool_call_counts': row['tools_used'],
                 'status': 'completed' if complete else 'incomplete', 'retrieved_docids': ev['retrieved_docids'],
                 'result': [{'type': 'output_text', 'output': response}],
                 'metadata': {'model': data['model'], 'scope': data['dataset'].get('provenance', {}).get('scope'),
                              'official_leaderboard_comparable': False}}
        (folder / (ev['query_id'] + '.json')).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('run', type=Path)
    p.add_argument('--export', type=Path, required=True, dest='destination')
    a = p.parse_args()
    data = json.loads((a.run / 'results.json').read_text(encoding='utf-8'))
    export(data, a.destination)
    print(a.destination)


if __name__ == '__main__':
    main()
