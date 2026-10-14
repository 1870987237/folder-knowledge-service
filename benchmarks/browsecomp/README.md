# BrowseComp-Plus 接入

让现有 OMP + GLM 通过 Folder KB 的真实 MCP 工具完成公开基准问题，对比 grep、Search 和混合配置。复用现有 Agent runner，不改变工具 schema 或替 Agent 规划调用。

支持两种数据规模：`full_prepare/full_run` 使用 **全部 830 题、100,195 篇文档**；`prepare/campaign` 保留缩小语料的快速回归。两者结果不可直接混合。原问题、文档和答案不改写；判分仍按用户选择使用 Flash，不能直接当作采用上游 Qwen3-32B 的官方榜单成绩。

## 全量入口

```powershell
# 下载并校验官方 6 个问题分片和 7 个语料分片，导入全部文档；可从下载/导入中断处继续
python -m benchmarks.browsecomp.full_prepare

# 只构建共享的大语料索引，不调用模型
python -m benchmarks.browsecomp.full_run --output data/bcp-full-search --prepare-only

# 原题文档级检索，分别输出 evidence/gold Recall@5/100/1000 和 nDCG@10；无 LLM 调用
python -m benchmarks.browsecomp.full_retrieval --run data/bcp-full-search --output data/bcp-full-retrieval

# 全部 830 题，Search 配置每题一次，每题最多 300 秒；Flash 答题和判分
python -m benchmarks.browsecomp.full_run --output data/bcp-full-search --resume
```

完整数据保存到 `data/browsecomp-plus-full/`，下载缓存为 `data/browsecomp-full-downloads/`。约 4.54 GB 压缩分片，另需语料、构建输入和索引空间。`manifest.json` 固定源版本、分片 SHA、全部题目/答案指纹、语料计数与逐文档指纹；不抽负例、不排除开发题、不截断长文档。标准答案只供判分器使用，Agent Key 仅挂载 `knowledge/`。

`full_run` 的只读语料和磁盘索引共用，不逐题复制或重建。每题使用新 OMP 会话、新 Key，保留真实工具轨迹、错误、Token 和判分；源码或数据改变后拒绝混入同一轮。`--resume` 校验已完成记录并跳过；已开始但崩溃的题记为中断失败，不暗中重新作答。运行中可查看 `status.json`、`report.md`、`metrics.json`，结束后另有 `official-format/`。服务状态和索引保存在 `service.json` 指向的短路径中，结束后保留用于复核与续跑。

全量检索使用独立 `sqlite-corpus` 插件，其协议、产物和算法见 [插件说明](../../examples/plugins/sqlite-corpus/package/README.md)。它是文档 BM25 基线，不等同于旧 Markdown 分块基线或上游 Pyserini 实现。平台继续提供相同 MCP 权限和工具接口；830 题中没有预设调用顺序。

`full_retrieval` 直接调用该插件的文档排序入口，原题不经 LLM 改写，可返回 Top-1000；MCP Search 保持最多 10 条引用的上下文预算。这两层分别回答“索引能否召回”和“Agent 能否利用工具完成任务”，不会把片段 Top-10 冒充文档 Top-1000。`run.trec` 可交给官方 `trec_eval` 再核验。

这是**官方全量数据 + 本平台工具 + OMP/Flash**的评测配置，仍需披露同模型判分偏差、工具预算和插件差异。公开原题可能已进入模型训练，也包含先前使用过的开发题；全量不等于模型未见过的数据。不得将题目、答案和原始轨迹推送到远端。

## 缩小语料回归：配置、对照、检索和人工复核

主入口是 `campaign.py`。先准备冻结数据，默认只生成计划；加 `--execute` 才开始调用模型。`configs/main.json` 配置模型、判分模型、重复次数、预算及插件列表，不包含密钥。默认 Flash 答题、Flash 判分，报告明确标记同模型偏差。

```powershell
# 从本地固定分片随机抽题，排除已用于开发的旧任务；保留全部标准证据
python -m benchmarks.browsecomp.prepare --source data/browsecomp-upstream/test-00000-of-00006.parquet `
  --output data/bcp-holdout --selection random --questions 50 --negatives-per-question 3 `
  --seed 20261013 --exclude-dataset data/baseline-20261012/dataset-bcp

# 只冻结计划和源码快照，不调用模型
python -m benchmarks.browsecomp.campaign --dataset data/bcp-holdout --output data/bcp-main

# 在同样源码和配置下执行已冻结计划
python -m benchmarks.browsecomp.campaign --dataset data/bcp-holdout --output data/bcp-main --resume --execute

# 小规模接入验证；该配置额外比较内置 BM25 插件
python -m benchmarks.browsecomp.campaign --config benchmarks/browsecomp/configs/smoke.json `
  --dataset data/browsecomp-plus-smoke --output data/bcp-campaign-smoke --execute
