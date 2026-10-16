# 隔离并发负载测试

小规模评测默认使用 `bm25-markdown`，运行时需显式 `--trust-plugin`；历史旧插件成绩不改写。全量 BrowseComp-Plus 仍使用 `sqlite-corpus`。

这是接口/调度负载实验，与 Agent 自主任务 benchmark 分开。无需模型或 OMP。

```powershell
python -m benchmarks.load.run --output data/load-smoke --clients 1 5 --requests 3 --files 8 --file-bytes 1024 --trust-plugin
python -m benchmarks.load.run --output data/load-baseline --clients 1 5 10 25 50 100 --requests 10 --plugin-definition examples/plugins/bm25-markdown/definition.json --plugin-source examples/plugins/bm25-markdown/package --trust-plugin
```

- 每个场景、并发档位独立初始化合成库，各客户端使用不同 Key。
- HTTP 由真实 `python -m folder_kb` 子进程启动，默认并发限制 32；客户端与服务器在同机不同进程。日志只在忽略的运行目录。
- read：读取原文；search：查询已构建索引；mixed：约 20% 创建文件，其余读取，同库竞争；background：读取与独立更新 worker 的三个候选生成任务并行。
- 每客户端串行发送指定数量请求，无思考间隔，统一起跑，不自动重试。不同客户端并发。这是闭环突发负载，不等于相同数量的人类在线或固定到达速率。
- 后台使用 reference-docs 确定性模板；单独记录候选状态和负载结束后的等待时间，不自动审核发布。未测模型限流或 Git 网络性能。
- 报告包括成功率、状态码、超时、成功/总吞吐和 P50/P95/P99。成功请求延迟与全部请求延迟分开，防止快速 503 掩盖服务退化。
- 不改变生产并发限制，不接生产 URL，不读取私人资料；只有本轮生成的临时目录会被清理。

短时本机测试只能找瓶颈，不能推导生产 SLA。持续容量验收需要目标部署硬件、独占负载、更长时间、请求分布及可接受的成功率/延迟阈值。参数范围有上限，测试失败保留运行目录，不覆盖旧结果。
