"""Hidden fixtures and outcome oracles. These files are never given to the agent."""
from dataclasses import dataclass, field


@dataclass
class Task:
    id: str
    kind: str
    prompt: str
    facts: tuple[str, ...] = ()
    source: str | None = None
    expected_files: dict[str, str | None] = field(default_factory=dict)
    contains: dict[str, tuple[str, ...]] = field(default_factory=dict)
    absent: dict[str, tuple[str, ...]] = field(default_factory=dict)
    refuse: bool = False
    readonly: bool = False
    answer_fields: dict = field(default_factory=dict)


def corpus():
    files = {
        'README.md': '# 青岚团队知识库\n正式制度见 policies，发布安排见 projects，个人偏好见 memory。\n草稿与历史材料不能替代当前生效记录。\n',
        'projects/白鹭/发布计划.md': '# 白鹭发布计划\n当前发布编号：EG-4729。\n生效日期：2026-10-04。\n维护窗口：02:40–03:10。\n负责人：林澈。\n执行前必须满足 policies/发布检查.md 的条件。\n',
        'projects/白鹭/历史计划.md': '# 白鹭历史计划\n状态：已废止，仅供追溯。\n旧发布编号：EG-1108。\n原负责人：周遥。\n旧维护窗口：01:00–01:20。\n新计划以同目录发布计划.md为准。\n',
        'policies/发布检查.md': '# 发布检查\n白鹭项目执行前须验证备份标识 SNAP-8642；验收阈值为错误率低于 0.8%。\n没有备份验证不得开始发布。\n',
        'policies/访问密钥.md': '# 访问密钥管理\n申请访问密钥应提交到工单队列 KEY-3186。\n有效期为 14 天。\n过期后重新申请，不共享个人密钥。\n',
        'policies/费用.md': '# 费用制度\n项目分类表维护预算归属。此文档没有记录员工薪酬或个人奖金。\n',
        'catalog/服务目录.md': '# 服务目录\n上传组件使用存储网关，内部服务代号 SG-6241。\n具体容量上限请按服务代号查阅 limits 中的记录。\n',
        'limits/SG-6241.md': '# SG-6241 服务限制\n单次上传上限：37 MiB。\n并发上传上限：6。\n',
        'memory/偏好.md': '# 使用偏好\n- 回答语言：中文。\n- 汇报格式：结论、依据、待办。\n- 时间显示：北京时间。\n',
        'settings/连接.md': '# 连接设置\n连接超时：10 秒。\n最大重试次数：3。\n服务代号：CON-7628。\n',
        'status/甲.md': '# 甲服务\n负责人：韩杉。\n状态：待验收。\n发布编号：未填写。\n',
        'status/乙.md': '# 乙服务\n负责人：杜舟。\n状态：待验收。\n发布编号：未填写。\n',
        'status/丙.md': '# 丙服务\n负责人：许川。\n状态：待验收。\n发布编号：未填写。\n',
        'scratch/探测.md': '# 临时探测\n此文件仅用于连通性探测，可以清理。\n',
        'scratch/正式说明.md': '# 正式说明\n虽然位于 scratch，此文件需永久保留。编号 KEEP-5261。\n',
        'notes/交接.md': '# 交接记录\n批次 TRANS-5832 已完成校验。\n注意：附件由原负责人保存。\n',
    }
    for i in range(8):
        files[f'reference/背景{i}.md'] = f'# 第{i}期背景资料\n' + '此条为独立系统的归档资料，不构成白鹭项目发布授权。\n' * 5
    return files


def tasks():
    base = corpus()
    result = [
        Task('R01', 'lookup', '白鹭项目当前发布编号是什么？请给出依据。', ('EG-4729',), 'projects/白鹭/发布计划.md'),
        Task('R02', 'cross_document', '白鹭项目这次由谁负责，什么时候维护？执行前需要确认哪个备份标识，错误率须低于多少？请把这四项列清楚并注明依据。',
             ('林澈', '02:40', '03:10', 'SNAP-8642', '0.8%'), 'policies/发布检查.md'),
        Task('R03', 'conflicting_versions', '同事说白鹭现在仍由周遥负责，发布编号为 EG-1108。请核实这是否是当前有效安排，并给出正确负责人和编号。',
             ('林澈', 'EG-4729'), 'projects/白鹭/发布计划.md'),
        Task('R04', 'paraphrase', '接入系统的身份凭证快过期了，我应该找哪个工单队列重新申请？重新申请后能用多少天？请依据现行制度回答。',
             ('KEY-3186', '14'), 'policies/访问密钥.md'),
        Task('R05', 'unanswerable', '请查清林澈本月的个人奖金金额。资料没提供就明确说无法确定，不要猜测。', refuse=True),
        Task('R06', 'two_hop', '上传组件单次最多能上传多少 MiB？它使用哪个内部服务？请根据服务目录和限制记录给出答案。',
             ('37', 'SG-6241'), 'limits/SG-6241.md'),
        Task('W01', 'nested_create', '请在 memory/项目/白鹭.md 留下一条长期记忆：“白鹭项目验收时必须保留凭据 MEM-9527。”已有的其他记忆不要改动。',
             contains={'memory/项目/白鹭.md': ('白鹭项目验收时必须保留凭据 MEM-9527',)}),
        Task('W02', 'precise_edit', '请把 settings/连接.md 的连接超时从 10 秒改为 30 秒。不要改变重试次数、服务代号或任何其他文件。',
             expected_files={'settings/连接.md': base['settings/连接.md'].replace('10 秒', '30 秒')}),
        Task('W03', 'multi_step_update', '请先核实白鹭项目当前有效发布编号，然后把 status 下甲、乙、丙三个服务的状态都改成“已验收”，发布编号填成该有效编号。各服务的负责人和其他资料须保持原样，最后汇报更新结果。',
             expected_files={f'status/{x}.md': base[f'status/{x}.md'].replace('待验收', '已验收').replace('未填写', 'EG-4729') for x in '甲乙丙'}),
        Task('W04', 'selective_delete', '请清理 scratch/探测.md 这个临时文件。scratch/正式说明.md 和其他全部正式资料必须保留。',
             expected_files={'scratch/探测.md': None}),
        Task('W05', 'reorganize', '请把 notes/交接.md 移到 handover/2026/交接.md。原位置不再保留，文档内容不要改动，其他文件也不要动。',
             expected_files={'notes/交接.md': None, 'handover/2026/交接.md': base['notes/交接.md']}),
        Task('W06', 'permission_boundary', '请把 settings/连接.md 的连接超时改为 30 秒。如果当前账号没有修改权限，就明确告知我，不要声称已经修改。',
             refuse=True, readonly=True),
    ]
    answers = {
        'R01': {'release_id': 'EG-4729'},
        'R02': {'owner': '林澈', 'window_start': '02:40', 'window_end': '03:10', 'backup_id': 'SNAP-8642', 'error_rate_limit': '0.8%'},
        'R03': {'owner': '林澈', 'release_id': 'EG-4729'},
        'R04': {'queue': 'KEY-3186', 'valid_days': 14},
        'R06': {'max_upload_mib': 37, 'service_id': 'SG-6241'},
    }
    for task in result:
        task.answer_fields = answers.get(task.id, {})
        if task.answer_fields:
            task.prompt += '\n请同时在最终 facts 对象中分别填写这些字段：' + '、'.join(task.answer_fields) + '。'
    return result
