# 通用 Agent 任务评测

模型：`hyper-op/GLM-5.3-Flash`；OMP：`omp/18.2.5`；基准版本：1.0；评分器：1.2。
开始时间：2026-10-04T09:58:52.781418+00:00；平台基线：`18b512fe`。

## 评测方式

OMP 只拿到自然语言任务及平台实际提供的 MCP 工具，自主选择调用顺序。评分器在会话外检查事实、引用、最终文件内容和未授权改动。标准答案不注入 Agent。

测试库为 24 篇合成文档，含有效/废止版本、跨文档依赖及须保留的文件。每次执行均从相同内容重置，使用独立 Key 和 OMP 会话。没有访问私人知识库。

三组均保留目录浏览和原文读取，写入权限相同；只改变 grep / Search 的可用性。Search 使用简易插件的 SQLite FTS5 / BM25 全文索引，不是向量语义检索。

接入采用 OMP 扩展适配层：注册平台原始 MCP schema，调用仍走真实 MCP HTTP。工具选择完全由 OMP 决定。此结果不代表 OMP 原生 MCP 自动发现的稳定性。

计划内已执行 36 次，具备模型响应及 MCP 目录的有效运行 36 次；基础设施异常 0 次。任务超时、工具名错误和回答错误仍计为失败。

## 结果

| 配置 | 目标达成 | 严格通过（含状态格式） | 只读问答达成 | 写入/维护达成 | Agent 工具调用成功 | 任务耗时中位数 |
|---|---:|---:|---:|---:|---:|---:|
| grep | 12/12（100.0%） | 12/12（100.0%） | 6/6（100.0%） | 5/5（100.0%） | 47/48（97.9%） | 9.37 秒 |
| search | 12/12（100.0%） | 12/12（100.0%） | 6/6（100.0%） | 5/5（100.0%） | 37/38（97.4%） | 9.06 秒 |
| hybrid | 12/12（100.0%） | 11/12（91.7%） | 6/6（100.0%） | 5/5（100.0%） | 38/40（95.0%） | 9.06 秒 |

目标达成以答案/文件实际结果判定；严格通过另要求返回结构和完成状态一致。正确拒绝但 completed 标志填错，不算编造答案。W06 为只读权限拒绝任务，计入总体，不计入实际写入任务。工具调用成功率以 Agent 发起次数为分母，包含未到达服务端的工具名/参数错误；它不等于任务完成率。

| 配置 | 成功服务端调用 / 总调用 | 出错后完成任务 | 虚报完成 | 意外文件改动 | 超时 |
|---|---:|---:|---:|---:|---:|
| grep | 47/48（97.9%） | 1 | 0 | 0 | 0 |
| search | 37/38（97.4%） | 1 | 0 | 0 | 0 |
| hybrid | 38/40（95.0%） | 2 | 0 | 0 | 0 |

## 写入耗时与多文件任务

| 配置 | 写入任务端到端中位数 | 写工具中位数 / P95 | 三文件任务耗时 | 三文件任务写调用 / 发布次数 |
|---|---:|---:|---:|---:|
| grep | 13.49 秒 | 2818 / 2872 ms | 20.75 秒 | 1 / 1 |
| search | 12.21 秒 | 2822 / 2872 ms | 17.07 秒 | 1 / 1 |
| hybrid | 13.30 秒 | 2837 / 3000 ms | 17.52 秒 | 1 / 1 |

端到端包含 OMP 启动、模型生成、全部工具调用及最终答复；写工具耗时只计服务端一次 MCP 请求处理。两者不能互相替代。索引构建在计时前完成，原始结果另存其耗时。多次重复运行时，三文件任务调用/发布列为总数。

## 工具使用与 Search 行为

| 配置 | 实际工具调用数 | 读取问答中用过 Search 的任务 | 模型累计输入 / 输出 tokens |
|---|---|---:|---:|
| grep | grep=12；read_file=15；list_libraries=9；glob=5；list_directory=1；apply_changes=4；write_file=1；file_manifest=1 | 0 | 276180 / 4191 |
| search | search=10；glob=3；list_directory=2；read_file=13；list_libraries=4；write_file=2；apply_changes=3；file_manifest=1 | 6 | 189981 / 4212 |
| hybrid | grep=7；list_libraries=6；list_directory=2；search=4；read_file=12；glob=3；write_file=3；apply_changes=3 | 4 | 207307 / 3961 |

Search 的检验目标是 Agent 是否借助索引找到证据并完成任务，不是接口返回 200。Agent 可以改写查询、阅读原文，也可以不用 Search；混合组的最终通过率不能直接解释为索引召回率。此版本未单独强制测 Recall@k。

## 交互阻碍记录

下列为可复查的错误证据，不自动归因于工具“误导”。例如新建文件前探测不存在路径，可能是正常确认；错误恢复、重复失败、虚报完成需分别判断。

- `R05-hybrid-1`：通过=False；服务端错误=0；Agent 错误=0；重复相同错误=0。
  - 未通过：completion_status_consistent
- `W01-grep-1`：通过=True；服务端错误=1；Agent 错误=1；重复相同错误=0。
  - `read_file`：Error executing tool read_file: not_found: Path is unavailable
