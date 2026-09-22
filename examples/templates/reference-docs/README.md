# reference-docs 测试模板

这是开发者提供的独立程序包，仅使用 Python 标准库，不依赖 Folder KB SDK 或大模型。

当前为模板 0.2.0、程序协议 1.1。支持 `initialize` 和 `update`，通过 `--input / --work / --output` 目录交换数据。平台为每次运行提供空的 output 目录。

初始化在 knowledge_subpath 下建立 INDEX.md；更新复制完整 input/workspace 到 output/workspace，保留 sources、metadata 等辅助资料，把标准材料整理到检索子目录下的 demo-inbox/，保留来源、场景和记录 ID。重复处理同一记录会覆盖同名候选文档，避免重复追加。

三类材料均标注“待审核”，纠错不会自动覆盖已有结论。处理结果为 partially_accepted，仅表示接收为审核材料，不代表知识有效或已经发布。此模板用于验证程序协议，不代表正式的知识整理策略。

模板元数据由平台注册，本目录仅保存代码。演示注册配置见 [平台配置示例](../../template-platform-config.json)，运行方法见 [模板协议](../../../docs/template-protocol.md)。
