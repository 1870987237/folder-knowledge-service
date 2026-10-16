# 基础读写测试集

24 篇合成文档、12 个自然语言任务。测试数据与执行代码分开维护。

```text
basic/
├── knowledge/          # 初始知识库：普通 UTF-8 文本文件，可直接浏览编辑
│   ├── projects/       # 项目当前安排与历史版本
│   ├── policies/       # 制度与执行条件
│   ├── catalog/        # 服务目录
│   ├── limits/         # 服务限制
│   ├── memory/         # 个人偏好
│   ├── settings/       # 连接设置
│   ├── status/         # 服务验收状态
│   ├── notes/          # 交接记录
│   ├── scratch/        # 临时文件与需要保留的文件
│   └── reference/      # 背景资料
├── tasks.csv           # 任务清单，可用 Excel 编辑
└── expectations.json   # 按任务编号关联的验收规则，仅评分端读取
```

平台只导入 `knowledge/`。CSV、验收规则和本说明不进入知识库。每次任务都使用初始文档的新副本，Agent 的修改不会写回这里。

## 编辑任务

CSV 一条记录对应一个任务，保存为 **CSV UTF-8**。当前文件带 BOM，方便 Excel 识别中文；程序也接受不带 BOM 的 UTF-8。任务正文中的换行属于同一个单元格。

| 字段 | 含义 |
|---|---|
| `id` | 唯一编号；`R` 开头表示问答，`W` 开头表示写入或维护。允许英文字母、数字、下划线、短横线 |
| `kind` | 任务类别，例如 `lookup`、`precise_edit`，用于标记和分析 |
| `prompt` | 完整的自然语言任务，原样交给 Agent；不规定调用哪个工具 |
| `write_policy` | `direct` 允许直接写入；`readonly` 禁止写入。它是 Key 权限，不是任务类型 |

基线中普通问答也使用 `direct`，以检验 Agent 是否会误改资料；只有 W06 使用 `readonly` 验证拒绝越权写入。改变这些权限会改变测试条件。

新增任务时，在 CSV 加一条记录，并在 `expectations.json` 增加同编号的验收规则。CSV 中明确列出需要返回的 `facts` 字段名称，标准值仅写在验收规则中。

## 验收规则

```json
{
  "R07": {
    "answer_fields": {"owner": "林澈"},
    "source": "projects/白鹭/发布计划.md"
  },
  "W07": {
    "contains": {"memory/新记录.md": ["必须保留的内容"]}
  }
}
```

| 字段 | 检查内容 |
|---|---|
| `answer_fields` | 最终答复 `facts` 中的字段值 |
| `source` | 必须引用的知识库相对路径 |
| `expected_files` | 文件最终完整内容；值为 `null` 表示文件应删除 |
| `contains` / `absent` | 指定文件必须存在，并包含 / 不包含哪些文本 |
| `refuse` | `true` 表示应明确拒绝或说明资料不足，且 `completed=false` |
| `facts` | 兼容正文关键词验收；有 `answer_fields` 时不重复检查它 |

文件路径用 `/` 分隔。未列在文件验收规则中的文档一律不得改动。现有评分器还检查完成状态与真实结果是否一致；`R` 类任务必须实际调用读取或检索工具取证。

现有评分器适用于确定性事实与文件变化。开放式长文质量等新验收方式仍需扩展评分代码，不能只加 CSV 即自动判分。

## 使用其他测试集

复制整个 `basic/`，修改知识文件、任务和验收规则，从仓库根目录指定新目录：

```powershell
python -m benchmarks.agent.runner --dataset benchmarks/datasets/my-suite --output data/my-suite-run --trust-plugin
```

当前支持 UTF-8 文本文件，不支持二进制附件、符号链接和目录联接；空目录不作为验收对象。任务与答案编号、字段格式和引用来源会在启动服务和调用模型之前校验。

运行结果记录知识、任务与验收规则指纹。重评分使用同一测试集：

```powershell
python -m benchmarks.agent.regrade data/my-suite-run --dataset benchmarks/datasets/my-suite
```

文档或任务改变后应建立新基线；重评分拒绝使用不同知识内容或任务。若只修订验收规则，会在新评分文件记录修订后的指纹并保留原评分。