```

每轮主基准顺序执行：

1. 只把原问题交给一个无工具模型会话，生成一条 1–200 字符查询。它看不到语料、标准答案或检索结果。最多三次仅针对 JSON 格式/长度的尝试，全部留档；仍无有效查询就停止，不截断、不按检索成绩挑选查询。工具越界或进程失败直接停止。
2. 冻结查询 CSV 和 evidence qrels，交给所有插件各执行一次检索，比较 Recall@K、nDCG@K、MRR、Precision。
3. 无知识库组只凭已有知识答原题，无任何工具、MCP instructions 或正文。记录真实模型请求中的工具目录，出现额外工具即视为无效运行。
4. 各插件按配置运行 grep / search / hybrid。问题、语料、模型和时间预算相同；每题轮换工具配置次序，保留重复次数。插件之间按配置顺序分批执行，因此延迟差异仍可能受远端负载影响。
5. 新会话语义判分，合并报告；按问题和重复编号与无知识库组配对，统计确认正确率增益。独立固定查询分数单独呈现。

50 题 × 4 组 × 3 次 = **600 次 Agent 任务**，最多另有 600 次判分及 50 条查询准备（格式修复算入最多 150 次查询准备调用）。这个数字是计划工作量，不表示已跑完。源码、数据、插件及任务矩阵改变后必须另建输出。可向配置中的 `plugins` 添加插件；控制组只执行一次，不因插件数量增加重复执行。比较同一工具配置下的插件效果时，确保各插件都包含该配置。

`--resume` 跳过完成且产物哈希一致的阶段，不覆盖已经中断的半个阶段。部分执行仍留在原目录，不能选择性补跑答错题冒充同一轮。长实验中途失败后应先分析部分结果，再决定新开完整运行；目前尚不支持逐题断点续跑。运行前后都会校验评测与平台源码，避免中途修改混入一轮数据。

主报告的确认正确率使用所有已执行样本为分母，未判分单列。完整运行才能做配对增益比较。95% bootstrap 区间先聚合同题的多次重复，再按题目重采样；不把重复三次当成三道独立题。小样本或全部同分时区间可能退化，不构成稳定优劣或统计显著性的证明。

### 人工抽检

可导出所有已执行但机器判错/未判的有效答复，并按组随机抽取部分机器判对答复。失败进程不人工补判成成功答题。CSV 隐藏配置名、自动判分及工具次数，保存问题、参考答案、候选答复和空的人工判定栏；答案包仅保留本地。

```powershell
python -m benchmarks.browsecomp.review export --run data/bcp-main/program-bm25/execution `
  --dataset data/bcp-holdout --judgments data/bcp-main/program-bm25/judge/judgments.json `
  --output data/bcp-review --sample 10

# 人工填写 review.csv 中 decision（correct/incorrect/unjudged）、reviewer、reason
python -m benchmarks.browsecomp.review apply --queue data/bcp-review `
  --decisions data/bcp-review/review.csv --output data/bcp-reviewed

python -m benchmarks.browsecomp.analyze data/bcp-main/program-bm25/execution `
  --dataset data/bcp-holdout --judgments data/bcp-reviewed/judgments.json --output data/bcp-reviewed-report
```

导入校验运行哈希、行 ID 及问题/答案内容，要求明确审核人和理由。机器原分数不覆盖，人工修改单独留档；没有人填写不能宣称已经人工抽检。抽样复核不能替代全量独立判分。

## 运行

在仓库根目录执行；沿用本机 OMP 的模型配置，不在命令中传密钥。

```powershell
pip install -e ".[test,benchmark]"

# 下载所需官方分片并导入。首次单个分片约 400–485 MiB，会缓存。
python -m benchmarks.browsecomp.prepare --output data/bcp-suite `
  --questions 3 --negatives-per-question 20 --seed 42

# 也可从已下载的原始、尚未解混淆的官方 parquet 导入
python -m benchmarks.browsecomp.prepare --source path/to/test-00000-of-00006.parquet `
  --output data/bcp-local --questions 2 --negatives-per-question 20

# 推荐：一次命令完成模型预检、Agent 执行、独立判分、报告和上游格式导出
python -m benchmarks.browsecomp.run --dataset data/bcp-local `
  --output data/bcp-experiment --timeout 180 --repeats 1

# 比较独立程序插件：自然语言问题、模型、预算及工具权限保持一致
# --trust-plugin 仅对本次测试进程批准该包，使用前审阅插件源码
python -m benchmarks.browsecomp.run --dataset data/bcp-local `
  --output data/bcp-program --timeout 180 --repeats 1 `
  --plugin-definition examples/plugins/simple-rag-python/definition.json `
  --plugin-source examples/plugins/simple-rag-python/package --trust-plugin
