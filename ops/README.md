# Windows benchmark host

长时间全量评测由 Windows 任务计划程序直接启动虚拟环境中的 `pythonw.exe`，入口为 `ops/windows_benchmark_host.py`。不通过 PowerShell 包装。宿主验证自己没有控制台，工作流子进程使用 `CREATE_NO_WINDOW`，标准输入关闭，输出写入评测目录。

任务的工作目录设为仓库根目录，以当前用户普通权限运行即可。它是一次性任务，不配置周期触发器或失败自动重跑。不要把模型凭据值写入参数，只传既有模型配置文件路径。

参数：

```text
pythonw.exe -B ops/windows_benchmark_host.py
  --output <评测目录> --python <虚拟环境python.exe>
  --omp <omp.exe> --models-config <models.yml>
```

恢复既有评测时加 `--resume`；原工作流继续核对冻结计划、数据、代码和结果，已回答的题目不会重新作答。

若主调度器退出，但原 `--prepare-only` 子进程还活着，可以额外传 `--wait-pid` 和 `--wait-created`。后者是 Windows `GetProcessTimes` 的创建时间 FILETIME 整数，可用 `ProcessHandle(pid).created` 查询。宿主持有该进程句柄并校验创建时间，避免 PID 重用；仅在原子进程正常退出且索引发布记录、`index_ready` 状态均存在后接续。失败则停止。接管期间保留旧状态副本、更新工作流心跳，不重建索引，也不结束原子进程。

观察 `host.json`（托管状态）、`host.log`（宿主异常）、`workflow.json`（阶段）、各阶段 stdout/stderr 日志。心跳代表进程在线，文档/题目计数才代表实际进度。调度器退出码 0xC000013A 表示控制台中断退出，单凭这个代码不能确定触发来源。

宿主文件是运行基础设施，位于评测算法指纹范围外；冻结的 `benchmarks`、`folder_kb`、`tools` 源码仍由原工作流校验。宿主变更应单独提交并记录在报告中。