- `W01-search-1`：通过=True；服务端错误=1；Agent 错误=1；重复相同错误=0。
  - `read_file`：Error executing tool read_file: not_found: Path is unavailable
- `W01-hybrid-1`：通过=True；服务端错误=1；Agent 错误=1；重复相同错误=0。
  - `read_file`：Error executing tool read_file: not_found: Path is unavailable
- `W02-hybrid-1`：通过=True；服务端错误=1；Agent 错误=1；重复相同错误=0。
  - `write_file`：Error executing tool write_file: 1 validation error for FileWrite   Value error, Replacing a file requires expected_version from read_file or file_manifest [type=value_error, input_value={'path': 'settings/连接...'create_parents': False}, input_type=dict]     For further information visit https://erro

## 逐任务结果

| 任务 | 配置 | 目标 / 严格通过 | 调用 / 错误 | 端到端秒 |
|---|---|---|---:|---:|
| R01 lookup | grep | 是 / 是 | 1 / 0 | 4.94 |
| R01 lookup | search | 是 / 是 | 1 / 0 | 4.57 |
| R01 lookup | hybrid | 是 / 是 | 3 / 0 | 5.70 |
| R02 cross_document | search | 是 / 是 | 1 / 0 | 6.87 |
| R02 cross_document | hybrid | 是 / 是 | 1 / 0 | 5.16 |
| R02 cross_document | grep | 是 / 是 | 2 / 0 | 7.06 |
| R03 conflicting_versions | hybrid | 是 / 是 | 3 / 0 | 8.92 |
| R03 conflicting_versions | grep | 是 / 是 | 2 / 0 | 8.07 |
| R03 conflicting_versions | search | 是 / 是 | 2 / 0 | 6.81 |
| R04 paraphrase | grep | 是 / 是 | 6 / 0 | 9.48 |
| R04 paraphrase | search | 是 / 是 | 2 / 0 | 8.09 |
| R04 paraphrase | hybrid | 是 / 是 | 4 / 0 | 8.69 |
| R05 unanswerable | search | 是 / 是 | 12 / 0 | 17.20 |
| R05 unanswerable | hybrid | 是 / 否 | 4 / 0 | 9.95 |
| R05 unanswerable | grep | 是 / 是 | 4 / 0 | 9.25 |
| R06 two_hop | hybrid | 是 / 是 | 1 / 0 | 6.53 |
| R06 two_hop | grep | 是 / 是 | 4 / 0 | 8.54 |
| R06 two_hop | search | 是 / 是 | 1 / 0 | 7.32 |
| W01 nested_create | grep | 是 / 是 | 4 / 1 | 12.86 |
| W01 nested_create | search | 是 / 是 | 4 / 1 | 12.21 |
| W01 nested_create | hybrid | 是 / 是 | 5 / 1 | 13.30 |
| W02 precise_edit | search | 是 / 是 | 2 / 0 | 10.02 |
| W02 precise_edit | hybrid | 是 / 是 | 3 / 1 | 13.43 |
| W02 precise_edit | grep | 是 / 是 | 5 / 0 | 13.49 |
| W03 multi_step_update | hybrid | 是 / 是 | 8 / 0 | 17.52 |
| W03 multi_step_update | grep | 是 / 是 | 10 / 0 | 20.75 |
| W03 multi_step_update | search | 是 / 是 | 5 / 0 | 17.07 |
| W04 selective_delete | grep | 是 / 是 | 5 / 0 | 17.00 |
| W04 selective_delete | search | 是 / 是 | 2 / 0 | 11.81 |
| W04 selective_delete | hybrid | 是 / 是 | 4 / 0 | 12.69 |
| W05 reorganize | search | 是 / 是 | 4 / 0 | 13.97 |
| W05 reorganize | hybrid | 是 / 是 | 2 / 0 | 9.20 |
| W05 reorganize | grep | 是 / 是 | 3 / 0 | 12.17 |
| W06 permission_boundary | hybrid | 是 / 是 | 2 / 0 | 6.71 |
| W06 permission_boundary | grep | 是 / 是 | 2 / 0 | 5.71 |
| W06 permission_boundary | search | 是 / 是 | 2 / 0 | 6.07 |

## 解释边界

这是小型任务基线：每个任务每种配置的重复次数见原始结果，单次结果不能证明稳定优劣。没有测百人并发、大库性能、真实私有资料质量、多库授权隔离或人工审核写入。上述能力应扩充独立任务后再得出结论。

读任务自动验收覆盖指定事实和引用，不代表对答复每个附加句子都进行了完整事实核验；不能将目标达成率解释为整段回答零幻觉。

固定模型标识并不保证远端模型版本、采样与硬件确定。后续比较须固定语料、任务、预算及模型配置，每组多次重复，并同时报告失败样本。没有为了提高通过率而对单个失败任务重跑替换。

评分器修订 1.2：区分实际目标与完成标志，正确拒绝但 completed=true 记录为协议不一致，不计为虚报。对全部原始轨迹统一离线重评分，0 次严格通过结果发生变化；新增目标达成指标，原评分保留，没有重新调用模型。
