# 收件箱标准内容接口 v1

收件箱接收待整理材料，不直接修改正式知识。知识库管理者领取、整理并回填结果；后续更新程序可复用同一材料格式。

## 1. 标准材料

REST 的请求体与 MCP 的 `material` 参数使用同一 JSON：

```json
{
  "schema_version": "1.0",
  "submission_id": "feedback-20260922-001",
  "library_id": "team-guide",
  "kind": "correction",
  "title": "使用说明与实际行为有差异",
  "content": {
    "format": "markdown",
    "text": "## 实际观察\n在某条件下，操作结果与手册描述不同。\n## 建议\n请验证并补充适用条件。"
  },
  "source": {
    "reference": "session:example-001",
    "description": "用户在实际操作中反馈"
  },
  "context": {
    "product_version": "example-version",
    "scenario": "特定使用条件",
    "related_document": "guide/usage.md"
  }
}
```

| 字段 | 约定 |
|---|---|
| `schema_version` | 当前仅接受 `1.0`，省略时默认该值 |
| `submission_id` | 必填；调用方生成，重试沿用。1～128 字符，字母数字开头，允许字母、数字、`._:-` |
| `library_id` | 必填；目标知识库 ID，不是文件路径 |
| `kind` | `new_content` 新增内容、`correction` 纠错、`observation` 观察记录；默认 new_content，仅表示提交意图，不触发不同处理流程 |
| `title` | 可选，最多 200 字符 |
| `content` | 必填；format 为 text / markdown，默认 markdown；text 不能为空白，最多 32 KiB UTF-8，保留正文原始空白 |
| `source` | 可选；reference 最多 2048 字符，description 最多 1024 字符；只存引用，不访问链接或读取路径 |
| `context` | 可选；最多 16 个字符串键值对，键 1～64 字符，值最多 1024 字符；描述场景、版本等条件 |

服务端生成记录 `id`、`submitted_by`、接收/更新时间、处理状态；客户端不能指定这些字段。来源和场景属于提交者声明，未经验证。未知字段拒绝；不支持文件上传或附件内容。格式不要求知识库把正式知识组织成 QA、事实表或任何固定目录。

新增知识和新增记忆均使用 `new_content`；内容最终如何组织，由目标知识库及其模板决定，不由收件箱类别决定。

规范化材料最多 48 KiB UTF-8；HTTP 请求体（包括 JSON 包装）最多 64 KiB。完整机器可读约束见 [JSON Schema](schemas/material-v1.schema.json)；UTF-8 字节上限另由服务端校验。MCP 通过 tools/list 发布参数 schema，REST OpenAPI 在 `/openapi.json`（管理员认证）。

## 2. 提交与查询

仅管理模式（配置 `KB_ADMIN_TOKEN`、`KB_STATE_DIR`）提供。现有 Key 默认无提交权限。

| 调用方 | REST | MCP | 作用 |
|---|---|---|---|
| 获准提交的 Agent Key | POST `/api/v1/inbox/submissions` | `submit_material(material)` | 接收材料，返回 202 |
| 原提交 Key | GET `/api/v1/inbox/submissions/{id}` | `get_submission(item_id)` | 查询自己的材料和处理结果，仍需当前提交授权 |
| 管理员 | GET / PUT `/admin/api/keys/{key_id}/inbox-grants` | — | 查询/设置可提交的知识库，PUT 请求为 `{"library_ids":["team-guide"]}`；空列表撤销 |
| 管理员 | GET `/admin/api/inbox` | — | 按 library_id、status 过滤；limit 1～100，默认 50；返回 next_after 用于 after 续页 |
| 管理员 | GET `/admin/api/inbox/{id}` | — | 查看一份材料 |

授权示例（环境变量由部署方填写，KEY_ID 是管理员创建 Key 时返回的 ID）：

```bash
curl -X PUT "$BASE_URL/admin/api/keys/$KEY_ID/inbox-grants" \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"library_ids":["team-guide"]}'

curl -X POST "$BASE_URL/api/v1/inbox/submissions" \
  -H "Authorization: Bearer $AGENT_KEY" -H "Content-Type: application/json" \
  --data-binary @material.json
```

保存上面的标准材料为 material.json，并将 library_id 替换为已注册且获授权的库。MCP 参数为 `{"material": {上面的标准材料对象}}`。

提交返回示例（省略时间等字段）：

