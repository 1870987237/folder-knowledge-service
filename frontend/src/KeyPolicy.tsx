import { useEffect, useState } from 'react';
import { Alert, App, Checkbox, Form, Input, Select, Switch, Table, Typography } from 'antd';
import { Api, errorMessage } from './api';
import { AsyncForm, LoadError } from './components';

type Grant = { library_id: string; read: boolean; grep: boolean; search: boolean; write_policy: 'readonly' | 'submit' | 'direct' };
type Policy = { permissions: Grant[]; instructions: string; policy_revision: number };
type Library = { id: string; name: string };

export function KeyPolicy({ api, id, libraries, done }: { api: Api; id: string; libraries: Library[]; done: () => void }) {
  const [policy, setPolicy] = useState<Policy>(), [error, setError] = useState('');
  const { message } = App.useApp();
  const load = () => { setError(''); api.request<Policy>(`/keys/${id}/policy`).then(setPolicy).catch(e => setError(errorMessage(e))); };
  useEffect(load, [api, id]);
  if (error) return <LoadError error={error} retry={load} />;
  if (!policy) return <Typography.Text>正在读取权限…</Typography.Text>;
  return <AsyncForm<Policy> initial={policy} submitText="保存权限" submit={async values => {
    await api.request(`/keys/${id}/policy`, 'PUT', { permissions: values.permissions, instructions: values.instructions, expected_revision: policy.policy_revision });
    message.success('权限已更新，Agent 刷新工具或重新连接后可看到变化'); done();
  }}>
    <Form.Item name="permissions"><Matrix libraries={libraries} /></Form.Item>
    <Form.Item name="instructions" label="Agent 使用说明（instructions）"><Input.TextArea rows={3} maxLength={8000} placeholder="例如：这是个人记忆库，回答前先检索与当前问题相关的记忆。" /></Form.Item>
    <Typography.Paragraph type="secondary">提交进入收件箱；直接写入立即发布，仅限已开启该能力的平台文档库。Search 还需要绑定检索插件。</Typography.Paragraph>
  </AsyncForm>;
}

function Matrix({ libraries, value = [], onChange }: { libraries: Library[]; value?: Grant[]; onChange?: (value: Grant[]) => void }) {
  const row = (id: string): Grant => value.find(v => v.library_id === id) || { library_id: id, read: false, grep: false, search: false, write_policy: 'readonly' };
  function change(id: string, patch: Partial<Grant>) {
    const next = { ...row(id), ...patch };
    if (!next.read) Object.assign(next, { grep: false, search: false, write_policy: 'readonly' });
    onChange?.([...value.filter(v => v.library_id !== id), next]);
  }
  return <Table size="small" rowKey="id" dataSource={libraries} pagination={false} scroll={{ x: 640, ...(libraries.length > 6 ? { y: 340 } : {}) }} columns={[
    { title: '知识库', dataIndex: 'name' },
    ...(['read', 'grep', 'search'] as const).map((field, i) => ({ title: ['读取', 'Grep', 'Search'][i], width: 76,
      render: (_: unknown, l: Library) => <Checkbox aria-label={`${l.name} ${field}`} disabled={field !== 'read' && !row(l.id).read} checked={row(l.id)[field]} onChange={e => change(l.id, { [field]: e.target.checked })} /> })),
    { title: '写入方式', width: 160, render: (_, l) => <Select aria-label={`${l.name} 写入方式`} style={{ width: 136 }} disabled={!row(l.id).read} value={row(l.id).write_policy} onChange={v => change(l.id, { write_policy: v })} options={[{ value: 'readonly', label: '只读' }, { value: 'submit', label: '提交收件箱' }, { value: 'direct', label: '直接写入' }]} /> },
  ]} />;
}

type AgentSettings = { purpose: string; direct_enabled: boolean; direct_eligible: boolean };
export function LibraryAgentSettings({ api, id }: { api: Api; id: string }) {
  const [value, setValue] = useState<AgentSettings>(), [error, setError] = useState('');
  const { message } = App.useApp();
  const load = () => { setError(''); api.request<AgentSettings>(`/libraries/${id}/agent-settings`).then(setValue).catch(e => setError(errorMessage(e))); };
  useEffect(load, [api, id]);
  if (error) return <LoadError error={error} retry={load} />;
  if (!value) return <Typography.Text>加载中…</Typography.Text>;
  return <AsyncForm<AgentSettings> initial={value} submitText="保存 Agent 设置" submit={async v => {
    await api.request(`/libraries/${id}/agent-settings`, 'PUT', { purpose: v.purpose, direct_enabled: v.direct_enabled }); message.success('已保存'); load();
  }}>
    <Form.Item name="purpose" label="知识库用途"><Input.TextArea rows={3} maxLength={1000} /></Form.Item>
    <Form.Item name="direct_enabled" label="允许授权 Agent 直接写入" valuePropName="checked"><Switch disabled={!value.direct_eligible} /></Form.Item>
    {value.direct_eligible ? <Alert type="warning" title="获得直接写入授权的 Key 可立即发布文件，无需人工审核。" /> : <Typography.Text type="secondary">当前库通过挂载、Git 或更新插件维护，不支持直接写入。</Typography.Text>}
  </AsyncForm>;
}
