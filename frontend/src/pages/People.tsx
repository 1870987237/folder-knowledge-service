import { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, App, Button, Form, Input, Select, Empty, Modal, Spin, Table, Tag } from 'antd';
import { useSession } from '../context';
import { Api, errorMessage } from '../api';
import { OrganizationMembers } from './OrganizationMembers';
import { GovernanceTable } from './Governance';
import { LoadError, PageHeader } from '../components';

export type Client = Pick<Api, 'request'>;
export type Organization = { id: string; name: string; role: string };
export type Identity = { id: string; username: string; platform?: boolean; organizations: Organization[] };
type Member = { id: string; username: string; role: string };
type Account = Member & { status: string; created_at: string };

export function Organizations({ api, identity, reload }: { api: Client; identity: Identity; reload: () => void }) {
  const [selected, setSelected] = useState(''), [creating, setCreating] = useState(false), [saving, setSaving] = useState(false), [error, setError] = useState('');
  const [form] = Form.useForm();
  const orgs = identity.organizations.filter(o => o.role === 'admin');
  const oid = orgs.some(o => o.id === selected) ? selected : orgs[0]?.id;
  return <div className="organizations-page">
    <div className="organization-toolbar"><Select className="organization-select" aria-label="选择组织" placeholder="选择管理的组织" value={oid} options={orgs.map(o => ({ label: o.name, value: o.id }))} onChange={setSelected} />
      {identity.platform && <Button onClick={() => { form.resetFields(); setError(''); setCreating(true); }}>新建组织</Button>}
    </div>
    {oid ? <OrganizationMembers key={oid} api={api} oid={oid} onChanged={reload} /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无可管理的组织" />}
    <Modal title="新建组织" open={creating} okText="创建" onCancel={() => setCreating(false)} onOk={() => form.submit()} confirmLoading={saving} closable={!saving} maskClosable={false} cancelButtonProps={{ disabled: saving }}>
      {error && <Alert type="error" title={error} className="block-gap" />}
      <Form form={form} layout="vertical" onFinish={async v => { setSaving(true); setError(''); try { const result = await api.request<{ id: string }>('/organizations', 'POST', v); setSelected(result.id); setCreating(false); reload(); } catch (e) { setError(errorMessage(e)); } finally { setSaving(false); } }}><Form.Item name="name" label="组织名称" rules={[{ required: true, whitespace: true }]}><Input maxLength={80} /></Form.Item></Form>
    </Modal>
  </div>;
}

function useAdminIdentity() {
  const { api } = useSession();
  const [identity, setIdentity] = useState<Identity>(), [error, setError] = useState('');
  const load = useCallback(async () => { setError(''); try { setIdentity(await api.portal<Identity>('/me')); } catch (e) { setError(errorMessage(e)); } }, [api]);
  useEffect(() => { void load(); }, [load]);
  const client = useMemo<Client>(() => ({ request: (p, m, b) => api.portal(p, m, b) }), [api]);
  return { api, client, identity, error, load };
}

export function OrganizationsPage() {
  const { client, identity, error, load } = useAdminIdentity();
  return <div className="admin-organizations-page" aria-label="组织管理">
    {error && <LoadError error={error} retry={load} />}
    {identity ? <Organizations api={client} identity={identity} reload={load} /> : !error && <Spin />}
  </div>;
}

export function OwnershipPage() {
  const { api, identity, error, load } = useAdminIdentity();
  return <><PageHeader title="知识库归属" />{error && <LoadError error={error} retry={load} />}
    {identity ? <GovernanceTable api={api} organizations={identity.organizations} reload={load} /> : !error && <Spin />}
  </>;
}

export function AccountsPage() {
  const { api } = useSession(); const { modal } = App.useApp();
  const [users, setUsers] = useState<Account[]>([]), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  async function load() { setBusy(true); setError(''); try { setUsers(await api.request<Account[]>('/accounts')); } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); } }
  useEffect(() => { void load(); }, [api]);
  return <><PageHeader title="账号管理" />{error && <LoadError error={error} retry={load} />}<Table<Account> size="small" rowKey="id" loading={busy} dataSource={users} pagination={{ hideOnSinglePage: true }} columns={[
    { title: '账号', dataIndex: 'username' },
    { title: '状态', render: (_, r) => <Tag>{({ active: '已启用', pending: '待审批', disabled: '已禁用' } as Record<string, string>)[r.status]}</Tag> },
    { title: '操作', render: (_, r) => <Button type="link" danger={r.status === 'active'} onClick={() => modal.confirm({ title: `${r.status === 'active' ? '禁用' : '启用'} ${r.username}？`, onOk: async () => { try { await api.request('/accounts/' + r.id, 'PUT', { status: r.status === 'active' ? 'disabled' : 'active' }); await load(); } catch (e) { setError(errorMessage(e)); throw e; } } })}>{r.status === 'active' ? '禁用' : '启用'}</Button> },
  ]} /></>;
}
