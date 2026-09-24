# 模板注册、安装与绑定 API

本版提供管理员 REST 接口，复用现有 KB_ADMIN_TOKEN。模板代码由开发者提供，元数据保存在平台；消费 Agent 的 MCP 接口不变。

## 1. 已实现范围

```text
开发者打包 → 注册元数据 → 上传并校验 ZIP → 安装固定版本 → 知识库绑定版本及配置
```

安装、绑定不会运行程序、拉取镜像或修改正式知识。程序调度、模型连接及人工审核发布见[更新流程](template-updates.md)。每任务容器隔离尚未实现；runtime 的 memory_mb、cpus 是声明，尚不代表独立任务配额。

管理页面入口：**模板**中注册并上传程序包，**知识库 → 模板配置**中设置版本、实例配置和工作范围。

## 2. 开发者打包

在源码仓库根目录运行，--output 必须是尚不存在的目录：

```bash
python -m folder_kb.template_package --source examples/templates/reference-docs --definition examples/template-platform-config.json --output data/template-upload
```

Windows 可使用 `.venv/Scripts/python.exe` 替换 python。产物：

- package.zip：开发者程序包，program.py 等文件直接位于 ZIP 根层，不额外套一层目录。
- registration.json：平台元数据，自动补充 source.type=uploaded-zip 及 ZIP 的 SHA-256。

本版采用 ZIP 上传，暂不接受服务端任意本地路径或直接拉取 Git。开发者可先从固定 Git commit 检出代码，再打包上传。

## 3. 注册与安装

```bash
curl -X POST "$BASE_URL/admin/api/templates" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary @data/template-upload/registration.json

curl -X PUT "$BASE_URL/admin/api/templates/reference-docs/versions/0.2.0/package" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/zip" \
  --data-binary @data/template-upload/package.zip
```

注册元数据：

| 字段 | 规则 |
|---|---|
| id / version | 分别标识模板及固定版本；允许字符见 OpenAPI，不允许路径跳转 |
| name / description / maintainer | 名称、说明及开发者 |
| protocol_version | 支持 1.0 / 1.1；显式工作范围绑定要求 1.1 |
| source | type=uploaded-zip，archive_sha256 为 ZIP 文件摘要 |
| capabilities | initialize / update，至少一个且不可重复 |
| entrypoints | 与 capabilities 一一对应的 argv 数组；本版采用 `["python","program.py","update"]` 形式，脚本必须在包内 |
| runtime | type 为 container 或 trusted-python-demo；container 声明必须提供按 SHA-256 固定的镜像；另含 timeout_seconds、memory_mb、cpus |
| config_schema | JSON Schema Draft 2020-12，根类型必须为 object；允许本地引用，不访问远程 Schema |
| required_secrets | 所需环境变量名，不填写密钥；运行前校验所选模型连接及变量映射 |
| output | 1.0 的 directory 为 knowledge，1.1 为 workspace；required_files 声明应存在的候选文件，默认 INDEX.md，执行阶段再校验 |

成功注册返回 201，status=registered。相同 ID+版本及相同规范化元数据重试返回 duplicate=true；改变任何元数据需使用新版本，否则 409。注册后没有编辑旧版本的接口。

上传成功返回 200，status=installed 及 package_sha256（展开后的目录内容摘要）。archive_sha256 是上传 ZIP 的字节摘要，两者用途不同。

安装限制：

- 上传 ZIP 最多 32 MiB，解压后最多 32 MiB、2000 个条目。
- 拒绝绝对路径、父目录跳转、链接、特殊文件、加密 ZIP、重复路径和大小写目录冲突。
- 检查入口脚本存在，但不执行代码或安装依赖。
- 先在私有临时目录解压，通过校验后切换到 templates/<id>/<version>/；数据库写入成功后标记 installed。
- 并发安装同一包可重试；同版本不覆盖不同内容。若进程在目录切换后、数据库提交前中断，可重新上传同一个包恢复。
- 普通失败清理本次临时目录；进程被强制终止可能遗留 .staging 下临时文件，自动清理策略尚未实现。

