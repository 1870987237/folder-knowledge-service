# 固定查询检索评测

与 Agent 任务评测分开：不给模型自由改写和多轮查找，按固定 CSV 查询真实 MCP Search，衡量插件在当前工具返回预算下的文档排序。

```powershell
python -m benchmarks.retrieval.run --output data/retrieval-baseline
python -m benchmarks.retrieval.run --output data/retrieval-program --plugin-definition examples/plugins/simple-rag-python/definition.json --plugin-source examples/plugins/simple-rag-python/package --trust-plugin
```

默认复用合成知识目录，使用独立 `retrieval-queries.csv`（id,query）和 `retrieval-qrels.json`（查询 ID 到相关文档相对路径列表）。8 个查询包含专有标识、中文词、同义表达和无答案查询，不代表真实业务分布。

可指定 `--dataset`、`--queries`、`--qrels` 和 `--ks 1 3 5 10`。标准答案和标签不发送给插件；插件仅获得建索引语料和查询。只使用隔离服务，不支持指向生产 URL。

- 每条查询取最多 10 个片段，按首次出现顺序去重成文档；不足 K 不补造结果。
- Recall@K：前 K 个去重文档中相关文档数 / 全部相关文档数。
- nDCG@K：二值相关性，以 log2(rank+1) 折损并除以理想排序。
- MRR@K：第一个相关文档的倒数排名。Precision@K 固定除以 K。
- 请求失败按空结果计入有正例查询均值，同时单独统计成功率。无正例查询单列，不进入召回率均值。
- 原始问题超过 200 字符会在运行前明确拒绝。需要另行编写并冻结短查询，不静默截断、不依据答案挑关键词。

这不是完整文档 Top-K 候选集：同一文档的多个片段可能占满返回预算。报告必须注明“Top-10 片段内文档去重”；不能宣称官方 Recall@100/1000。若接入 BrowseComp，使用固定任务 ID，qrels 分别选择 evidence 或 gold 并保留出处，不合并两个不同标签口径。

产物有 plan.json、setup.json、results.json 和 report.md，记录语料/查询/qrels/插件/源码指纹及构建和查询耗时。结果含查询，公开基准产物应留在忽略的 data 目录。比较插件时固定同一语料、查询和 qrels；耗时比较还需要多次重复和负载控制。
