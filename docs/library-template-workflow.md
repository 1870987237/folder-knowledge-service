# 知识库模板与增量更新管理

## 入口与职责

1. 平台管理员在「插件 → 详情 → 使用授权」批准已安装的具体版本，并选择组织或个人账号。模型连接在「使用授权」中单独分配。插件版本授权绑定程序包摘要。
2. 组织管理员创建组织库、个人创建私有库时，可以选择已授权模板；不选择即创建普通文档库。库内「知识库设置 → 模板与更新」可更换模板、配置参数、选择模型、启停更新、设置批量大小和自动生成候选。
3. 编辑者通过「增量收件箱」提交材料；Agent 继续使用 `submit_material`。发布者通过「更新任务」触发处理、查看差异、批准发布、拒绝候选或重试失败任务。

自动生成只生成候选，发布必须人工审核。初始化也是一个需要审核的候选，在新库的「更新任务 → 初始化知识库」启动。未配置程序时，材料可以暂存在收件箱。

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
- 更新模板库：模板拥有更新工作目录，人员与 Agent 提交材料，程序生成候选后审核发布；正文可阅读，不能同时直接编辑或直接写入。

原生库首次绑定更新模板时，必须是独立目录且没有未完成草稿。绑定操作接管该发布目录，不扩大到父目录。更换模板只改变程序和配置，不迁移已有知识结构；已有库应使用增量更新。当前不提供把已发布模板库一键转换回普通文档库的功能。

## 组件与存储

- `resource_access.py`：平台资源授权与按库归属筛选。面向普通用户仅返回插件说明/配置规范和模型名称，不返回服务器 URL、密钥变量或其他库的绑定信息。
- `library_updates.py`：工作区 API，负责身份与库范围校验，调用既有服务；不另建任务队列或 worker。
- `templates.py`：安装、绑定和配置校验；`inbox.py`：统一材料记录；`updates.py`：任务状态与审批；`template_worker.py`：执行与发布。

人员材料仍在 `KB_STATE_DIR/management.sqlite3` 的 `inbox_items`。新增 `user_id`，与 `key_id` 恰有一个非空；分别按人员/Key、库、提交编号幂等，不创建隐藏 Agent Key。任务、候选、正式发布目录继续使用原有位置，见 [更新流程](template-updates.md)。

SQLite 迁移至 v12，保留原 Agent 材料、幂等编号、任务关联和回执；部署前备份，HTTP 与 worker 同时升级。插件执行仍要求平台启用受信 Python 运行和凭据白名单，资源使用授权不绕过运行环境约束。

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

索引仍需在「检索索引」中显式构建，没有新增自动建索引。

## 验证

`tests/test_library_updates.py` 覆盖组织/个人资源隔离、配置权、混合材料、幂等、手动审批、四类授权撤销、跨库任务与材料 ID、初始化、重试、v11 迁移。浏览器使用 `python -m tests.library_updates_ui_fixture` 创建独立合成数据；此工具拒绝覆盖已有目录，不读取真实知识内容。
