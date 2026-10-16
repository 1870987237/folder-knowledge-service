# 知识库模板与增量更新管理

## 入口与职责

1. 平台管理员在「插件 → 详情 → 使用授权」批准已安装的具体版本，并选择组织或个人账号。模型连接在「使用授权」中单独分配。插件版本授权绑定程序包摘要。
2. 组织管理员创建组织库、个人创建私有库时，可以选择已授权模板；不选择即创建普通文档库。库内「知识库设置 → 模板与更新」可更换模板、配置参数、选择模型、启停更新、设置批量大小和自动生成候选。
3. 编辑者通过「增量收件箱」提交材料；Agent 继续使用 `submit_material`。发布者通过「更新任务」触发处理、查看差异、批准发布、拒绝候选或重试失败任务。

材料更新只生成候选，发布必须人工审核。新建模板库并启用更新程序时，平台在同一事务中排队初始化，由 worker 自动发布初始结构；不在 HTTP 请求内执行插件。未启用更新时暂不执行，首次启用后排队。此授权仅用于新库未被修改的初始目录，执行及发布均重新检查创建者管理权限、插件授权和目录摘要；人工修改或授权失效会停止自动发布。已有库手动初始化仍生成待审核候选。

未初始化时，更新请求返回 `library_not_initialized`，不创建失败任务、不领取材料。页面显示初始化状态，初始化完成后隐藏初始化按钮；无可处理材料时禁用处理按钮。自动初始化失败可在详情中重试；若期间有人修改了文档，应检查内容后使用手动初始化并审核。

“处理待办材料”包含有权限的新增材料和失败待重试材料，遵守每批数量上限，并保留历史任务关联。自动触发只领取从未进入任务的新材料，不自动反复重试失败任务。列表显示待处理总数及其中失败待重试数量；处理中的材料不再重复领取。

## 权限

| 能力 | 读者 | 编辑者 | 发布者 / 库级管理者 | 个人所有者 / 所属组织管理员 |
|---|---|---|---|---|
| 阅读已发布文件 | 是 | 是 | 是 | 是 |
| 提交材料、查看自己的材料 | 否 | 是 | 是 | 是 |
| 查看库内全部材料、生成候选、重试及审核发布 | 否 | 否 | 是 | 是 |
| 配置模板、模型、更新策略及成员权限 | 否 | 否 | 否 | 是 |
| 安装程序包、批准资源、配置模型密钥及服务器路径 | 否 | 否 | 否 | 否，属于平台管理 |

资源按**知识库归属**判定：授权给某个组织的模型不能被该组织管理员用于个人库，给个人的资源也不能用于组织库。成员身份本身不授予库内容权限，仍沿用库共享与成员/部门/用户组授权。

资源授权、提交者权限在执行和发布前重新检查；人员审核者降权或离开组织后，尚未发布的候选不能继续发布。旧的、由平台直接批准的绑定保留原有行为，不自动开放到资源目录；从工作区重新配置后进入上述委托授权机制。

更新候选涉及完整工作目录，工作区检查其覆盖的所有知识库权限。委托更新不能跨越不同归属的知识库，也不能使用知识根目录或 Git 发布目录作为可写工作区。服务器目录与工作根映射仍由平台管理。

## 两种写入模式

- 普通文档库：正文编辑、草稿审核、可授权的 Agent 直接写入。
- 更新模板库：模板拥有更新工作目录，人员与 Agent 提交材料，程序生成候选后审核发布；是否允许直接编辑由插件声明，见 [插件组合与编辑](plugin-composition-and-editing.md)。

原生库首次绑定更新模板时，必须是独立目录且没有未完成草稿。绑定操作接管该发布目录，不扩大到父目录。更换模板只改变程序和配置，不迁移已有知识结构；已有库应使用增量更新。当前不提供把已发布模板库一键转换回普通文档库的功能。

## 组件与存储

- `resource_access.py`：平台资源授权与按库归属筛选。面向普通用户仅返回插件说明/配置规范和模型名称，不返回服务器 URL、密钥变量或其他库的绑定信息。
- `library_updates.py`：工作区 API，负责身份与库范围校验，调用既有服务；不另建任务队列或 worker。
- `templates.py`：安装、绑定和配置校验；`inbox.py`：统一材料记录；`updates.py`：任务状态与审批；`template_worker.py`：执行与发布。

人员材料仍在 `KB_STATE_DIR/management.sqlite3` 的 `inbox_items`。新增 `user_id`，与 `key_id` 恰有一个非空；分别按人员/Key、库、提交编号幂等，不创建隐藏 Agent Key。任务、候选、正式发布目录继续使用原有位置，见 [更新流程](template-updates.md)。

