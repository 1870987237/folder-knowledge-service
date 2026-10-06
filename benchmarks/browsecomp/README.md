# BrowseComp-Plus 接入

让现有 OMP + GLM 通过 Folder KB 的真实 MCP 工具完成公开基准问题，对比 grep、Search 和混合配置。复用现有 Agent runner，不改变工具 schema 或替 Agent 规划调用。

**当前是缩小语料的内部适配版，不是官方全量复现。**原始问题、文档与答案不改写；所有选中问题共用一个文档池，包含全部对应 evidence/gold 文档及固定随机种子抽取的 hard negatives。每次使用独立只读知识库和会话。

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

# 任务编号来自生成的 tasks.csv；省略 --tasks 执行全部导入的问题
python -m benchmarks.agent.runner --dataset data/bcp-suite `
  --output data/bcp-run --variants grep search hybrid --timeout 180

python -m benchmarks.agent.report data/bcp-run

# 独立、无工具的判分会话；默认沿用已配置的 GLM，可指定其他判分模型
python -m benchmarks.browsecomp.judge data/bcp-run --dataset data/bcp-suite `
  --output data/bcp-judge --model hyper-op/GLM-5.3-Flash

# 导出上游支持的 run 格式，每种配置、每次重复分开
python -m benchmarks.browsecomp.evaluate data/bcp-run --export data/bcp-official-format
```

每个输出目录必须新建或为空。运行 `--repeats 3` 可观察稳定性；不要挑选性重跑失败题替换原结果。

## 文件组织

```text
data/bcp-suite/
  knowledge/documents/<原始 docid>.md   # 完整原文，仅此文件夹挂载
  tasks.csv                            # 原始问题、任务编号、readonly
  expectations.json                    # 答案和 evidence/gold 标签，不传给 Agent
  manifest.json                        # 来源版本、分片 SHA、采样方式、语料指纹

data/bcp-run/
  results.json                         # 运行环境、数据指纹、逐题结果
  report.md                            # 调用、耗时和证据召回
  episodes/...                         # MCP 请求响应、OMP 事件和最终文件

data/bcp-judge/
  judgments.json                       # 独立判分结果，不覆盖原执行记录
  report.md                            # 正确、错误、未判定分别汇总
```

沿用现有 `benchmarks/datasets/basic/` 作为快速读写回归；BrowseComp-Plus 单独测试检索，不混合统计两种任务。

## 指标与验收

- **证据召回**：经 search / grep / read_file 实际返回正文的文档 ID，与官方 evidence/gold 标签比较。目录中出现文件名不算召回，同文档多个片段只计一次。这是整个任务的文档召回，不是单次 Search 的 Recall@k，也不代表 Agent 已理解全部证据。
- **精确匹配代理**：比较 `facts.final_answer` 和参考答案，统一大小写与空白。仅作排查，未匹配不能直接解释为语义回答错误。`answer_correct` 在语义判分前保持 `null`。
- **内部语义判分**：无工具的新会话比较原问题、参考答案与候选答案，不读取其他任务轨迹。模型失败或格式错误记为“未判定”。若与被测 Agent 使用同一个模型，报告明确标注；需考虑同模型偏差，不能等同于上游判分。
- **成本与可靠性**：保留工具调用分布、错误、耗时、模型用量、超时和意外文件变化。所有配置使用同一语料；不把超时从总体执行数中移除。
- **官方格式导出**：保留原 query_id/docid、工具计数、取回文档集合及答复，将路径引用映射为 `[docid]`。每种配置及重复单独导出，可交给上游 `scripts_evaluation/evaluate_run.py`，配合对应原始 ground truth/qrels 和 Qwen3-32B 判分。导出不包含标准答案。

通用 runner 的 `success` 在本适配模式下表示“精确匹配代理＋运行约束通过”，**不是官方答案正确率**。专用报告明确区分这些指标，正式结论优先查独立判分结果并人工抽检。

## 与官方全量评测的差异

1. 官方约十万篇文档；这里选前 N 个问题并保留全部正例及每题指定数量的困难负例，多个问题共用同一池。减小了检索空间，因此不能与官方榜单直接比成绩。
2. 官方文档规模超过现有演示索引的 1,000 文件、16 MiB、16,000 分块上限。导入器在写出前检查，超限会报错，不会偷偷截断文档或丢证据。全量跑需要先实现更大规模的索引后端和语料加载方式。
3. 使用我们的 MCP 工具、Markdown 分块、返回长度和 OMP 的提示约定；不同于上游默认 Top-5/512-token 工具设置。
4. 原问题原样传给 Agent，输出格式要求放在通用系统提示中；语义判分默认使用配置的 GLM，官方默认是 Qwen3-32B。

## 来源与数据保护

- [项目与论文](https://github.com/texttron/BrowseComp-Plus)，代码参考版本 `046949032b0328319cc9a02663a759ec601d9402`。
- [官方问题及附带文档](https://huggingface.co/datasets/Tevatron/browsecomp-plus)，自动下载固定版本 `144cff8e35b5eaef7e526346aa60774a9deb941f`；本地分片导入记录实际 SHA，不假定属于此版本。
- [官方独立完整语料](https://huggingface.co/datasets/Tevatron/browsecomp-plus-corpus)。首版使用问题记录附带的文档，以避免下载整个语料，仅支持缩小语料测试。
- 解混淆遵循发布者公开的 SHA-256 派生密钥/XOR 协议；上游许可证见 [UPSTREAM_LICENSE.txt](UPSTREAM_LICENSE.txt)。

上游要求避免把基准题目与答案以明文传播到网上。下载、解混淆、任务表、答案和原始轨迹均放在 Git 忽略的 `data/` 内；导入目录另带 `.gitignore`。提交代码和不含题目答案的汇总即可，不提交原始数据。
