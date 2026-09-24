import { errorMessage } from '../api';
import { useState } from 'react';
import { App, Button, Checkbox, DatePicker, Form, Input, Modal, Space, Table, Tag, Typography } from 'antd';
import { CopyOutlined, PlusOutlined } from '@ant-design/icons';
import { useSession, useResource } from '../context';
import { AsyncForm, date, keyState, LoadError, NameCell, PageHeader, required, Surface } from '../components';
import type { AgentKey } from '../types';
import type { Dayjs } from 'dayjs';

export function KeysPage() {
  const { api, overview, refresh } = useSession(), { message, modal } = App.useApp();
  const [creating, setCreating] = useState(false), [secret, setSecret] = useState('');
  const [editing, setEditing] = useState<AgentKey>();
  return <><PageHeader title="Agent 与密钥" action={<Button type="primary" icon={<PlusOutlined />} onClick={() => setCreating(true)}>生成 Key</Button>} />
    <Surface><Table<AgentKey> rowKey="id" dataSource={overview.keys} scroll={{ x: 900 }} pagination={overview.keys.length > 20 ? { pageSize: 20 } : false} columns={[
      { title: 'Agent', render: (_, k) => <NameCell name={k.agent_name} id={k.id} /> },
      { title: '可访问知识库', render: (_, k) => <Space wrap>{k.library_ids.length ? k.library_ids.map(id => <Tag key={id}>{overview.libraries.find(l => l.id === id)?.name || id}</Tag>) : <Typography.Text type="secondary">未授权</Typography.Text>}</Space> },
      { title: '状态', render: (_, k) => <div className="name-cell"><span><Tag color={keyState(k)[1]}>{keyState(k)[0]}</Tag></span><Typography.Text type="secondary">{k.expires_at ? date(k.expires_at) + ' 到期' : '无到期时间'}</Typography.Text></div> },
      { title: '最近访问', render: (_, k) => date(k.activity.last_seen) },
      { title: '操作', width: 190, render: (_, k) => k.revoked_at ? '—' : <Space wrap><Button type="link" onClick={() => setEditing(k)}>权限设置</Button><Button type="text" danger onClick={() => modal.confirm({ title: '撤销此 Key？', content: '撤销后无法恢复，原 Key 的后续请求将被拒绝。', okButtonProps: { danger: true }, onOk: async () => { try { await api.request(`/keys/${k.id}/revoke`, 'POST'); refresh(); message.success('Key 已撤销'); } catch (e) { message.error(errorMessage(e)); throw e; } } })}>撤销</Button></Space> },
    ]} /></Surface>
    <Modal title="生成 Agent Key" open={creating} onCancel={() => setCreating(false)} footer={null} destroyOnHidden><AsyncForm<{ agent_name: string; library_ids: string[]; expiry?: Dayjs }> submitText="生成密钥" submit={async values => {
      const result = await api.request<{ token: string }>('/keys', 'POST', { agent_name: values.agent_name, library_ids: values.library_ids, expires_at: values.expiry?.toISOString() || null }); setCreating(false); setSecret(result.token); refresh();
    }}><Form.Item name="agent_name" label="Agent 名称" rules={required}><Input maxLength={80} /></Form.Item><Form.Item name="library_ids" label="可访问知识库" rules={[{ required: true, type: 'array', min: 1, message: '至少选择一个知识库' }]}><Checkbox.Group className="checkbox-stack" options={overview.libraries.map(l => ({ value: l.id, label: l.name + (l.enabled ? '' : '（已停用）') }))} /></Form.Item><Form.Item name="expiry" label="到期时间（可选）"><DatePicker showTime className="full-width" /></Form.Item></AsyncForm></Modal>
    <Modal title="Key 已生成" open={!!secret} onCancel={() => setSecret('')} footer={<Button type="primary" icon={<CopyOutlined />} onClick={async () => { try { await navigator.clipboard.writeText(secret); message.success('已复制'); } catch { message.error('复制失败，请手动选中密钥复制。'); } }}>复制密钥</Button>}><Typography.Paragraph>关闭后无法再次查看，请保存到 Agent 的配置中。</Typography.Paragraph><pre className="secret-value">{secret}</pre><Typography.Text code>{location.origin}/mcp</Typography.Text></Modal>
    <Modal title={'权限设置 · ' + (editing?.agent_name || '')} open={!!editing} onCancel={() => setEditing(undefined)} footer={null} width={640} destroyOnHidden>{editing && <GrantsForm key={editing.id} agent={editing} done={() => setEditing(undefined)} />}</Modal>
  </>;
}
interface Permissions { library_ids: string[]; submit_library_ids: string[] }
function GrantsForm({ agent, done }: { agent: AgentKey; done: () => void }) {
  const { api, refresh } = useSession(), { message } = App.useApp();
  const resource = useResource<Permissions>(`/keys/${agent.id}/permissions`);
  if (resource.error) return <LoadError error={resource.error} retry={resource.refresh} />;
  if (!resource.data) return <Typography.Text>正在读取权限…</Typography.Text>;
  return <AsyncForm<{ permissions: Permissions }> initial={{ permissions: resource.data }} submitText="保存权限" submit={async values => {
    await api.request(`/keys/${agent.id}/permissions`, 'PUT', values.permissions);
    refresh(); done(); message.success('权限已更新');
  }}>
    <Form.Item name="permissions"><PermissionMatrix /></Form.Item>
    <Typography.Text type="secondary">提交需先允许访问；提交内容进入收件箱。</Typography.Text>
  </AsyncForm>;
}
function PermissionMatrix({ value, onChange }: { value?: Permissions; onChange?: (value: Permissions) => void }) {
  const { overview } = useSession();
  const permissions = value || { library_ids: [], submit_library_ids: [] };
  function change(id: string, type: 'read' | 'submit', checked: boolean) {
    const reads = new Set(permissions.library_ids), submits = new Set(permissions.submit_library_ids);
    if (type === 'read') {
      if (checked) reads.add(id); else { reads.delete(id); submits.delete(id); }
    } else if (checked && reads.has(id)) submits.add(id); else submits.delete(id);
    onChange?.({ library_ids: [...reads], submit_library_ids: [...submits] });
  }
  return <Table rowKey="id" dataSource={overview.libraries} size="small" pagination={false} scroll={{ y: 360 }} columns={[
    { title: '知识库', render: (_, library) => <NameCell name={<>{library.name}{!library.enabled && <Tag>已停用</Tag>}</>} id={library.id} /> },
    { title: '允许访问', width: 100, align: 'center', render: (_, library) => <Checkbox aria-label={library.name + '允许访问'} checked={permissions.library_ids.includes(library.id)} onChange={e => change(library.id, 'read', e.target.checked)} /> },
    { title: '允许提交', width: 100, align: 'center', render: (_, library) => <Checkbox aria-label={library.name + '允许提交'} disabled={permissions.library_ids.includes(library.id) ? undefined : true} checked={permissions.submit_library_ids.includes(library.id)} onChange={e => change(library.id, 'submit', e.target.checked)} /> },
  ]} />;
}
