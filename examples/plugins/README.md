# 基础插件

| 插件 | 用途 | 模型依赖 | 更新能力 |
|---|---|---|---|
| `bm25-markdown` | Markdown/文本的 BM25 全文检索 | 无 | 无，保持现有目录结构 |
| `general-knowledge` | 通用 Markdown 知识库，分类目录与来源审计 | 自然语言整理需要 LLM | 初始化、精确去重、语义合并建议、纠错和冲突待审；内含 BM25 |
| [memory-files](https://github.com/oh-my-harness/folder-kb-memory-template) | 跨会话个人记忆 | 自然语言整理需要 LLM | 独立仓库维护 |
| `sqlite-corpus` | 大规模只读语料的文档级 BM25 基线 | 无 | 无，供全量 benchmark 使用 |

`simple-rag`、`simple-rag-python` 是旧 BM25 实现的兼容 ID，名称不代表向量检索或生成能力；冻结版本及既有绑定保留，新安装优先使用 `bm25-markdown`。`personal-memory` 仅为声明示例，不能替代可运行的 memory-files 插件。

一个库可分别选择知识管理插件和检索插件。通用知识库仍提供组合式 BM25 入口；也可以在「检索索引」中独立绑定 `bm25-markdown` 或其他检索插件。管理插件声明是否允许直接编辑，检索插件不改变文档编辑策略。平台定义协议和授权，具体分类、去重、模型判断及检索算法都属于插件。

## 打包安装

```powershell
python -m folder_kb.plugin_package --source examples/plugins/bm25-markdown/package --definition examples/plugins/bm25-markdown/definition.json --output dist/bm25-markdown-1.0.1
python -m folder_kb.plugin_package --source examples/plugins/general-knowledge/package --definition examples/plugins/general-knowledge/definition.json --output dist/general-knowledge-1.1.1
```

管理台“插件 → 安装插件”上传各目录的 `plugin.json` 和 `package.zip`。注册版本不可原地替换，升级应使用新版本。已有库绑定旧插件不会自动迁移。

通用库使用步骤：创建空的工作目录和其中的 `knowledge/` → 绑定 general-knowledge，工作根指向完整目录、检索子目录为 knowledge → 更新设置选择已授权的模型连接并配置 README 中的环境映射、批准程序包 → 初始化候选审核发布 → 接收增量材料 → 审核增量候选 → 手动建/重建索引。

`1.1.1` 统一 skip 证据约定，增加一次有边界的模型输出纠正，区分来源可信度与事实冲突，并停止把适用范围重复写进正文页脚。已有绑定不会自动升级。

也可在用户工作区新建知识库时直接选择已授权的通用模板，平台创建独立完整工作区和 `knowledge/` 子目录。`1.1.0` 支持人工编辑正文与插件更新共存，自动生成的 `INDEX.md` 保持只读；已有 `1.0.1` 绑定保持原有策略，升级需要显式选择新版本。

BM25 索引程序和更新程序分别使用平台现有的运行审批；安装包不等于授予执行权限。通用插件的具体组织、去重边界及首版限制见 [README](general-knowledge/package/README.md)。
