# BM25 Markdown 检索

独立的全文检索插件：Markdown 章节分块 → SQLite FTS5 建索引 → BM25 排序 → 返回来源路径、行号和原文片段。支持中文相邻双字、英文词和目录过滤。不调用 LLM 或 Embedding，不生成答案。

新安装使用 `bm25-markdown@1.0.0`，是原 `simple-rag-python` 的明确命名版本。旧 ID 和包保留兼容；安装新插件不会自动改动已有绑定。旧 `simple-rag` 则是兼容的声明式全文后端。

`search.py --input request.json --output output/` 实现 Search 程序协议 1.0。仅依赖 Python 标准库，需 SQLite FTS5。索引由管理员显式构建，文档改变后重建；最多 16,000 个片段。它是词法检索基线，对同义改写、跨语言和长问题的质量不作保证。

原实现来自同仓库 `simple-rag-python`。通用知识库插件内置相同程序以同时提供更新和检索；测试检查两份程序一致。
