# Agent 文件事务

用于已开启「直接写入」的平台原生知识库。沿用 Key 的 `write_policy=direct`，创建、替换和删除共用这项权限；只读和收件箱权限不能使用变更接口。Git 库和带更新程序的插件库使用各自更新流程。

## 接口

| MCP 工具 | REST | 用途 |
|---|---|---|
| `write_file` | `POST /api/v1/write` | 单文件创建/替换，兼容原参数 |
| `file_manifest` | `GET /api/v1/files/manifest` | 递归版本清单，不返回正文 |
| `apply_changes` | `POST /api/v1/files/changes` | 有序批量建目录、写入、删除，一次发布 |

REST 使用 `Authorization: Bearer <Agent Key>`。多库 Key 必须指定 `library_id`。MCP 在存在直接写入授权时展示上述工具；所有调用实时重新鉴权。清单是读取操作，REST 按读取权限校验。

### 单文件

`write_file` 的 `operation` 为 `create` 或 `replace`。创建必须不存在；替换必须带 `read_file` 或清单返回的 `expected_version`。新增 `create_parents` 默认为 false，仅创建时可设为 true。

所有变更请求带 `request_id`：1–128 字符，匹配 `^[A-Za-z0-9][A-Za-z0-9._:-]*$`。建议生成 UUID，不要直接使用中文文件名。超时后原样重试复用同一个 ID；修改内容或重新读取版本后使用新 ID。同一 Key 下 ID 跨两个变更接口共用，重复且相同的请求返回原回执，不同请求返回冲突。撤权后不能凭旧回执绕过权限。

### 清单与批量替换

清单参数：`library_id`、`path`（默认 `.`）、`offset`（默认 0）、`limit`（默认 100，最多 200），可带 `expected_library_version`。

返回 `library_version`、`entries`、`next_offset`。文件条目包括 `path/type/version/sha256/bytes`，目录条目包括 `path/type`。不返回服务器物理路径和正文。翻页时带首批的 `expected_library_version`；发生变化则重新获取。

先获取清单，再将整库版本放入批量请求：

```json
{
  "library_id": "my-library",
  "request_id": "5e88081a-9ee7-49d4-8349-bdfbe20d3c7b",
  "expected_library_version": "<file_manifest 返回的 library_version>",
  "changes": [
    {"operation": "mkdir", "path": "notes/project", "create_parents": true},
    {"operation": "create", "path": "notes/project/overview.md", "content": "# 项目概览\n"},
    {"operation": "replace", "path": "README.md", "content": "# 知识库\n"},
    {"operation": "delete", "path": "test-root.md"}
  ]
}
```

以上假定 README.md 和 test-root.md 已存在。纯新增可省略整库版本。按数组顺序执行，各路径最多出现一次；不允许同批删除父目录并操作其子路径。

| 操作 | 约束 |
|---|---|
| `mkdir` | 已有同名目录可成功；已有文件则冲突。可带 create_parents |
| `create` | 路径必须不存在；可带 create_parents |
| `replace` | 路径必须是已有文件；需 expected_version 或整库 expected_library_version |
| `delete` 文件 | 需 expected_version 或整库 expected_library_version |
| `delete` 目录 | 必须提供整库 expected_library_version；非空时另需 recursive=true |

两种版本都提供时必须都匹配。版本变化返回 409，不自动覆盖。删除整个库根目录不被允许。批量回执包含 `results`（各操作路径、类型、写入后的文件版本）、新的 `library_version`、发布时间和 `duplicate`。

## 原子性与限制

- 每批 1–100 项，正文 UTF-8 合计最多 1 MiB；单文件最多 256 KiB，仅 `.md` / `.txt`。JSON 请求体另有 2 MiB 上限，大量转义导致超限时需拆批。
- 原生库现有限制保持：最多 2000 个文件及目录、总内容最多 32 MiB。不适用于任意代码/二进制仓库上传。
- 路径相对知识库根，使用 `/`，最多 1024 字符、64 层；禁止隐藏路径、上跳、绝对路径、链接、保留文件名。同批路径大小写变体也视为重复。
- 全批验证并修改候选快照，最后以 SQLite 事务切换一次库指针并保存回执。任一项失败，活动库不变，候选快照清理。
- 全批只复制一次；复用初始快照，仍需最终候选校验与源库变化校验。成本仍随库大小增长，尚未实现按文件增量存储。较大批次会占用 SQLite 写事务，百人并发能力需另行压测。
- 发布后此前文件版本可能全部变化，后续批次应重新取清单。内容变化会使检索索引失效，需重建索引。
- 权限允许直接发布，无人工审核；需要审核的材料继续走收件箱。一次请求只针对一个库，不支持跨库事务。

28 个已有文件的批量替换，在一页清单足够的情况下只需「清单 + 批量提交」两次调用和一次快照复制。跨批次不保证整体原子性。

## 常见错误

| 错误 | 含义与处理 |
|---|---|
| `parent_not_found`（404） | 父目录不存在；先 mkdir 或创建时带 create_parents |
| `parent_not_directory`（409） | 父路径某一层是文件，修正路径 |
| `file_not_found`（404） | 要替换/删除的路径不存在（旧文件版本校验也可能先报 version_changed） |
| `file_exists`（409） | 创建目标已存在，或 mkdir 被文件占用 |
| `directory_not_empty`（409） | 删除非空目录需显式 recursive=true |
| `version_changed`（409） | 文件/整库已变化，重新获取并评估变更 |
| `request_conflict`（409） | 此 request_id 已用于不同请求 |
| `invalid_path`（400） | 路径格式不允许 |
| 参数校验失败（REST 422） | request_id、批量大小、正文、操作参数等不符合约定 |

## 历史清理（服务端运维）

默认不自动清理。仅处理 Agent 文件写入回执中已知的旧 `document-releases/<uuid>` 快照，不扫描删除任意知识目录。

```bash
# 默认只预览，不删除
python -m folder_kb.release_gc --root /srv/knowledge --state-dir /srv/state

# 保留各库最近 20 次 Agent 发布，只清理至少 7 天前的无引用快照
python -m folder_kb.release_gc --root /srv/knowledge --state-dir /srv/state --keep 20 --min-age-days 7 --apply
```

`--root/--state-dir` 也可由 `KB_ROOT/KB_STATE_DIR` 提供。最少保留 1 次发布，最短年龄 1 天。活动库、覆盖该目录的其他挂载、草稿、模板及其他管理记录引用的快照都会保留；引用判断采用保守策略，可能多保留。执行删除时持有发布使用的写锁，建议在低峰运行。

删除旧快照会失去该快照的文件内容，先预览并按需备份。幂等回执和审计记录保留；重试仍返回原回执，但不恢复已清理版本。正在使用旧版本的跨请求读取可能需要从当前版本重新开始。此命令不清理草稿历史、Git 历史或进程崩溃遗留的未知目录。
