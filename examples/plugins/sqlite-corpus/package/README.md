# SQLite Corpus

独立的大语料 BM25 基线插件，采用 SQLite FTS5 的 Porter + Unicode61 分词与文档级排序。正文存入磁盘表，FTS 使用外部内容表，流式构建、分批提交，不加载整个知识库到内存。不调用大模型或 Embedding 服务。

平台接入使用 Search 协议 1.1，见 `docs/search-plugin-protocol.md`。它与旧简易 Markdown 分块插件是两个不同基线：不能把二者的差异都解释成平台优化。

- `build`：从 `documents_path` 的 JSONL 逐行读取 `path/text/sha256/version`，生成 `index/search.sqlite3`。
- `search`：只读打开已有索引，返回匹配文档及最多 1,200 字符的原文窗口和行号；支持目录过滤。窗口优先覆盖查询词，不生成摘要。
- `rank`：供独立检索评测使用的额外入口，返回最多 1,000 个去重文档路径和分数，不返回正文。它不是额外暴露给 Agent 的 MCP 工具。

查询保持原文传入。插件内部做 NFKC、大小写归一、词提取、英文停用词过滤、去重及最多 256 个查询词的 OR 匹配；正文分词使用 FTS5。参数是该插件的实现，不由平台统一规定。该 BM25 实现不等同于上游 Pyserini。

程序属于管理员审核并按包指纹批准的可信代码。SQLite 以 `mode=ro` 打开查询索引，不能修改输入或已发布索引；Python 运行时不是安全沙箱。
