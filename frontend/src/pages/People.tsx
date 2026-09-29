import { useEffect, useState } from 'react';
import { Alert, App, Button, Card, Form, Input, Select, Space, Table, Tabs, Tag } from 'antd';
import { useSession } from '../context';
import { Api, errorMessage } from '../api';
import { PageHeader } from '../components';

export type Client = Pick<Api, 'request'>;
export type Organization = { id: string; name: string; role: string };
export type Identity = { id: string; username: string; platform?: boolean; organizations: Organization[] };
type Member = { id: string; username: string; role: string };
type Account = Member & { status: string; created_at: string };

export function Organizations({ api, identity, reload }: { api: Client; identity: Identity; reload: () => void }) {
  const [oid, setOid] = useState(''), [members, setMembers] = useState<Member[]>([]), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const [form] = Form.useForm(); const { modal } = App.useApp();
  const orgs = identity.organizations.filter(o => o.role === 'admin');
  async function load(id: string) { setOid(id); setBusy(true); setError(''); try { setMembers(await api.request<Member[]>(`/organizations/${id}/members`)); } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); } }
  return <Space orientation="vertical" className="full-width" size="large">
    {error && <Alert type="error" title={error} />}
    {identity.platform && <Form layout="inline" onFinish={async (v: { name: string }) => { setBusy(true); setError(''); try { await api.request('/organizations', 'POST', v); reload(); } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); } }}><Form.Item name="name" rules={[{ required: true, whitespace: true }]}><Input aria-label="组织名称" placeholder="新组织名称" maxLength={80} /></Form.Item><Button htmlType="submit" loading={busy}>创建组织</Button></Form>}
    <Select className="organization-select" aria-label="选择组织" placeholder="选择管理的组织" value={oid || undefined} options={orgs.map(o => ({ label: o.name, value: o.id }))} onChange={id => { void load(id); form.resetFields(); }} />
    {oid && <><Form form={form} layout="inline" initialValues={{ role: 'member' }} onFinish={async v => { setBusy(true); setError(''); try { await api.request(`/organizations/${oid}/members`, 'PUT', v); await load(oid); form.resetFields(); } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); } }}><Form.Item name="username" rules={[{ required: true }]}><Input placeholder="已启用的账号" aria-label="成员账号" /></Form.Item><Form.Item name="role"><Select className="role-select" aria-label="组织角色" options={[{ value: 'member', label: '成员' }, { value: 'admin', label: '管理员' }]} /></Form.Item><Button htmlType="submit" loading={busy}>保存成员</Button></Form>
    <Table<Member> rowKey="id" dataSource={members} loading={busy} columns={[{ title: '账号', dataIndex: 'username' }, { title: '角色', render: (_, r) => r.role === 'admin' ? '组织管理员' : '成员' }, { title: '操作', render: (_, r) => <Space><Button type="link" onClick={() => form.setFieldsValue(r)}>设置角色</Button><Button type="link" danger onClick={() => modal.confirm({ title: `移出 ${r.username}？`, content: '该账号及其 Agent Key 将失去此组织的权限。', onOk: async () => { try { await api.request(`/organizations/${oid}/members`, 'PUT', { username: r.username, role: 'remove' }); await load(oid); } catch (e) { setError(errorMessage(e)); throw e; } } })}>移出</Button></Space> }]} /></>}
  </Space>;
}

export function PeoplePage() {
  const { api } = useSession(); const { modal } = App.useApp();
  const [users, setUsers] = useState<Account[]>([]), [identity, setIdentity] = useState<Identity>(), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const [libraries, setLibraries] = useState<{ id: string; name: string; owner_id?: string; organization_id?: string }[]>([]);
  async function load() { setBusy(true); setError(''); try { const [u, me, libs] = await Promise.all([api.request<Account[]>('/accounts'), api.portal<Identity>('/me'), api.portal<typeof libraries>('/libraries')]); setUsers(u); setIdentity(me); setLibraries(libs); } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); } }
  useEffect(() => { void load(); }, [api]);
  const client: Client = { request: (p, m, b) => api.portal(p, m, b) };
  return <><PageHeader title="账号与组织" action={<Button href="/workspace" target="_blank">打开用户工作区</Button>} />{error && <Alert type="error" title={error} action={<Button onClick={load}>重试</Button>} />}<Card><Tabs items={[
    { key: 'accounts', label: '账号', children: <Table<Account> rowKey="id" loading={busy} dataSource={users} columns={[{ title: '账号', dataIndex: 'username' }, { title: '状态', render: (_, r) => <Tag>{({ active: '已启用', pending: '待审批', disabled: '已禁用' } as Record<string, string>)[r.status]}</Tag> }, { title: '操作', render: (_, r) => <Button type="link" danger={r.status === 'active'} onClick={() => modal.confirm({ title: `${r.status === 'active' ? '禁用' : '启用'} ${r.username}？`, onOk: async () => { try { await api.request('/accounts/' + r.id, 'PUT', { status: r.status === 'active' ? 'disabled' : 'active' }); await load(); } catch (e) { setError(errorMessage(e)); throw e; } } })}>{r.status === 'active' ? '禁用' : '启用'}</Button> }]} /> },
    { key: 'organizations', label: '组织', children: identity && <Organizations api={client} identity={identity} reload={load} /> },
    { key: 'adopt', label: '存量库归属', children: <Form layout="vertical" onFinish={async v => { setBusy(true); setError(''); try { await api.request(`/libraries/${v.library_id}/ownership`, 'PUT', { organization_id: v.organization_id }); await load(); } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); } }}><Form.Item name="library_id" label="未分配的知识库" rules={[{ required: true }]}><Select options={libraries.filter(l => !l.owner_id && !l.organization_id).map(l => ({ value: l.id, label: l.name }))} /></Form.Item><Form.Item name="organization_id" label="所属组织" rules={[{ required: true }]}><Select options={identity?.organizations.map(o => ({ value: o.id, label: o.name }))} /></Form.Item><Button type="primary" htmlType="submit" loading={busy}>分配归属</Button></Form> },
  ]} /></Card></>;
}
