# 知识库模板与本地程序协议

状态：工作目录协议 1.1 原型。模板由知识库开发者实现，平台定义接口、管理绑定并提供运行资源。

## 1. 当前决策

- 模板注册元数据和实例绑定保存在平台 SQLite；知识仓库无需增加描述文件。
- Git 或程序包保存模板代码。模板版本绑定固定来源和摘要，不原地替换。
- 程序在平台资源上运行，通过命令行和文件交换数据，不要求开发者部署 HTTP 服务。
- 模板接收完整工作目录快照并生成完整候选工作目录；Agent 只检索其中配置的子目录。平台验证、检查基准版本、审核并发布。模板不直接改正式知识。
- 正式运行采用隔离的容器任务；当前先提供只运行已审阅测试程序的本地协议验证器。

## 2. 平台保存的元数据

| 对象 | 字段 |
|---|---|
| 模板版本 | ID、名称、说明、开发者、版本、协议版本、固定程序来源与摘要、初始化/更新入口、运行环境、配置 Schema、凭据名称、产物约定 |
| 知识库绑定 | 知识库 ID、模板 ID 和版本、实例配置、工作来源、检索子目录、排除路径与 workspace_key；后续增加凭据引用及触发/发布策略 |
| 更新任务 | 任务 ID、知识库、模板版本、知识基准版本、运行状态、结果及错误类型 |

当前已增加 template_versions、library_templates、template_jobs 三张表；数据库版本为 v5（绑定表增加工作范围字段）。模板元数据中的资源需求是申请值，实际配额和可用权限由平台决定。凭据明文不进入元数据或 request.json。

模板程序、注册权限与模板使用权限分开管理；模板注册、ZIP 安装和绑定已提供管理员 REST 接口，见[模板管理 API](template-management-api.md)。账号/组织权限尚未实现，当前沿用现有管理员令牌。演示通过同一业务逻辑操作独立数据库，不影响运行中的服务。

## 3. 平台目录

```text
platform-data/
├── management.sqlite3
├── templates/<template-id>/<version>/
│   ├── package/                  # 已安装程序，正式运行只读挂载
│   └── manifest.json             # 安装版本与摘要，不是管理配置的权威来源
└── jobs/<job-id>/
    ├── input/
    │   ├── request.json
    │   ├── materials.jsonl
    │   └── workspace/            # 工作快照，包含知识与辅助资料
    ├── work/
    └── output/
        ├── workspace/            # 完整候选工作目录
        └── result.json
```

正式知识仍在原知识目录，Agent 的读取工具不暴露 templates/、jobs/ 或收件箱。每次尝试使用独立任务目录；模板版本安装后不变，升级使用新版本。

### 工作范围与快照边界

普通目录使用管理员显式配置的 root_path（相对 KB_ROOT）；Git 使用 repository_id + workspace_subpath，从当前发布目录解析，不读取正在变化的原 checkout。knowledge_subpath 必须映射到现有知识库挂载目录。

工作根只决定模板处理范围，不扩大 Agent Key 的读取范围。不填写 workspace 的旧绑定继续只使用挂载目录，绝不自动向父目录扩大。完整输入输出仅覆盖已授权且未排除的工作内容。

默认排除任意层级的 .git、.env、.env.*、.venv、__pycache__、.cache、.secrets、credentials，以及 .pem/.key；管理员可用 exclude_paths 追加相对文件或目录。该规则不会识别任意命名文件中的秘密，其他敏感内容需明确排除。排除项不会从原仓库删除，未来发布必须保留这些项和工作根以外的内容，不能用过滤后的候选目录直接覆盖整个仓库。

相同工作范围的检索视图共享 workspace_key，要求模板版本、实例配置和排除规则一致。当前已实现绑定一致性检查；实际更新调度、工作范围互斥锁和多视图原子发布仍由后续 worker/发布器实现。Git 发布还须以仓库为单位协调不同 workspace_subpath，禁止基于过期 commit 覆盖其他更新。

快照前检查版本，复制后核对摘要；Git 使用同一发布 commit 的文件。普通本地目录只有前后摘要检查，不等同于文件系统原子快照，需要外部写入方配合停写或版本化。每次解析会重新检查库映射，映射改变后旧配置不匹配则拒绝处理。

## 4. 程序接口

支持 initialize 和 update；统一传入三个目录：

```bash
python program.py update --input /job/input --work /job/work --output /job/output
```

initialize 接收空的工作快照和空材料列表；update 接收包含 sources、metadata 等辅助资料的工作快照。output 在运行前为空，程序只能把候选产物写入 output，把临时数据写入 work。正式容器运行时由挂载权限落实；当前本地测试器只有运行前后摘要检查，不提供操作系统隔离。

request.json 示例：

```json
{
  "protocol_version": "1.1",
  "job_id": "job-example",
  "operation": "update",
  "library_id": "team-guide",
  "base_version": "snapshot-content-sha256",
  "template": {
    "id": "reference-docs",
    "version": "0.2.0",
    "package_sha256": "installed-package-sha256"
  },
  "knowledge_subpath": "knowledge",
  "workspace_key": "platform-workspace-identity",
  "source_version": null,
  "exclude_paths": ["temporary"],
  "config": {}
}
```

