# 插件目录与使用指南

以下是源码提供的插件，不代表某台服务器已经安装或授权。实际可用版本以管理台「插件」及资源使用授权为准。

| 插件 ID / 源码版本 | 定位与用途 | 模型依赖 | 文档与源码 |
|---|---|---|---|
| `bm25-markdown@1.0.1` | 推荐基础检索插件；Markdown 章节分块、SQLite FTS5 / BM25，保持原目录，不生成答案 | 无 | [README](bm25-markdown/package/README.md) · [定义](bm25-markdown/definition.json) |
| `general-knowledge@1.1.2` | 推荐知识管理插件；分类目录、初始化、精确去重、语义合并建议、纠错与冲突待审；内含 BM25 | 自然语言整理需要 LLM；structured 模式用于确定性验证 | [README](general-knowledge/package/README.md) · [定义](general-knowledge/definition.json) |
| `memory-files`（独立仓库） | 跨会话个人记忆；整理用户偏好、反馈、项目约定与资料位置，生成候选并保留来源 | 自然语言整理需要 LLM | [源码与 README](https://github.com/oh-my-harness/folder-kb-memory-template) |
| `sqlite-corpus@1.0.0` | 大规模语料评测基线；文档级 BM25，流式建库，提供评测用 rank 入口 | 无 | [README](sqlite-corpus/package/README.md) · [定义](sqlite-corpus/definition.json) |

旧 `simple-rag`、`simple-rag-python` 和 `personal-memory-guide` 示例已从当前源码删除。新的安装和 benchmark 使用上表插件；历史评测仍保留原 ID 与结果，不改写为新插件成绩。已有服务器安装记录不会因源码删除而自动卸载，迁移到 BM25 Markdown 后需重建索引。

## 如何选择与组合

- 已有文件夹只需要搜索：使用 `bm25-markdown`。
- 需要将提交材料整理成通用知识：使用 `general-knowledge`，可保留内置 BM25，也可单独选择检索插件。
- 需要积累跨会话偏好和项目经验：使用独立仓库的 `memory-files`。
- 需要对大规模语料做检索评测：使用 `sqlite-corpus`；其文档级排序与 Markdown 分块基线不同，结果不能直接归因于平台改进。

一个库可分别选择知识管理插件和检索插件。通用知识库仍提供组合式 BM25 入口；也可以在「检索索引」中独立绑定 `bm25-markdown` 或其他检索插件。管理插件声明是否允许直接编辑，检索插件不改变文档编辑策略。平台定义协议和授权，具体分类、去重、模型判断及检索算法都属于插件。

## 打包安装

```powershell
python -m folder_kb.plugin_package --source examples/plugins/bm25-markdown/package --definition examples/plugins/bm25-markdown/definition.json --output dist/bm25-markdown-1.0.1
python -m folder_kb.plugin_package --source examples/plugins/general-knowledge/package --definition examples/plugins/general-knowledge/definition.json --output dist/general-knowledge-1.1.2
```

管理台“插件 → 安装插件”上传各目录的 `plugin.json` 和 `package.zip`。注册版本不可原地替换，升级应使用新版本。已有库绑定旧插件不会自动迁移。

平台管理员安装并批准具体插件版本、配置模型连接，再将插件和模型授权给使用的组织或个人。安装本身不会执行程序，也不会自动升级已有绑定。

通用库推荐使用流程：

1. 在用户工作区新建知识库，选择已授权的 `general-knowledge@1.1.2`。平台创建完整工作区和 `knowledge/` 子目录。
2. 选择已授权的模型并启用更新。1.1.2 的 `model_binding` 让平台自动派生 `KNOWLEDGE_LLM_*` 映射，llm 模式缺少模型时明确拒绝；凭据仍由 worker 环境注入。
3. 新建库由 worker 初始化并自动发布初始结构；此行为只适用于未被修改的新库初始目录。已有库手动初始化仍需审核。未初始化时页面提示状态并阻止增量处理。
4. 提交材料后，开启「收到材料后自动生成候选」可自动处理新材料；也可点击「处理待办材料」，一并处理新增及失败待重试材料。自动触发不会反复重试失败材料。
5. 查看候选差异和逐材料结论，人工审核后发布。增量候选不会自动发布。
6. 在「检索索引」手动构建索引；知识发布或人工修改后需重建。配置 Key 的 Search 权限不能替代建索引。

已有目录由管理员绑定时，工作根须指向完整目录，检索子目录为 `knowledge`，不能把 metadata 开放给消费 Agent。完整流程、初始化例外与权限见 [知识库模板与增量更新管理](../../docs/library-template-workflow.md)。

`1.1.1` 统一 skip 证据约定，增加一次有边界的模型输出纠正，区分来源可信度与事实冲突，并停止把适用范围重复写进正文页脚。已有绑定不会自动升级。

`1.1.0` 起支持人工编辑正文与插件更新共存，自动生成的 `INDEX.md` 保持只读。`1.1.2` 补充模型依赖与映射校验，已有 1.1.x 工作目录升级无需重新初始化；已有旧版本绑定保持原有策略，升级需要显式选择新版本并保存配置。

BM25 索引程序和更新程序分别使用平台现有的运行审批；安装包不等于授予执行权限。通用插件的具体组织、去重边界及首版限制见 [README](general-knowledge/package/README.md)。

## 如何理解通用知识库的处理结果

| 结果 | 含义 |
|---|---|
| 采纳 | 生成新增、合并或纠错候选；重复内容也可能作为已采纳处理并补充来源，不一定新增文档 |
| 部分采纳 / 待确认 | 存在 defer 决策，相关内容进入待审记录，尚未全部写入知识正文 |
| 未采纳 / 无可复用知识 | 模型选择 skip，未生成知识正文；这是内容筛选结论，不是任务执行失败 |
| 任务失败 | 模型请求、输出校验、授权或工作区等环节失败，需要查看失败原因并重试 |

当前通用插件偏向提取可复用知识，临时任务状态、单次会议计划可能被模型跳过；它不保证将所有提交材料归档为可浏览文档。原始提交仍保留在平台收件箱，处理决定保存在候选工作区的 `metadata/general-knowledge/processed.jsonl`。模型判断可能有误，应结合原始材料审核；尚未实现“原文自动归档＋知识提取”的双轨流程。

## 开发与接口

- [插件安装与清单](../../docs/plugins.md)：平台安装、绑定和授权边界。
- [更新程序协议](../../docs/template-protocol.md) / [更新任务与模型配置](../../docs/template-updates.md)：输入快照、候选产物与审核发布。
- [Search 协议](../../docs/search-plugin-protocol.md)：插件决定索引和排序，平台负责路由、来源校验和权限。
- [插件组合与编辑](../../docs/plugin-composition-and-editing.md)：独立绑定、自动生成文件保护及版本冲突处理。
