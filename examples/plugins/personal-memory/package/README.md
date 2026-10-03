# 个人记忆插件示例

这是声明式插件的安装载荷。推荐由知识库管理者采用如下文件组织：

```text
INDEX.md
profile.md
projects/
decisions/
observations/
```

本示例不初始化目录，不生成记忆，也不构建索引。管理台展示清单中的索引规则和 Agent 指引；二者的执行消费者将在后续接入。

已有记忆更新程序仍可使用 update 入口，并在清单中附加 plugin 字段。
