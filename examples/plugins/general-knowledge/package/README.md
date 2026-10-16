# 通用 Markdown 知识库

将增量材料整理为可浏览、可追溯的知识文档。提供初始化、更新与 BM25 检索三个入口；只生成候选版本，由平台人工审核后发布。Python 3.11+ 标准库。

```text
workspace/
├── knowledge/                 # 只挂载此层给用户与 Agent
│   ├── INDEX.md               # 标题、分类、适用范围、摘要和链接
│   ├── guide/                 # 操作指南
│   ├── concept/               # 概念说明
│   ├── reference/             # 参数、规范等参考资料
│   └── decision/              # 决策与适用条件
└── metadata/general-knowledge/
    ├── state.json             # 托管文档、内容哈希和精确去重映射
    ├── processed.jsonl        # 材料处理记录、来源和变更前内容
    └── review.jsonl           # 冲突/不确定材料（存在时创建）
```

分类目录在首次生成对应文档时创建。文件名使用程序生成的稳定 ID，显示标题写在 Markdown 中和索引里。模型不能指定文件路径。

更新步骤：

1. 核对完整工作快照与基准版本，保留已有辅助文件。
2. 相同材料 ID 与内容重放直接复用结果；相同 ID 携带不同内容则失败。
3. 对相同标题、上下文、适用范围和正文（仅统一换行符）的新内容做精确去重，不调用模型；保留新增来源。相关文档已被修改则不复用过期的去重结论。
4. LLM 根据同范围的现有文档判断新增、补充合并、语义重复、明确纠错或待审。`merge` 只追加兼容信息，`revise` 仅允许 correction 材料替换，旧内容保留在审计记录。
5. 校验目标 ID、适用范围、决策结构与原文引文，生成候选文档、索引、逐材料结果和待审记录。
6. 平台审核并发布；发布后需要手动重建检索索引。

`observation` 总是进入待审区。模型判断的冲突也进入待审区，不写入知识正文。模型未能识别的冲突仍可能进入候选，因此候选始终需要人工审核；引文校验不能证明结论真实或保证语义去重准确。

## 配置

默认 `{"mode":"llm","default_scope":"general","max_documents":500,"max_context_chars":80000}`。

平台模型连接使用 OpenAI 兼容 Chat Completions JSON object 接口，环境映射：

```json
{
  "KNOWLEDGE_LLM_BASE_URL": "base_url",
  "KNOWLEDGE_LLM_MODEL": "model",
  "KNOWLEDGE_LLM_API_KEY": "api_key"
}
```

密钥只由平台注入环境。模型错误使任务失败，不降级为假结果。`structured` 模式用于开发验证或显式整理的输入：context 可含 `knowledge_action`（create/merge/revise/duplicate/defer/skip）、`knowledge_type`、`knowledge_target`、`knowledge_summary`、`knowledge_title` 和 `scope`。它不提供语义去重；生产自然语言材料使用 llm 模式。

当前范围：适合从空工作区创建的小型知识库，最多 500 文档，每篇正文最多 4,000 字符；同范围模型上下文最多配置到 160,000 字符，超限明确失败，不静默截断。快照最多 2,000 项/32 MiB。自动导入已有任意目录、归档、重分类、删除和自动建索引不在首版范围。

若托管文档被网页或其他程序直接编辑，下一次更新会检测哈希变化并停止，防止覆盖人工修改；首版需要显式迁移后再更新。辅助文件会原样保留。范围字段仅用于内容组织，权限仍由平台实现。

## 接口与来源

`python program.py initialize|update --input <input> --work <work> --output <output>`，遵循平台工作区协议 1.1。检索入口为 `search.py`，Search 协议 1.0。模板需绑定完整工作根，`knowledge_subpath` 指向 `knowledge`，不能指向根目录或 metadata。

更新器基于 `oh-my-harness/folder-kb-memory-template` 的协议校验、来源记录和候选处理实现，独立随包运行，不导入平台内部代码；检索来自同仓库 `bm25-markdown`。本地可信 Python 执行器不提供第三方程序沙箱。