SQLite v14 新增 `library_bootstrap`，记录新库初始化授权的创建者、插件、工作范围、初始摘要及任务 ID。迁移不为已有库补跑初始化，不修改材料和历史任务。已有库优先依据同插件的已发布任务识别就绪；导入目录没有任务历史时检查插件声明的必需产物，不能替代插件内部完整性校验。部署前备份，HTTP 与 worker 同时升级。插件执行仍要求平台启用受信 Python 运行和凭据白名单，资源使用授权不绕过运行环境约束。

## 接口

| 接口 | 用途 |
|---|---|
| `GET /admin/api/resource-subjects` | 查询可分配的组织与启用账号 |
| `GET/PUT /admin/api/resource-access/plugin/{id}@{version}` | 插件版本授权、摘要批准、模型变量映射 |
| `GET/PUT /admin/api/resource-access/model/{id}` | 模型使用授权 |
| `GET /portal/api/resources?organization_id=…` 或 `?library_id=…` | 查询当前归属可用资源；省略参数为个人库 |
| `POST /portal/api/libraries` | 新增可选 `template` 字段，与库创建原子提交 |
| `GET/PUT /portal/api/libraries/{id}/plugin-settings` | 读取、修改实例模板和更新策略 |
| `GET/POST /portal/api/libraries/{id}/inbox` | 分页查看、幂等提交标准材料 |
| `GET /portal/api/libraries/{id}/inbox/{item}` | 材料与处理回执 |
| `GET/POST /portal/api/libraries/{id}/update-jobs` | 当前库任务列表、触发 `initialize` / `update` |
| `GET /portal/api/libraries/{id}/update-jobs/{job}`、`…/preview` | 任务与候选差异 |
| `POST /portal/api/libraries/{id}/update-jobs/{job}/{approve\|reject\|retry}` | 审批、拒绝、重试 |

任务列表额外返回 `initialization`（`ready`、`required`、初始化任务状态或 `unavailable`）、`pending_count`、`retry_count`，以及控制按钮的 `can_run` / `can_initialize`。任务详情提供不含凭据的 `error_message`；新库自动初始化的 `publish_mode` 为 `new_library_initialization`，材料更新及手动初始化为 `manual_review`。

`template` 与 `PUT plugin-settings` 使用相同结构：

```json
{
  "plugin_id": "reference-docs",
  "plugin_version": "0.2.0",
  "config": {},
  "enabled": true,
  "auto_trigger": false,
  "model_connection_id": null,
  "max_batch_size": 10
}
```

程序包批准摘要、模型环境变量映射由平台授权派生，工作区请求不能传入这些字段，也不能指定服务器工作目录。材料沿用 [标准信封](inbox-api.md)，`submitted_by.type` 为 `user` 或 `agent_key`。

插件可用顶层 `model_binding` 声明条件性模型依赖。平台按声明生成资源授权与更新策略的默认映射，保存授权时验证变量对应字段；启用策略、入队和执行时检查当前模式是否需要模型。`required_when` 按配置字段全部相等判定，遗漏的字段使用 `config_schema` 默认值；空条件表示始终需要模型。`required_fields` 控制执行时哪些连接值不得为空，默认 `base_url` 和 `model`。未声明的旧插件保持原有行为。

```json
{
  "model_binding": {
    "required_when": {"mode": "llm"},
    "environment": {
      "KNOWLEDGE_LLM_BASE_URL": "base_url",
      "KNOWLEDGE_LLM_MODEL": "model",
      "KNOWLEDGE_LLM_API_KEY": "api_key"
    },
    "required_fields": ["base_url", "model"]
  }
}
```

这是变量接线声明，不包含密钥，不替代资源授权和 worker 凭据白名单。注册定义不可变，已有通用知识库应升级绑定到 `general-knowledge@1.1.2` 并重新保存更新设置，无需重新初始化。`tests/test_plugin_model_binding.py` 验证默认映射、错误配置拒绝，以及 HTTP 配置 → worker 子进程 → 模型 HTTP → 候选审核发布的完整链路。

索引仍需在「检索索引」中显式构建，没有新增自动建索引。

## 验证

`tests/test_library_updates.py` 覆盖组织/个人资源隔离、配置权、混合材料、幂等、手动审批、四类授权撤销、跨库任务与材料 ID、初始化、重试、v11 迁移。浏览器使用 `python -m tests.library_updates_ui_fixture` 创建独立合成数据；此工具拒绝覆盖已有目录，不读取真实知识内容。