```

一键运行默认同一模型在全新、无工具的会话中判分，报告标出同模型偏差。
可以用 `--judge-model provider/model` 指定已配置的独立模型，或用 `--no-judge` 暂缓判分。
2 题仅验证链路；评估稳定性应准备更大的固定子集，并对所有配置统一增加重复次数，不能挑成功任务汇总。

也可以分步运行，便于保留执行结果后调整判分或报告：

```powershell

# 任务编号来自生成的 tasks.csv；省略 --tasks 执行全部导入的问题
python -m benchmarks.agent.runner --dataset data/bcp-suite `
  --output data/bcp-run --variants grep search hybrid --timeout 180

python -m benchmarks.agent.report data/bcp-run

# 独立、无工具的判分会话；默认沿用已配置的 GLM，可指定其他判分模型
python -m benchmarks.browsecomp.judge data/bcp-run --dataset data/bcp-suite `
  --output data/bcp-judge --model hyper-op/GLM-5.3-Flash

# 导出上游支持的 run 格式，每种配置、每次重复分开
python -m benchmarks.browsecomp.evaluate data/bcp-run --export data/bcp-official-format

# 合并语义判分与调用轨迹，输出 report.md、metrics.json 和逐任务 tasks.csv
python -m benchmarks.browsecomp.analyze data/bcp-run --dataset data/bcp-suite `
  --judgments data/bcp-judge/judgments.json --output data/bcp-analysis

# 两个插件的对比报告：数据、题目、重复次数、模型、预算、提示词和 runner 版本须一致
python -m benchmarks.browsecomp.analyze data/bcp-experiment/execution data/bcp-program/execution `
  --dataset data/bcp-local `
  --judgments data/bcp-experiment/judge/judgments.json data/bcp-program/judge/judgments.json `
  --output data/bcp-comparison
```

每个输出目录必须新建或为空。运行 `--repeats 3` 可观察稳定性；不要挑选性重跑失败题替换原结果。

预检失败不执行题目；初始化失败保存 `setup.json`，不伪造答案成绩。程序索引的临时服务使用系统短路径，避免 Windows 深层报告目录导致索引创建失败；停止服务后清理临时库，原始轨迹留在指定输出目录。

## 文件组织

```text
data/bcp-suite/
  knowledge/documents/<原始 docid>.md   # 完整原文，仅此文件夹挂载
  tasks.csv                            # 原始问题、任务编号、readonly
  expectations.json                    # 答案和 evidence/gold 标签，不传给 Agent
  manifest.json                        # 来源版本、分片 SHA、采样方式、语料指纹

data/bcp-run/
  plan.json                            # 计划题目、配置、重复次数与预算
  preflight.json                       # 模型连通性分类，不含密钥/原始诊断
  results.json                         # 运行环境、数据指纹、逐题结果
  report.md                            # 调用、耗时和证据召回
  episodes/...                         # MCP 请求响应、OMP 事件和最终文件

data/bcp-judge/
  judgments.json                       # 独立判分结果，不覆盖原执行记录
  report.md                            # 正确、错误、未判定分别汇总

data/bcp-experiment/                    # 一键运行的输出
  execution/                           # 等同 bcp-run
  judge/                               # 等同 bcp-judge
  status.json                          # 执行阶段及工作流是否完成，不代表答题全对
  report.md                            # 合并后的任务与 Search 信息增量报告
  metrics.json                         # 不含题目、答案或查询正文的诊断指标
  official-format/                     # 上游格式；包含 Agent 答复，仅供本地评测