示例中的摘要为占位说明，实际值为平台计算的 SHA-256。程序不自行选择库或改变模板版本。base_version 是排除策略生效后的完整工作快照摘要；Git 的 source_version 为同一次解析取得的 commit，普通目录为 null。knowledge_subpath 相对 workspace，workspace_key 标识共同工作范围。

materials.jsonl 每行是 `{"id":"服务端收件箱记录 ID","material":{标准材料}}`。material 复用 [收件箱协议](inbox-api.md)，kind 为 new_content / correction / observation。平台只传入目标知识库的已领取记录；测试器每次最多 100 条。

result.json 包含：

| 字段 | 约定 |
|---|---|
| protocol_version、job_id、library_id、base_version、template、knowledge_subpath、workspace_key、source_version | 必须与请求一致 |
| candidate_path | 协议 1.1 固定为 workspace，指向 output 下完整候选工作目录 |
| deleted_paths | 相对工作根的明确删除清单；默认空，未声明而遗漏原文件或目录会被拒绝 |
| summary | 本次处理说明 |
| materials | 每条输入记录恰好一条结论：record_id、outcome、summary、references |

outcome 沿用 accepted / partially_accepted / rejected；references 相对候选工作根，必须指向其中存在的普通文件；可以关联辅助文件，但不因此向消费 Agent 开放其读取权限，不允许越界。程序非零退出、超时或结果不合规均视为失败。

当前测试器要求候选的 knowledge_subpath 内包含 INDEX.md，拒绝链接、junction 和特殊文件；每棵输入/输出树最多 2000 个文件、32 MiB，result.json 最多 1 MiB。这些是原型约束，正式平台需将通用限制与模板声明的产物要求分别校验。

任务状态到 candidate_ready 仅说明候选通过协议校验；不代表知识真实或已发布。发布前必须再检查授权及基准版本。演示仅回填“候选已生成”的处理结论，不触发发布。

## 5. 测试模板与运行方法

模板程序：[reference-docs](../examples/templates/reference-docs/README.md)。

开发者包只有程序及说明；[模板注册配置示例](../examples/template-platform-config.json)放在包外，模拟平台注册信息。运行时把它写入平台数据库，并安装程序到 templates/，按库保存绑定。

在代码仓库根目录运行：

```bash
python -m folder_kb.template_demo
```

Windows 使用仓库已有虚拟环境：

```powershell
.venv/Scripts/python.exe -m folder_kb.template_demo
```

每次在 data/template-demo/run-<随机值>/ 创建全新的演示环境，包含独立知识目录、SQLite、模板安装目录和任务目录，不读取运行中服务的数据或环境密钥。

演示依次执行：

1. 注册并绑定 reference-docs@0.2.0，测试初始化入口。
2. 通过实际收件箱逻辑提交三种材料，并领取处理。
3. 创建完整工作快照，调用本地模板，保留 metadata 辅助文件并排除示例 .env。
4. 生成候选知识并回填逐条结果，任务标记 candidate_ready。
5. 输出 summary.json、候选工作目录与检索子目录路径，验证原目录未变、辅助资料保留且示例凭据未进入快照。

测试模板不调用大模型，只把材料整理成带来源、场景的待审核文档。纠错不覆盖原知识；同一记录重跑不重复追加；所有结论标为 partially_accepted，表示仅整理为候选审核材料。

演示从源码目录运行，示例模板不随当前 Python wheel 分发。调用器仅面向随仓库提供的可信模板；它不是沙箱，不能用来执行未知开发者代码。当前不实现通用入口调度、动态依赖安装、CPU/内存隔离、进程树回收和网络策略。模板注册与安装不会触发程序执行。

## 6. 后续实现顺序

| 顺序 | 交付内容 | 验收重点 |
|---|---|---|
| 1，已实现原型 | 文件协议、测试程序、元数据表、本地端到端演示 | 三类材料生成候选；输入不变；结果逐条对应；重复处理不重复追加 |
| 2，已实现首版 | 模板注册/ZIP 安装/绑定 API | 来源和固定版本校验；同版本不可替换；实例配置符合开发者 Schema |
| 3 | 独立 worker 与容器执行 | CPU/内存/超时限制、只读输入、凭据注入、失败与进程重启恢复 |
| 4 | 收件箱任务调度及审核发布 | 领取与任务事务衔接、并发更新冲突、当前授权检查、失败保留正式版本 |
| 5 | 管理 UI 与正式模板 | 注册绑定、运行记录、差异审核；开发者实现真正的提炼及更新逻辑 |

目前不需要新增面向消费 Agent 的 MCP 工具，沿用读取、提交和查询回执。模板管理和任务运行属于平台管理能力。

协议兼容：平台注册仍可保存 1.0 模板，但显式工作范围绑定要求 1.1。本地测试执行器当前验证 1.1，不会把旧程序当作新协议执行；收件箱材料 schema_version 仍为 1.0。
