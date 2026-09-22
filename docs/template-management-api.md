# 模板注册、安装与绑定 API

本版提供管理员 REST 接口，复用现有 KB_ADMIN_TOKEN。模板代码由开发者提供，元数据保存在平台；消费 Agent 的 MCP 接口不变。

## 1. 已实现范围

```text
开发者打包 → 注册元数据 → 上传并校验 ZIP → 安装固定版本 → 知识库绑定版本及配置
```

安装、绑定不会运行程序、拉取镜像或修改正式知识。程序调度、容器隔离、凭据绑定和 UI 待后续实现；runtime 中的资源字段当前是声明，不代表配额已执行。

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

curl -X PUT "$BASE_URL/admin/api/templates/reference-docs/versions/0.1.0/package" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/zip" \
  --data-binary @data/template-upload/package.zip
```

注册元数据：

| 字段 | 规则 |
|---|---|
| id / version | 分别标识模板及固定版本；允许字符见 OpenAPI，不允许路径跳转 |
| name / description / maintainer | 名称、说明及开发者 |
| protocol_version | 当前固定 1.0 |
| source | type=uploaded-zip，archive_sha256 为 ZIP 文件摘要 |
| capabilities | initialize / update，至少一个且不可重复 |
| entrypoints | 与 capabilities 一一对应的 argv 数组；本版采用 `["python","program.py","update"]` 形式，脚本必须在包内 |
| runtime | type 为 container 或 trusted-python-demo；container 声明必须提供按 SHA-256 固定的镜像；另含 timeout_seconds、memory_mb、cpus |
| config_schema | JSON Schema Draft 2020-12，根类型必须为 object；允许本地引用，不访问远程 Schema |
| required_secrets | 所需凭据名称，不填写密钥。本版可登记，但包含所需凭据的模板暂不能绑定 |
| output | directory 固定 knowledge；required_files 声明应存在的候选文件，默认 INDEX.md，执行阶段再校验 |

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
  -d '{"template_id":"reference-docs","template_version":"0.1.0","config":{}}'
```

绑定要求：

- 库存在，模板版本已安装且支持 update。
- 包目录仍与安装摘要一致。
- config 通过开发者声明的 Schema；不自动填充 Schema 的 default，不修改配置。
- required_secrets 为空；凭据管理完成后再开放依赖密钥的模板绑定。

不同知识库持有独立的 config。同一知识库再次 PUT 可更换版本或配置，但不会立即更新知识；没有自动升级到最新版本的行为。元数据中不要填写密钥，实际凭据注入后续单独实现。

## 5. 查询与解除

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | /admin/api/templates | 分页列表；offset 默认 0，limit 默认 50、最多 100，使用 next_offset 续页 |
| GET | /admin/api/templates/{id}/versions/{version} | 查看模板定义、安装状态及摘要 |
| GET | /admin/api/libraries/{library_id}/template | 查看当前绑定；未绑定返回 binding=null |
| DELETE | /admin/api/libraries/{library_id}/template | 解除绑定，保留知识文件和模板程序包 |

本版没有删除模板版本或启动模板任务的 HTTP 接口。所有上述接口只允许管理员令牌，Agent Key 不能调用。操作审计保存模板 ID、版本及目标库，不记录配置正文或 ZIP 内容。

## 6. 验证与后续

```bash
python -m pytest tests/test_template_management.py tests/test_templates.py -q
python -m folder_kb.template_demo
```

演示已复用同一 TemplateCatalog 注册、安装和绑定逻辑，仍在全新独立目录中运行，不操作正在运行的服务。

下一步实现独立 worker 与容器执行，补齐凭据引用、资源限制、任务状态恢复、当前授权检查以及候选发布。本版继续使用数据库 v4，无新增迁移。