installed 表示该包曾完成安装，不等于程序可运行。绑定时会重新核对目录完整性；未来执行器仍须在启动前核对。

## 4. 知识库绑定

先通过已有知识库管理接口注册知识库，再绑定模板：

```bash
curl -X PUT "$BASE_URL/admin/api/libraries/team-guide/template" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"template_id":"reference-docs","template_version":"0.2.0","config":{}}'
```

绑定要求：

- 库存在，模板版本已安装且支持 update。
- 包目录仍与安装摘要一致。
- config 通过开发者声明的 Schema；不自动填充 Schema 的 default，不修改配置。
- required_secrets 可声明所需环境变量，执行前由更新进程校验凭据注入。

不同工作范围持有独立的 config；共享同一工作范围的检索视图必须使用相同模板版本、配置及排除规则。同一知识库再次 PUT 可更换版本或配置，但不会立即更新知识；没有自动升级到最新版本的行为。元数据中不要填写密钥，实际凭据注入后续单独实现。

### 单独配置工作根与检索目录

绑定请求可增加 workspace，例如现有库只挂载 KB_ROOT/repo/knowledge，但更新需要整个 repo：

```json
{
  "template_id": "reference-docs",
  "template_version": "0.2.0",
  "config": {},
  "workspace": {
    "type": "local",
    "root_path": "repo",
    "knowledge_subpath": "knowledge",
    "exclude_paths": ["temporary"]
  }
}
```

Git 托管库的 workspace 则使用：

```json
{
  "type": "git",
  "repository_id": "已登记的仓库ID",
  "workspace_subpath": ".",
  "knowledge_subpath": "knowledge",
  "exclude_paths": []
}
```

root_path 必须在 KB_ROOT 内；Git 库必须对应现有 repository_libraries 关联，且已有发布版本。workspace_subpath 相对仓库根，knowledge_subpath 相对工作根，二者组合必须等于当前检索映射。平台在启动任务前重新解析，不固定保存某次发布的绝对路径。

GET 绑定返回 workspace 和 workspace_key。省略 workspace 时只使用原挂载目录作为工作根，knowledge_subpath 为“.”。修改挂载后需确认模板工作配置；当前配置不匹配则拒绝生成快照。共同工作范围使用不同模板/配置/排除规则返回 workspace_binding_conflict；切换共同模板时须一致调整所有视图，当前可先解除相关绑定再重新绑定。

默认排除项、完整候选与删除声明见[工作目录协议](template-protocol.md)。任务按工作区互斥调度，候选必须人工审核发布，见[更新流程](template-updates.md)。

## 5. 查询与解除

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | /admin/api/templates | 分页列表；offset 默认 0，limit 默认 50、最多 100，使用 next_offset 续页 |
| GET | /admin/api/templates/{id}/versions/{version} | 查看模板定义、安装状态及摘要 |
| GET | /admin/api/libraries/{library_id}/template | 查看当前绑定；未绑定返回 binding=null |
| DELETE | /admin/api/libraries/{library_id}/template | 解除绑定，保留知识文件和模板程序包 |

本版没有删除模板版本的 HTTP 接口。任务启动与审核接口见[更新流程](template-updates.md)。所有上述接口只允许管理员令牌，Agent Key 不能调用。操作审计保存模板 ID、版本及目标库，不记录配置正文或 ZIP 内容。

## 6. 验证与后续

```bash
python -m pytest tests/test_template_management.py tests/test_templates.py -q
python -m folder_kb.template_demo
```

演示已复用同一 TemplateCatalog 注册、安装和绑定逻辑，仍在全新独立目录中运行，不操作正在运行的服务。

已提供独立可信 Python worker、模型凭据引用、任务恢复、当前授权检查和人工审核发布。每任务容器隔离及 Git 写回尚未实现。数据库为 v6，旧绑定迁移不自动启用更新；HTTP 与所有 worker 需同步升级。