```

沿用现有 `benchmarks/datasets/basic/` 作为快速读写回归；BrowseComp-Plus 单独测试检索，不混合统计两种任务。

## 指标与验收

- **证据召回**：经 search / grep / read_file 实际返回正文的文档 ID，与官方 evidence/gold 标签比较。目录中出现文件名不算召回，同文档多个片段只计一次。这是整个任务的文档召回，不是单次 Search 的 Recall@k，也不代表 Agent 已理解全部证据。
- **精确匹配代理**：比较 `facts.final_answer` 和参考答案，统一大小写与空白。仅作排查，未匹配不能直接解释为语义回答错误。`answer_correct` 在语义判分前保持 `null`。
- **内部语义判分**：无工具的新会话比较原问题、参考答案与候选答案，不读取其他任务轨迹。模型失败或格式错误记为“未判定”。若与被测 Agent 使用同一个模型，报告明确标注；需考虑同模型偏差，不能等同于上游判分。
- **成本与可靠性**：保留工具调用分布、错误、耗时、模型用量、超时和意外文件变化。所有配置使用同一语料；不把超时从总体执行数中移除。
- **逐次 Search 增量**：记录每次查询返回文档数、相对之前所有检索/读取新增的文档数、新增标准证据数，以及完全重复的调用参数。按文档去重；没有新文档仍可能取得同文档的新片段，不能直接判成无效调用或工具误导。
- **可比性**：分析器拒绝合并其他运行的判分；跨插件比较会校验语料与答案指纹、任务矩阵、模型、OMP、预算、系统提示和 runner 源码指纹。不同代码版本的探索运行可分别分析，不标为受控索引对比。远端模型标识相同仍不保证服务端版本和延迟一致。
- **官方格式导出**：保留原 query_id/docid、工具计数、取回文档集合及答复，将路径引用映射为 `[docid]`。每种配置及重复单独导出，可交给上游 `scripts_evaluation/evaluate_run.py`，配合对应原始 ground truth/qrels 和 Qwen3-32B 判分。导出不包含标准答案。

通用 runner 的 `success` 在本适配模式下表示“精确匹配代理＋运行约束通过”，**不是官方答案正确率**。专用报告明确区分这些指标，正式结论优先查独立判分结果并人工抽检。

`benchmarks/browsecomp/` 职责：`prepare.py` 导入，`run.py` 编排全流程，`judge.py` 独立判分，`analyze.py` 诊断和对比，`evaluate.py` 证据指标与上游导出。通用 Agent 执行和模型预检分别位于 `benchmarks/agent/runner.py`、`preflight.py`，继续服务原有读写任务集。

### 知识库构建指标的边界

当前的 evidence/gold recall 是 Agent 整个任务中的累计文档召回，不能标成单次 Search 的 Recall@K。

独立检索实验已实现，见 [retrieval](../retrieval/README.md)：固定查询与 K，对每个插件计算 Recall@K 和 nDCG@K。主基准使用仅从问题生成、在检索前冻结的查询，也可从旧轨迹导出符合条件的首次查询；这两种查询集不能混用比较。平台当前 Search 最多返回 10 个片段，先去重为文档，不能当成上游文档 Recall@100/1000 评测。超长查询明确拒绝，不静默截断原问题。

这些标签主要评估文档检索，不能直接量化分块是否包含完整证据、知识抽取是否遗漏或答案每句话是否有依据。相关能力需增加片段级标签、忠实性核验和真实业务测试集。

## 与官方全量评测的差异

1. 官方约十万篇文档；这里选择部分问题（可按顺序或固定种子抽样），保留全部正例及每题指定数量的困难负例，多个问题共用同一池。减小了检索空间，因此不能与官方榜单直接比成绩。
2. 官方文档规模超过现有演示索引的 1,000 文件、16 MiB、16,000 分块上限。导入器在写出前检查，超限会报错，不会偷偷截断文档或丢证据。全量跑需要先实现更大规模的索引后端和语料加载方式。
3. 使用我们的 MCP 工具、Markdown 分块、返回长度和 OMP 的提示约定；不同于上游默认 Top-5/512-token 工具设置。
4. 原问题原样传给 Agent，输出格式要求放在通用系统提示中；语义判分默认使用配置的 GLM，官方默认是 Qwen3-32B。

## 来源与数据保护

- [项目与论文](https://github.com/texttron/BrowseComp-Plus)，代码参考版本 `046949032b0328319cc9a02663a759ec601d9402`。
- [官方问题及附带文档](https://huggingface.co/datasets/Tevatron/browsecomp-plus)，自动下载固定版本 `144cff8e35b5eaef7e526346aa60774a9deb941f`；本地分片导入记录实际 SHA，不假定属于此版本。
- [官方独立完整语料](https://huggingface.co/datasets/Tevatron/browsecomp-plus-corpus)。首版使用问题记录附带的文档，以避免下载整个语料，仅支持缩小语料测试。
- 解混淆遵循发布者公开的 SHA-256 派生密钥/XOR 协议；上游许可证见 [UPSTREAM_LICENSE.txt](UPSTREAM_LICENSE.txt)。

上游要求避免把基准题目与答案以明文传播到网上。下载、解混淆、任务表、答案和原始轨迹均放在 Git 忽略的 `data/` 内；导入目录另带 `.gitignore`。提交代码和不含题目答案的汇总即可，不提交原始数据。
