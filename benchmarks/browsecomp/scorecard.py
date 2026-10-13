"""Question-clustered summaries and paired knowledge-access gains; no task text."""
import random
import statistics

from benchmarks.agent.report import percentile


def clustered_interval(values, seed=42, samples=2000):
    """Resample questions, not correlated repeats; interval is descriptive."""
    means = [statistics.mean(values[q]) for q in sorted(values)]
    if not means:
        return {'mean':None, 'low':None, 'high':None, 'questions':0}
    if len(means) == 1:
        return {'mean':means[0], 'low':None, 'high':None, 'questions':1}
    rng = random.Random(seed)
    boot = [statistics.mean(rng.choices(means,k=len(means))) for _ in range(samples)]
    return {'mean':statistics.mean(means), 'low':percentile(boot,2.5),
            'high':percentile(boot,97.5), 'questions':len(means)}


def summarize(rows):
    by_question = {}
    for row in rows:
        by_question.setdefault(row['task_id'],[]).append(int(row['answer_correct'] is True))
    correct = sum(r['answer_correct'] is True for r in rows)
    incorrect = sum(r['answer_correct'] is False for r in rows)
    return {'episodes':len(rows), 'questions':len(by_question), 'correct':correct, 'incorrect':incorrect,
        'unjudged':len(rows)-correct-incorrect,
        'confirmed_correct_rate':clustered_interval(by_question),
        'judged_accuracy':correct/(correct+incorrect) if correct+incorrect else None,
        'judgment_coverage':(correct+incorrect)/len(rows) if rows else 0,
        'evidence_recall':statistics.mean(r['evidence_recall'] for r in rows) if rows else 0,
        'gold_recall':statistics.mean(r['gold_recall'] for r in rows) if rows else 0,
        'calls_mean':statistics.mean(r['agent_tool_attempts'] for r in rows) if rows else 0,
        'tool_success_rate':1-sum(r['server_errors'] for r in rows)/sum(r['server_calls'] for r in rows) if sum(r['server_calls'] for r in rows) else None,
        'seconds_p50':percentile([r['wall_seconds'] for r in rows],50),
        'seconds_p95':percentile([r['wall_seconds'] for r in rows],95),
        'tokens_total':sum(r.get('usage',{}).get('totalTokens',0) for r in rows),
        'execution_failures':sum(r['status'] in ('timeout','execution_failed') for r in rows)}


def paired_gain(treatment, control):
    def keyed(rows):
        pairs={(r['task_id'],r['repeat']):r for r in rows}
        if len(pairs)!=len(rows):raise ValueError('Duplicate paired episode')
        return pairs
    t,c=keyed(treatment),keyed(control)
    if set(t)!=set(c):raise ValueError('Control and treatment must have identical question/repeat matrices')
    differences={};wins=losses=unresolved=0
    for pair in sorted(t):
        a,b=t[pair]['answer_correct'],c[pair]['answer_correct']
        unresolved+=a is None or b is None
        wins+=a is True and b is False
        losses+=a is False and b is True
        differences.setdefault(pair[0],[]).append(int(a is True)-int(b is True))
    return {'confirmed_gain':clustered_interval(differences),'paired_episodes':len(t),
            'judged_wins':wins,'judged_losses':losses,'pairs_with_unjudged':unresolved,
            'interpretation':'Difference in confirmed correct / all planned completed episodes; unknown judgments are not asserted wrong.'}


