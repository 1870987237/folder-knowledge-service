"""Aggregate autonomous-agent episodes without hiding failures or retries."""
import argparse
from collections import Counter
import json
from pathlib import Path


def percentile(values, percent):
    if not values:
        return 0
    values = sorted(values)
    pos = (len(values) - 1) * percent / 100
    low = int(pos)
    return values[low] + (values[min(low + 1, len(values) - 1)] - values[low]) * (pos - low)


def ratio(n, d):
    return f'{n}/{d}（{100*n/d:.1f}%）' if d else '—'


def error_text(call):
    return ' '.join(x.get('text', '') for x in call.get('result', {}).get('content', []) if x.get('type') == 'text')


def outcome(row):
    return row['evaluation'].get('outcome_success', row['evaluation']['success'])


def render(data, traces):
    rows = data['episodes']
    valid = [r for r in rows if r['valid_run']]
    lines = ['# 通用 Agent 任务评测', '',
             f"模型：`{data['model']}`；OMP：`{data['omp_version']}`；基准版本：{data['benchmark_version']}；评分器：{data.get('grader_version', '1.0')}。",
             f"开始时间：{data['started_at']}；平台基线：`{data['environment']['commit'][:8]}`。", '',
             '## 评测方式', '',
             'OMP 只拿到自然语言任务及平台实际提供的 MCP 工具，自主选择调用顺序。评分器在会话外检查事实、引用、最终文件内容和未授权改动。标准答案不注入 Agent。', '',
             '测试库为 24 篇合成文档，含有效/废止版本、跨文档依赖及须保留的文件。每次执行均从相同内容重置，使用独立 Key 和 OMP 会话。没有访问私人知识库。', '',
             '三组均保留目录浏览和原文读取，写入权限相同；只改变 grep / Search 的可用性。Search 使用简易插件的 SQLite FTS5 / BM25 全文索引，不是向量语义检索。', '',
             '接入采用 OMP 扩展适配层：注册平台原始 MCP schema，调用仍走真实 MCP HTTP。工具选择完全由 OMP 决定。此结果不代表 OMP 原生 MCP 自动发现的稳定性。', '',
             f"计划内已执行 {len(rows)} 次，具备模型响应及 MCP 目录的有效运行 {len(valid)} 次；基础设施异常 {len(rows)-len(valid)} 次。任务超时、工具名错误和回答错误仍计为失败。", '',
             '## 结果', '',
             '| 配置 | 目标达成 | 严格通过（含状态格式） | 只读问答达成 | 写入/维护达成 | Agent 工具调用成功 | 任务耗时中位数 |',
             '|---|---:|---:|---:|---:|---:|---:|']
    for variant in dict.fromkeys(r['variant'] for r in rows):
        group = [r for r in valid if r['variant'] == variant]
        reads = [r for r in group if r['task_id'].startswith('R')]
        writes = [r for r in group if r['task_id'].startswith('W') and r['task_id'] != 'W06']
        attempts = sum(r['agent_tool_attempts'] for r in group)
        ok_calls = sum(max(0, r['agent_tool_results']-len(r['agent_tool_errors'])) for r in group)
        lines.append(f"| {variant} | {ratio(sum(outcome(r) for r in group), len(group))} | {ratio(sum(r['evaluation']['success'] for r in group), len(group))} | "
                     f"{ratio(sum(outcome(r) for r in reads), len(reads))} | "
                     f"{ratio(sum(outcome(r) for r in writes), len(writes))} | "
                     f"{ratio(ok_calls, attempts)} | {percentile([r['wall_ms']/1000 for r in group], 50):.2f} 秒 |")
    lines += ['', '目标达成以答案/文件实际结果判定；严格通过另要求返回结构和完成状态一致。正确拒绝但 completed 标志填错，不算编造答案。W06 为只读权限拒绝任务，计入总体，不计入实际写入任务。工具调用成功率以 Agent 发起次数为分母，包含未到达服务端的工具名/参数错误；它不等于任务完成率。', '',
              '| 配置 | 成功服务端调用 / 总调用 | 出错后完成任务 | 虚报完成 | 意外文件改动 | 超时 |',
              '|---|---:|---:|---:|---:|---:|']
    for variant in dict.fromkeys(r['variant'] for r in rows):
        group = [r for r in valid if r['variant'] == variant]
        total = sum(r['tool_calls'] for r in group)
        lines.append(f"| {variant} | {ratio(total-sum(r['tool_errors'] for r in group), total)} | "
                     f"{sum(r['evaluation']['error_recovered'] for r in group)} | "
                     f"{sum(r['evaluation']['false_completion'] for r in group)} | "
                     f"{sum(bool(r['evaluation']['unintended_changes']) for r in group)} | {sum(r['timed_out'] for r in group)} |")
    lines += ['', '## 写入耗时与多文件任务', '',
              '| 配置 | 写入任务端到端中位数 | 写工具中位数 / P95 | 三文件任务耗时 | 三文件任务写调用 / 发布次数 |',
              '|---|---:|---:|---:|---:|']
    for variant in dict.fromkeys(r['variant'] for r in rows):
        group = [r for r in valid if r['variant'] == variant]
        writes = [r for r in group if r['task_id'].startswith('W') and r['task_id'] != 'W06']
        write_calls = [c for r in writes for c in traces.get(r['episode'], []) if c['tool'] in ('write_file', 'apply_changes')]
        multi = [r for r in group if r['task_id'] == 'W03']
        multi_calls = sum(sum(c['tool'] in ('write_file', 'apply_changes') for c in traces.get(r['episode'], [])) for r in multi)
        lines.append(f"| {variant} | {percentile([r['wall_ms']/1000 for r in writes], 50):.2f} 秒 | "
                     f"{percentile([c['duration_ms'] for c in write_calls], 50):.0f} / {percentile([c['duration_ms'] for c in write_calls], 95):.0f} ms | "
                     f"{percentile([r['wall_ms']/1000 for r in multi], 50):.2f} 秒 | {multi_calls} / {sum(r['publications'] for r in multi)} |")
    lines += ['', '端到端包含 OMP 启动、模型生成、全部工具调用及最终答复；写工具耗时只计服务端一次 MCP 请求处理。两者不能互相替代。索引构建在计时前完成，原始结果另存其耗时。多次重复运行时，三文件任务调用/发布列为总数。', '',
              '## 工具使用与 Search 行为', '',
              '| 配置 | 实际工具调用数 | 读取问答中用过 Search 的任务 | 模型累计输入 / 输出 tokens |',
              '|---|---|---:|---:|']
    for variant in dict.fromkeys(r['variant'] for r in rows):
        group = [r for r in valid if r['variant'] == variant]
        names = Counter()
        for r in group:
            names.update(r['tools_used'])
        search_tasks = sum(r['task_id'].startswith('R') and r['tools_used'].get('search', 0) > 0 for r in group)
        lines.append(f"| {variant} | {'；'.join(f'{k}={v}' for k,v in names.items())} | {search_tasks} | "
                     f"{sum(r['usage'].get('input',0) for r in group)} / {sum(r['usage'].get('output',0) for r in group)} |")
    lines += ['', 'Search 的检验目标是 Agent 是否借助索引找到证据并完成任务，不是接口返回 200。Agent 可以改写查询、阅读原文，也可以不用 Search；混合组的最终通过率不能直接解释为索引召回率。此版本未单独强制测 Recall@k。', '',
              '## 交互阻碍记录', '',
              '下列为可复查的错误证据，不自动归因于工具“误导”。例如新建文件前探测不存在路径，可能是正常确认；错误恢复、重复失败、虚报完成需分别判断。', '']
    for r in rows:
        errors = [c for c in traces.get(r['episode'], []) if c['is_error']]
        if errors or r['agent_tool_errors'] or not r['evaluation']['success']:
            lines.append(f"- `{r['episode']}`：通过={r['evaluation']['success']}；服务端错误={r['tool_errors']}；"
                         f"Agent 错误={len(r['agent_tool_errors'])}；重复相同错误={r['repeated_identical_errors']}。")
            for c in errors:
                lines.append(f"  - `{c['tool']}`：{error_text(c)[:300].replace(chr(10), ' ')}")
            if r['evaluation']['failed_checks']:
                lines.append('  - 未通过：' + '、'.join(r['evaluation']['failed_checks']))
    lines += ['', '## 逐任务结果', '', '| 任务 | 配置 | 目标 / 严格通过 | 调用 / 错误 | 端到端秒 |', '|---|---|---|---:|---:|']
    for r in rows:
        lines.append(f"| {r['task_id']} {r['kind']} | {r['variant']} | {'是' if outcome(r) else '否'} / {'是' if r['evaluation']['success'] else '否'} | "
                     f"{r['agent_tool_attempts']} / {len(r['agent_tool_errors'])} | {r['wall_ms']/1000:.2f} |")
    lines += ['', '## 解释边界', '',
              '这是小型任务基线：每个任务每种配置的重复次数见原始结果，单次结果不能证明稳定优劣。没有测百人并发、大库性能、真实私有资料质量、多库授权隔离或人工审核写入。上述能力应扩充独立任务后再得出结论。', '',
              '读任务自动验收覆盖指定事实和引用，不代表对答复每个附加句子都进行了完整事实核验；不能将目标达成率解释为整段回答零幻觉。', '',
              '固定模型标识并不保证远端模型版本、采样与硬件确定。后续比较须固定语料、任务、预算及模型配置，每组多次重复，并同时报告失败样本。没有为了提高通过率而对单个失败任务重跑替换。', '']
    if data.get('grading_note'):
        changed = sum(r['evaluation']['success'] != r['original_evaluation']['success'] for r in rows)
        lines += [f"评分器修订 {data['grader_version']}：区分实际目标与完成标志，正确拒绝但 completed=true 记录为协议不一致，不计为虚报。对全部原始轨迹统一离线重评分，{changed} 次严格通过结果发生变化；新增目标达成指标，原评分保留，没有重新调用模型。", '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--results', default='results.json')
    args = parser.parse_args()
    data = json.loads((args.run / args.results).read_text(encoding='utf-8'))
    traces = {r['episode']: json.loads((args.run / 'episodes' / r['episode'] / 'tool-trace.json').read_text(encoding='utf-8'))
              for r in data['episodes']}
    destination = args.output or args.run / 'report.md'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render(data, traces), encoding='utf-8')
    print(destination)


if __name__ == '__main__':
    main()
