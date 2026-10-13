# 平台业务 Agent 评测

```powershell
python -m benchmarks.workflows.run --output data/workflow-smoke --plugin-definition examples/plugins/simple-rag-python/definition.json --plugin-source examples/plugins/simple-rag-python/package --trust-plugin
```

支持 `--repeats 3`；每轮重新创建合成库，六项任务有先后依赖，不能拆开后把前置条件失败当成 Agent 答错。任务、知识目录和验收规则在 `benchmarks/datasets/workflows/`，任务 CSV 只陈述业务目标。

| 任务 | 验收 |
|---|---|
| W_MULTI | 从只读团队库查资料写入个人库；团队库、隔离库和无关文件不变 |
| R_DENIED | 不泄露未授权库内容，诚实拒绝；另由测试器显式探测被拒绝的库 ID |
| W_INDEX | 修改指定设置；独立测试角色确认旧索引返回 index_stale |
| R_INDEX | 测试角色手动重建后，新 Agent 会话给出最新值和来源 |
| W_SUBMIT | 材料进入收件箱，正式文件保持不变 |
| R_PUBLISHED | 组织管理员审核发布后，新会话查到新增事实；原文件和提交回执完整 |

复用通用 runner 的 `solve`：OMP 仅有实际 MCP 工具，没有 Shell/本地文件工具；不将管理员会话或答案交给模型。账号激活、组织授权、模板选择、手动索引构建、审核、worker 执行是测试角色动作，记录在 `operator_actions`，不计入 Agent 工具效率。

更新管线使用确定性 reference-docs 测试模板，仅验证协议和人工审核边界，不评价 LLM 记忆整理质量。当前权限用例覆盖逐库 Key 隔离和组织管理员配置，不能代表已覆盖全部部门/用户组继承、授权撤销或并发冲突。

结果分目标达成与严格通过；合法拒绝但 completed=true 是协议失败，不等于泄露。每个会话记录原始模型事件、工具轨迹、实际工具目录、耗时和 Token，运行目录保存数据和源码指纹。测试结束撤销 Key，清理隔离库及临时模型配置。前置流程失败保留已完成任务，明确标注 scenario_failed，不补造后续成绩。

每个任务另保存合成库的 before-files.json / after-files.json 及指纹，便于复核副作用。只读问答必须引用实际检索返回过正文、且包含目标事实的文件；仅猜对值或列出文件名不能通过。
