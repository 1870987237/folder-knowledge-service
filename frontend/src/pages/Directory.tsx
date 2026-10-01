import { useEffect, useState } from 'react';
import { Alert, App, Button, Checkbox, Form, Input, Modal, Select, Space, Table, Tabs, Typography } from 'antd';
import type { Client } from './People';
import { errorMessage } from '../api';
import { LoadError } from '../components';

type Kind = 'department' | 'group';
export type Unit = { id: string; name: string; kind: Kind; parent_id: string | null; member_ids: string[]; revision: number; children?: Unit[] };
type Member = { id: string; username: string };
type Values = { name: string; parent_id?: string; member_ids: string[] };
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

export function OrganizationDirectory({ api, oid }: { api: Client; oid: string }) {
  const [units, setUnits] = useState<Unit[]>([]), [members, setMembers] = useState<Member[]>([]);
  const [kind, setKind] = useState<Kind>('department'), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [dialog, setDialog] = useState<{ kind: Kind; unit?: Unit }>(), [saving, setSaving] = useState(false), [saveError, setSaveError] = useState('');
  const [form] = Form.useForm<Values>(); const { modal, message } = App.useApp();
  async function load() {
    setBusy(true); setError('');
    try { const [u, m] = await Promise.all([api.request<Unit[]>(`/organizations/${oid}/units`), api.request<Member[]>(`/organizations/${oid}/members`)]); setUnits(u); setMembers(m); }
    catch (e) { setError(errorMessage(e)); } finally { setBusy(false); }
  }
  useEffect(() => { void load(); }, [api, oid]);
  function open(selectedKind: Kind, unit?: Unit) { setSaveError(''); setDialog({ kind: selectedKind, unit }); form.setFieldsValue({ name: unit?.name || '', parent_id: unit?.parent_id || undefined, member_ids: unit?.member_ids || [] }); }
  async function save(values: Values) {
    if (!dialog) return; setSaving(true); setSaveError('');
    const { unit, kind: unitKind } = dialog;
    try {
      await api.request(`/organizations/${oid}/units${unit ? '/' + unit.id : ''}`, unit ? 'PUT' : 'POST', {
        ...values, parent_id: unitKind === 'department' ? values.parent_id || null : null,
        ...(unit ? { expected_revision: unit.revision } : { kind: unitKind }),
      });
      setDialog(undefined); await load(); message.success('已保存');
    } catch (e) { setSaveError(errorMessage(e)); } finally { setSaving(false); }
  }
  return <Space orientation="vertical" className="full-width">
    {error && <LoadError error={error} retry={load} />}
    <Tabs activeKey={kind} onChange={v => setKind(v as Kind)} tabBarExtraContent={<Button onClick={() => open(kind)} disabled={busy || !!error}>添加{labels[kind]}</Button>} items={Object.entries(labels).map(([key, label]) => ({ key, label }))} />
    <Table<Unit> size="small" rowKey="id" loading={busy} dataSource={kind === 'department' ? departmentRows(units) : units.filter(u => u.kind === 'group')} pagination={{ hideOnSinglePage: true }} columns={[
      { title: '名称', dataIndex: 'name' },
      { title: '直属成员', render: (_, u) => u.member_ids.length },
      { title: '操作', width: 160, render: (_, u) => <Space><Button type="link" onClick={() => open(u.kind, u)}>设置</Button><Button type="link" danger onClick={() => modal.confirm({ title: `删除${labels[u.kind]}「${u.name}」？`, content: '移除成员关联和对应的知识库授权，保留账号与文件。有下级部门时不能删除。', onOk: async () => {
        try { await api.request(`/organizations/${oid}/units/${u.id}?expected_revision=${u.revision}`, 'DELETE'); await load(); }
        catch (e) { setError(errorMessage(e)); throw e; }
      } })}>删除</Button></Space> },
    ]} />
    <Modal title={`${dialog?.unit ? '设置' : '添加'}${dialog ? labels[dialog.kind] : ''}`} open={!!dialog} onCancel={() => setDialog(undefined)} okText="保存" onOk={() => form.submit()} confirmLoading={saving} closable={!saving} maskClosable={!saving} cancelButtonProps={{ disabled: saving }}>
      {saveError && <Alert type="error" title={saveError} action={<Button onClick={() => { setDialog(undefined); void load(); }}>重新加载</Button>} />}
      <Form form={form} layout="vertical" onFinish={save}>
        <Form.Item name="name" label="名称" rules={[{ required: true, whitespace: true }]}><Input maxLength={80} /></Form.Item>
        {dialog?.kind === 'department' && <Form.Item name="parent_id" label="上级部门"><Select allowClear placeholder="顶级部门" showSearch optionFilterProp="label" options={parentChoices(units, dialog.unit).map(u => ({ value: u.id, label: unitPath(u, units) }))} /></Form.Item>}
        <Form.Item name="member_ids" label="直属成员"><Select mode="multiple" showSearch optionFilterProp="label" placeholder="选择组织成员" options={members.map(m => ({ value: m.id, label: m.username }))} /></Form.Item>
      </Form>
    </Modal>
  </Space>;
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