def report(summaries):
    """Each named run was frozen by the campaign; only complete matrices compare."""
    if not summaries:raise ValueError('No campaign results')
    baseline=next(iter(summaries.values()))
    fields=('model','omp_version','dataset','timeout_seconds','source_sha256','system_prompt_sha256','judge')
    for summary in summaries.values():
        if any(summary[k]!=baseline[k] for k in fields):raise ValueError('Campaign inputs or judge differ')
    arms={}
    for name,summary in summaries.items():
        for variant in dict.fromkeys(r['variant'] for r in summary['episodes']):
            arms[f'{name}/{variant}']=[r for r in summary['episodes'] if r['variant']==variant]
    control=arms.get('control/closed_book')
    complete=all(not s['missing_episodes'] for s in summaries.values())
    statistics_by_arm={name:summarize(rows) for name,rows in arms.items()}
    gains={name:paired_gain(rows,control) for name,rows in arms.items() if name!='control/closed_book'} if control and complete else {}
    return {'scope':'reduced-corpus-internal-adaptation','official_leaderboard_comparable':False,
        'complete':complete,'judge':baseline['judge'],'model':baseline['model'],
        'dataset':baseline['dataset'],'tested_questions':len({r['task_id'] for rows in arms.values() for r in rows}),'arms':statistics_by_arm,'paired_vs_closed_book':gains,
        'run_hashes':{name:s['run_sha256'] for name,s in summaries.items()},
        'readiness':'provisional; same-family/model judging and sampled corpus require human review'}


def render(data):
    lines=['# BrowseComp-Plus 主基准对照报告','',
        f"模型：{data['model']}；本次题目：{data['tested_questions']}；文档：{data['dataset']['corpus_files']}；运行完整：{data['complete']}。",
        '原题和标准证据保持不变，当前使用缩小语料；不能与官方全量成绩比较。',
        '确认正确率 = 已判正确 / 全部执行；未判单列，不宣称其答案错误。95% 区间按题目重采样，先聚合同题重复，避免把三次重复当成三道独立题。', '',
        '| 配置 | 正确/错误/未判 | 确认正确率 [95% 区间] | 证据召回 | 平均调用 | 秒 P50/P95 | Token 总量 |',
        '|---|---|---|---|---:|---|---:|']
    def rate(value):return '—' if value is None else f'{value:.1%}'
    for name,s in data['arms'].items():
        ci=s['confirmed_correct_rate']
        recall='—' if name.endswith('/closed_book') else rate(s['evidence_recall'])
        lines.append(f"| {name} | {s['correct']}/{s['incorrect']}/{s['unjudged']} | {rate(ci['mean'])} [{rate(ci['low'])}, {rate(ci['high'])}] | {recall} | {s['calls_mean']:.2f} | {s['seconds_p50']:.2f}/{s['seconds_p95']:.2f} | {s['tokens_total']} |")
    lines+=['','## 相比无知识库的增益','',
        '无知识库组使用同一问题、模型和时间预算，禁用所有工具，只允许已有知识作答；系统提示会明确没有资料，而不会虚构一个可访问的知识库。该对照衡量整个接入条件的收益，不是纯索引算法收益。','',
        '| 配置 | 确认正确率差 [95% 区间] | 已判配对胜/负 | 含未判的配对 |','|---|---|---:|---:|']
    for name,g in data['paired_vs_closed_book'].items():
        ci=g['confirmed_gain'];lines.append(f"| {name} | {rate(ci['mean'])} [{rate(ci['low'])}, {rate(ci['high'])}] | {g['judged_wins']}/{g['judged_losses']} | {g['pairs_with_unjudged']} |")
    lines+=['',f"判分：{data['judge']}。同模型判分保留偏差提示；人工抽检由 review 子命令导出和回填，不覆盖机器判分。",
        '任务失败与工具错误不可等同；问答正确也不证明每句解释忠实。固定查询 Recall@K/nDCG@K 请查看单独检索报告。少量题的区间与排序仅作诊断。','']
    if data.get('retrieval'):
        lines+=['## 固定查询检索','',
            '所有插件共用预先冻结的 question-only 查询和 evidence 标签；每题一次 Search，最多 10 个片段，按首次出现顺序去重成文档。','',
            '| 插件 | K | Recall@K | nDCG@K | MRR@K | Precision@K |','|---|---:|---:|---:|---:|---:|']
        for name,result in data['retrieval'].items():
            for k,m in result['ks'].items():lines.append(f"| {name} | {k} | {m['recall']:.3f} | {m['ndcg']:.3f} | {m['mrr']:.3f} | {m['precision_at_k']:.3f} |")
        lines+=['','该分数不与多轮累计召回混用，也不等于官方文档级 Top-100/1000。','']
    return '\n'.join(lines)