```json
{
  "id": "server-generated-record-id",
  "library_id": "team-guide",
  "submission_id": "feedback-20260922-001",
  "status": "pending",
  "submitted_by": {"type": "agent_key", "id": "server-resolved-key-id"},
  "duplicate": false,
  "knowledge_updated": false,
  "result": null
}
```

幂等范围是 **Key ID + 知识库 ID + submission_id**。同 ID、同规范化内容返回原记录及 `duplicate: true`；同 ID、不同内容返回 409，不能覆盖原材料。更正原提交应换新 ID，可在 context 中引用原记录。跨 Key 不去重，共享一个 Key 的客户端被视为同一个提交者。

## 3. 领取与结果回填

```text
pending → processing → completed
             │
             └─ 释放 → pending
```

| 管理接口 | 请求 | 结果 |
|---|---|---|
| POST `/admin/api/inbox/{id}/claim` | `{"lease_seconds":900}`，或 `{}`；30～3600 秒 | 原子领取，返回 claim_token 与 lease_until |
| POST `/admin/api/inbox/{id}/complete` | claim_token、outcome、summary，可附 references、version | 保存处理结论，状态 completed |
| POST `/admin/api/inbox/{id}/release` | `{"claim_token":"领取时返回的值"}` | 主动放回待处理队列，便于重试 |

complete 示例：

```json
{
  "claim_token": "token-from-claim",
  "outcome": "partially_accepted",
  "summary": "验证后采纳适用条件说明，其余意见不采纳。",
  "references": ["guide/usage.md"],
  "version": "git:example-commit"
}
```

outcome 为 accepted / partially_accepted / rejected。summary 必填，1～4000 字符；references 最多 32 条，每条 1～1024 字符；version 最多 200 字符。这些是管理者回填的结论和引用，服务不核验 Git 版本，也不将 completed 当作发布成功。

并发领取仅一个成功。租约过期后可重新 claim，原 claim_token 失效；无续租接口，超时处理者须重新领取。过期记录仍标为 processing，管理者通过 lease_until 判断是否可重领；不会自动执行任务。claim_token 只在领取响应返回，服务端仅存摘要。结果回填需有效租约；完成后不允许重新领取。

## 4. 权限、存储与模块边界

- **当前身份**：提交者由有效 Agent Key 解析；管理处理接口仅使用现有管理员令牌。没有新增组织或个人账号体系，也没有收件箱 UI。
- **独立提交授权**：位于 inbox_grants，不改变现有 Key 创建接口。当前提交范围必须是其库访问范围的子集；减少库授权会级联移除对应提交授权，再次增加库访问不会自动恢复提交权。撤销提交权限、停用库或撤销/过期 Key 后，提交与本人回执访问均被拒绝。
- **鉴权对接点**：`Inbox.authorize` 负责提交权限判定；后续由 lqj 的统一鉴权替换，并将管理员接口接入“处理材料”权限。当前尚不支持仅提交而不可读的 Key，不能当作完整六类权限实现。
- **隔离**：材料只进入 `KB_STATE_DIR/management.sqlite3`，不落在知识根目录，不会被 grep/read_file 检索或被 Git 发布覆盖。目录暂时不可用时，已启用库仍可接收反馈；库停用后不能提交。
- **首版持久化**：有限大小的文本正文、元数据及结果一起保存在 SQLite，便于幂等和原子提交；后续大文件/附件存储另定义，不在本版执行下载或解析。状态目录应与知识目录分离，沿用现有备份工具。
- **迁移**：启动时自动迁移，收件箱引入于 v3，模板元数据表引入于 v4，工作范围字段引入于 v5，保留原库和 Key，不默认开启提交。部署前备份，并同步升级 HTTP 服务和 Git worker；旧版本不能打开 v5 数据库。
- **审计**：提交成功（含重复）、授权变更、领取、释放及回填写审计，不包含正文、令牌或来源内容。收件箱操作暂不计入现有文件访问调用统计；失败访问审计及容量/保留策略待后续完善。
- **发布**：本版不调用 LLM、不启动知识库更新程序、不验证或发布知识。先由维护者通过已有工作目录与 Git 流程更新，再回填处理结果。

## 5. 错误与验证

REST：无效身份 401；缺少提交授权 403；找不到记录或他人记录 404；幂等冲突/领取冲突 409；请求体超限 413；字段或版本错误 422。MCP 对应工具调用失败返回 isError。

```bash
python -m pytest tests/test_inbox.py -q
```

测试覆盖默认只读、显式授权、跨 Key/库隔离、并发幂等、重启与备份、库目录故障、租约回收、结果回填、请求大小、v2 迁移和真实 MCP HTTP 调用。
