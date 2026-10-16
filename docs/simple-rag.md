# 从旧简易 RAG 迁移到 BM25 Markdown

旧 `simple-rag`、`simple-rag-python` 示例已从当前源码删除。现在使用 [BM25 Markdown](../examples/plugins/bm25-markdown/package/README.md)，打包与安装见[插件目录](../examples/plugins/README.md)。它提供词法检索，不调用模型、生成答案或构建向量索引。

## 已有知识库

1. 安装 `bm25-markdown@1.0.1`，批准程序运行并授权给库所属组织或个人。
2. 在知识库「检索索引」中独立选择该插件，保留原知识管理插件。
3. 手动重建索引，再验证 Search 和来源引用。旧索引不能直接复用。

源码清理不会删除服务器上的安装记录、知识文件或历史任务，也不会自动改绑正在使用的库。平台暂未提供插件卸载接口；不要直接删除仍被引用的安装目录或数据库记录。

## 历史评测

历史报告中的旧插件 ID、包摘要和指标保持原样。复现旧实现可从删除前的 Git 提交 `492cfea` 检出源码。当前 benchmark 默认使用 BM25 Markdown，需要显式 `--trust-plugin` 批准在隔离测试进程中执行；新结果应单独生成，不覆盖旧输出。全量 BrowseComp-Plus 继续使用 `sqlite-corpus`。

内置全文兼容后端的回归使用 `tests/fixtures/legacy-fulltext` 合成夹具，不再依赖可安装的旧示例。该夹具只用于兼容性验证，不列入基础插件目录。
