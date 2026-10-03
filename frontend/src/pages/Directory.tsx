import { useEffect, useState } from 'react';
import { Alert, App, Button, Checkbox, Form, Modal, Select, Space, Table, Typography } from 'antd';
import type { Client } from './People';
import { errorMessage } from '../api';
import { LoadError } from '../components';

type Kind = 'department' | 'group';
export type Unit = { id: string; name: string; kind: Kind; parent_id: string | null; member_ids: string[]; revision: number; children?: Unit[] };
const labels = { department: '部门', group: '用户组' };
export function unitPath(unit: Unit, units: Unit[]): string {
  const parent = units.find(u => u.id === unit.parent_id);
  return parent ? `${unitPath(parent, units)} / ${unit.name}` : unit.name;
}
export function departmentRows(units: Unit[], parent: string | null = null): Unit[] {
  return units.filter(u => u.kind === 'department' && u.parent_id === parent).map(u => {
    const children = departmentRows(units, u.id);
    return { ...u, children: children.length ? children : undefined };
  });
}
export function parentChoices(units: Unit[], editing?: Unit): Unit[] {
  return units.filter(u => {
    if (u.kind !== 'department') return false;
    let cursor: Unit | undefined = u;
    while (cursor) { if (cursor.id === editing?.id) return false; cursor = units.find(p => p.id === cursor!.parent_id); }
    return true;
  });
}


type Grant = { unit_id: string; role: string; include_descendants: boolean };
type Grants = { units: Unit[]; grants: Grant[]; revision: number };
const roles = [{ value: 'reader', label: '读者' }, { value: 'editor', label: '编辑者' }, { value: 'publisher', label: '发布者' }, { value: 'manager', label: '管理者' }];
export function UnitGrants({ api, lid, refreshKey, onChanged }: { api: Client; lid: string; refreshKey?: number; onChanged?: () => void }) {
  const [data, setData] = useState<Grants>(), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false), [saveError, setSaveError] = useState(''), [saving, setSaving] = useState(false);
  const [form] = Form.useForm<Grant>(); const unitId = Form.useWatch('unit_id', form); const { modal } = App.useApp();
  async function load() { setBusy(true); setError(''); try { setData(await api.request(`/libraries/${lid}/unit-grants`)); } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); } }
  useEffect(() => { void load(); }, [api, lid, refreshKey]);
  async function put(values: Grant) {
    if (!data) return;
    const result = await api.request<Grants>(`/libraries/${lid}/unit-grants/${values.unit_id}`, 'PUT', { role: values.role, include_descendants: !!values.include_descendants, expected_revision: data.revision });
    setData(result); onChanged?.();
  }
  function open(g?: Grant) { form.setFieldsValue({ unit_id: g?.unit_id || '', role: g?.role || 'reader', include_descendants: !!g?.include_descendants }); setSaveError(''); setEditing(true); }
  return <Space orientation="vertical" className="full-width">
    <Space><Typography.Text strong>部门与用户组权限</Typography.Text><Button disabled={!data || busy || !!error} onClick={() => open()}>添加授权</Button></Space>
    {error && <LoadError error={error} retry={load} />}
    <Table<Grant> size="small" rowKey="unit_id" loading={busy} pagination={{ hideOnSinglePage: true }} dataSource={data?.grants} columns={[
      { title: '部门 / 用户组', render: (_, g) => { const u = data?.units.find(u => u.id === g.unit_id); return u ? `${labels[u.kind]} · ${unitPath(u, data!.units)}` : g.unit_id; } },
      { title: '范围', render: (_, g) => g.include_descendants ? '含下级部门' : '直属成员' },
      { title: '角色', render: (_, g) => roles.find(r => r.value === g.role)?.label },
      { title: '操作', width: 180, render: (_, g) => <Space><Button type="link" onClick={() => open(g)}>设置</Button><Button type="link" danger onClick={() => modal.confirm({ title: '取消此项授权？', content: '其他个人、部门、用户组及组织全员授权仍然有效。', onOk: async () => { try { await put({ ...g, role: 'remove' }); } catch (e) { setError(errorMessage(e)); throw e; } } })}>取消授权</Button></Space> },
    ]} />
    <Modal title="部门与用户组授权" open={editing} onCancel={() => setEditing(false)} okText="保存" confirmLoading={saving} closable={!saving} maskClosable={!saving} cancelButtonProps={{ disabled: saving }} onOk={() => form.submit()}>
      {saveError && <Alert type="error" title={saveError} action={<Button onClick={() => { setEditing(false); void load(); }}>重新加载</Button>} />}
      <Form form={form} layout="vertical" onFinish={async v => { setSaving(true); setSaveError(''); try { await put(v); setEditing(false); } catch (e) { setSaveError(errorMessage(e)); } finally { setSaving(false); } }}>
        <Form.Item name="unit_id" label="部门 / 用户组" rules={[{ required: true }]}><Select showSearch optionFilterProp="label" onChange={() => form.setFieldValue('include_descendants', false)} options={data?.units.map(u => ({ value: u.id, label: `${labels[u.kind]} · ${unitPath(u, data.units)}` }))} /></Form.Item>
        <Form.Item name="role" label="角色"><Select options={roles} /></Form.Item>
        {data?.units.find(u => u.id === unitId)?.kind === 'department' && <Form.Item name="include_descendants" valuePropName="checked"><Checkbox>包含下级部门（含今后新增的下级）</Checkbox></Form.Item>}
      </Form>
    </Modal>
  </Space>;
}
