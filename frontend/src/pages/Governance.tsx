import { useEffect, useState } from 'react';
import { Alert, App, Button, Modal, Select, Space, Table, Tag, Typography } from 'antd';
import { Api, errorMessage } from '../api';
import { LoadError } from '../components';
import type { Organization } from './People';

type Client = Pick<Api, 'request'>;
type Library = { id: string; name: string; owner_id?: string; organization_id?: string; revision: number; enabled: boolean };
export function GovernanceTable({ api, organizations, reload }: { api: Api; organizations: Organization[]; reload: () => void }) {
  const [rows, setRows] = useState<Library[]>([]), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<Library>(), [target, setTarget] = useState(''), [saving, setSaving] = useState(false), [editError, setEditError] = useState('');
  const { message } = App.useApp();
  async function load() { setBusy(true); setError(''); try { setRows(await api.request('/library-governance')); } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); } }
  useEffect(() => { void load(); }, [api]);
  return <>{error && <LoadError error={error} retry={load} />}<Table size="small" rowKey="id" loading={busy} dataSource={rows} pagination={{ hideOnSinglePage: true }} columns={[
    { title: '知识库', render: (_, r) => <>{r.name}{!r.enabled && <Tag>已停用</Tag>}</> },
    { title: '当前归属', render: (_, r) => r.owner_id ? '个人私有' : organizations.find(o => o.id === r.organization_id)?.name || (r.organization_id ? r.organization_id : '平台托管') },
    { title: '操作', render: (_, r) => r.owner_id ? '—' : <Button type="link" onClick={() => { setEditing(r); setTarget(r.organization_id || ''); setEditError(''); }}>设置归属</Button> },
  ]} />
    <Modal title={'设置归属 · ' + (editing?.name || '')} open={!!editing} onCancel={() => setEditing(undefined)} okText="确认变更" confirmLoading={saving} closable={!saving} maskClosable={!saving} cancelButtonProps={{ disabled: saving }} onOk={async () => {
      if (!editing) return; setSaving(true); setEditError('');
      try { await api.request(`/libraries/${editing.id}/governance`, 'PUT', { organization_id: target || null, expected_revision: editing.revision }); setEditing(undefined); await load(); reload(); message.success('归属已更新'); }
      catch (e) { setEditError(errorMessage(e)); } finally { setSaving(false); }
    }}>
      <Space orientation="vertical" className="full-width" size="middle">
        <Select className="full-width" aria-label="目标归属" value={target} onChange={setTarget} options={[{ value: '', label: '平台托管（移出组织）' }, ...organizations.map(o => ({ value: o.id, label: o.name }))]} />
        <Alert type="warning" title="变更归属会清除本库的成员和 Key 授权，关闭全员可读、直接写入及自动增量触发。文件与历史保留，新归属下需重新授权。" />
        {editError && <Alert type="error" title={editError} />}
      </Space>
    </Modal>
  </>;
}

type Sharing = { organization_access: 'restricted' | 'org_read'; revision: number };
export function SharingSettings({ api, id }: { api: Client; id: string }) {
  const [data, setData] = useState<Sharing>(), [choice, setChoice] = useState<Sharing['organization_access']>('restricted'), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const { message, modal } = App.useApp();
  async function load() { setError(''); try { const value = await api.request<Sharing>(`/libraries/${id}/sharing`); setData(value); setChoice(value.organization_access); } catch (e) { setError(errorMessage(e)); } }
  useEffect(() => { void load(); }, [api, id]);
  async function save() { if (!data) return; setBusy(true); setError(''); try { const value = await api.request<Sharing>(`/libraries/${id}/sharing`, 'PUT', { organization_access: choice, expected_revision: data.revision }); setData(value); message.success('共享模式已更新'); } catch (e) { setError(errorMessage(e)); throw e; } finally { setBusy(false); } }
  return <Space orientation="vertical" className="full-width" size="small">
    {error && <LoadError error={error} retry={load} />}
    <Space wrap><Typography.Text>共享模式</Typography.Text><Select style={{ width: 196 }} aria-label="知识库共享模式" disabled={!data || busy} value={choice} onChange={setChoice} options={[{ value: 'restricted', label: '指定成员可访问' }, { value: 'org_read', label: '组织全员可读' }]} />
      <Button loading={busy} disabled={!data || choice === data.organization_access} onClick={() => modal.confirm({ title: choice === 'org_read' ? '允许组织全员读取此库？' : '关闭组织全员读取？', content: '单独授予的成员权限保持不变。', onOk: save })}>保存共享模式</Button></Space>
    {data?.organization_access === 'org_read' && <Typography.Paragraph type="secondary">组织成员均可阅读。取消个人授权不会取消全员读取；编辑和发布仍需单独授权。</Typography.Paragraph>}
  </Space>;
}
