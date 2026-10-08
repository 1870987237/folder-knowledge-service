import { useEffect, useRef, useState } from 'react';
import { Alert, App, Button, Checkbox, Drawer, Dropdown, Empty, Form, Input, Modal, Select, Space, Spin, Table, Tabs, Tree, Typography } from 'antd';
import type { TreeDataNode } from 'antd';
import { MoreOutlined, PlusOutlined, ReloadOutlined, SearchOutlined } from '@ant-design/icons';
import type { Client } from './People';
import { departmentRows, parentChoices, unitPath, type Unit } from './Directory';
import { errorMessage } from '../api';
import { LoadError } from '../components';

type Member = { id: string; username: string; role: string };
type Profile = Member & { unit_ids: string[] };
type MemberValues = { role: string; departments: string[]; groups: string[] };
type UnitValues = { name: string; parent_id?: string; member_ids: string[] };
const roles = [{ value: 'member', label: '成员' }, { value: 'admin', label: '组织管理员' }];
const labels = { department: '部门', group: '用户组' };

export function scopedMemberIds(units: Unit[], scope: string, descendants: boolean): Set<string> | undefined {
  if (scope === 'all') return undefined;
  if (scope === 'unassigned') return new Set(units.filter(u => u.kind === 'department').flatMap(u => u.member_ids));
  const included = new Set([scope]);
  if (descendants) {
    let changed = true;
    while (changed) { changed = false; for (const unit of units) if (unit.kind === 'department' && unit.parent_id && included.has(unit.parent_id) && !included.has(unit.id)) { included.add(unit.id); changed = true; } }
  }
  return new Set(units.filter(u => included.has(u.id)).flatMap(u => u.member_ids));
}

export function filterMembers(members: Member[], units: Unit[], scope: string, descendants: boolean, query: string, role: string) {
  const ids = scopedMemberIds(units, scope, descendants);
  return members.filter(m => (!ids || (scope === 'unassigned' ? !ids.has(m.id) : ids.has(m.id))) &&
    (!role || m.role === role) && m.username.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
}

export function OrganizationMembers({ api, oid, onChanged }: { api: Client; oid: string; onChanged: () => void }) {
  const { modal, message } = App.useApp();
  const [units, setUnits] = useState<Unit[]>([]), [members, setMembers] = useState<Member[]>([]);
  const [busy, setBusy] = useState(true), [error, setError] = useState('');
  const [tab, setTab] = useState('members'), [scope, setScope] = useState('all'), [descendants, setDescendants] = useState(false);
  const [query, setQuery] = useState(''), [role, setRole] = useState(''), [groupId, setGroupId] = useState<string>();
  const [page, setPage] = useState(1);
  const [adding, setAdding] = useState(false), [addError, setAddError] = useState('');
  const [memberId, setMemberId] = useState<string>(), [profile, setProfile] = useState<Profile>(), [profileBusy, setProfileBusy] = useState(false), [profileError, setProfileError] = useState('');
  const [unitDialog, setUnitDialog] = useState<{ kind: Unit['kind']; unit?: Unit }>(), [unitError, setUnitError] = useState('');
  const [saving, setSaving] = useState(false), [dirty, setDirty] = useState(false);
  const [memberForm] = Form.useForm<MemberValues>(), [unitForm] = Form.useForm<UnitValues>(), [addForm] = Form.useForm();
  const generation = useRef(0), profileGeneration = useRef(0);
  async function load() {
    const gen = ++generation.current; setBusy(true); setError('');
    try {
      const [u, m] = await Promise.all([api.request<Unit[]>(`/organizations/${oid}/units`), api.request<Member[]>(`/organizations/${oid}/members`)]);
      if (gen !== generation.current) return;
      setUnits(u); setMembers(m);
      setScope(s => ['all', 'unassigned'].includes(s) || u.some(x => x.id === s) ? s : 'all');
      setGroupId(g => u.some(x => x.id === g) ? g : undefined);
    } catch (e) { if (gen === generation.current) setError(errorMessage(e)); }
    finally { if (gen === generation.current) setBusy(false); }
  }
  useEffect(() => { void load(); return () => { ++generation.current; ++profileGeneration.current; }; }, [api, oid]);
  useEffect(() => { setPage(1); }, [scope, descendants, query, role, groupId, tab]);
  useEffect(() => { const warn = (e: BeforeUnloadEvent) => { if (dirty) e.preventDefault(); }; window.addEventListener('beforeunload', warn); return () => window.removeEventListener('beforeunload', warn); }, [dirty]);
  const departments = units.filter(u => u.kind === 'department'), groups = units.filter(u => u.kind === 'group');
  const group = groups.find(u => u.id === groupId);
  const selectedDepartment = departments.find(u => u.id === scope);
  const rows = filterMembers(members, units, tab === 'groups' && group ? group.id : scope, tab === 'members' && descendants, query, role);
  function close(action: () => void) { if (saving) return; if (dirty) modal.confirm({ title: '放弃未保存的修改？', onOk: () => { setDirty(false); action(); } }); else action(); }
  async function openMember(id: string) {
    const gen = ++profileGeneration.current; setMemberId(id); setProfile(undefined); setProfileBusy(true); setProfileError(''); setDirty(false); memberForm.resetFields();
    try {
      const [p, latest] = await Promise.all([api.request<Profile>(`/organizations/${oid}/members/${id}/profile`), api.request<Unit[]>(`/organizations/${oid}/units`)]);
      if (gen !== profileGeneration.current) return;
      setProfile(p); setUnits(latest); memberForm.setFieldsValue({ role: p.role, departments: p.unit_ids.filter(uid => latest.some(u => u.id === uid && u.kind === 'department')), groups: p.unit_ids.filter(uid => latest.some(u => u.id === uid && u.kind === 'group')) });
    } catch (e) { if (gen === profileGeneration.current) setProfileError(errorMessage(e)); }
    finally { if (gen === profileGeneration.current) setProfileBusy(false); }
  }
  function openUnit(kind: Unit['kind'], unit?: Unit, parent?: string) {
    setUnitError(''); setDirty(false); setUnitDialog({ kind, unit }); unitForm.resetFields();
    unitForm.setFieldsValue({ name: unit?.name || '', parent_id: unit?.parent_id || parent, member_ids: unit?.member_ids || [] });
  }
  function removeMember(m: Member) {
    modal.confirm({ title: `移出 ${m.username}？`, content: '将移除其组织、部门及用户组关联，并撤销来自本组织的访问权限。账号和个人知识库保留。', onOk: async () => {
      try { await api.request(`/organizations/${oid}/members`, 'PUT', { username: m.username, role: 'remove' }); await load(); onChanged(); }
      catch (e) { message.error(errorMessage(e)); throw e; }
    } });
  }
  function deleteUnit(u: Unit) {
    modal.confirm({ title: `删除${labels[u.kind]}「${u.name}」？`, content: '将移除成员关联和此项知识库授权，其他授权仍有效。账号与文件保留。', onOk: async () => {
      try { await api.request(`/organizations/${oid}/units/${u.id}?expected_revision=${u.revision}`, 'DELETE'); await load(); }
      catch (e) { message.error(errorMessage(e)); throw e; }
    } });
  }
  function unitMenu(u: Unit) {
    return { items: [{ key: 'edit', label: u.kind === 'department' ? '部门设置' : '用户组设置' }, ...(u.kind === 'department' ? [{ key: 'child', label: '新增下级部门' }] : []), { key: 'delete', label: '删除', danger: true }], onClick: ({ key }: { key: string }) => { if (key === 'delete') deleteUnit(u); else if (key === 'child') openUnit('department', undefined, u.id); else openUnit(u.kind, u); } };
  }
  function treeData(rows: Unit[]): TreeDataNode[] {
    return rows.map(u => ({ key: u.id, title: <span className="organization-tree-label"><span title={u.name}>{u.name}</span><Dropdown trigger={['click']} menu={unitMenu(u)}><Button type="text" size="small" aria-label={`${u.name}菜单`} icon={<MoreOutlined />} onClick={e => e.stopPropagation()} /></Dropdown></span>, children: u.children && treeData(u.children) }));
  }
  function membershipNames(id: string, kind: Unit['kind']) {
    const names = units.filter(u => u.kind === kind && u.member_ids.includes(id)).map(u => unitPath(u, units));
    return <Typography.Text className="full-width" ellipsis={{ tooltip: names.join('、') }}>{names.join('、') || '—'}</Typography.Text>;
  }
  const memberTable = <Table<Member> size="small" className="organization-member-table" rowKey="id" loading={busy} dataSource={rows} pagination={{ current: page, onChange: setPage, pageSize: 15, hideOnSinglePage: true }} scroll={{ x: 660 }} columns={[
    { title: '成员', dataIndex: 'username', width: 150, render: (name, m) => <Button type="link" className="name-link" onClick={() => void openMember(m.id)}>{name}</Button> },
    { title: '部门', width: 180, render: (_, m) => membershipNames(m.id, 'department') },
    { title: '用户组', width: 170, render: (_, m) => membershipNames(m.id, 'group') },
    { title: '组织角色', width: 130, render: (_, m) => m.role === 'admin' ? '组织管理员' : '成员' },
    { title: '', width: 48, render: (_, m) => <Dropdown trigger={['click']} menu={{ items: [{ key: 'edit', label: '成员详情' }, { key: 'remove', label: '移出组织', danger: true }], onClick: ({ key }) => key === 'edit' ? void openMember(m.id) : removeMember(m) }}><Button type="text" size="small" aria-label={`${m.username}操作`} icon={<MoreOutlined />} /></Dropdown> },
  ]} />;
  const filters = <div className="organization-filters"><Input className="member-search" aria-label="搜索成员" placeholder="搜索成员" prefix={<SearchOutlined />} value={query} allowClear onChange={e => setQuery(e.target.value)} /><Select aria-label="筛选组织角色" className="role-select" placeholder="全部角色" value={role || undefined} allowClear onChange={v => setRole(v || '')} options={roles} />{tab === 'members' && selectedDepartment && <Checkbox checked={descendants} onChange={e => setDescendants(e.target.checked)}>包含下级部门</Checkbox>}</div>;
  return <div className="organization-manager">
    {error && <LoadError error={error} retry={load} />}
    <Tabs className="organization-tabs" styles={{ header: { marginBottom: 0 } }} activeKey={tab} onChange={t => { setTab(t); setQuery(''); setRole(''); }} tabBarExtraContent={<Space><Button type="text" aria-label="刷新成员" icon={<ReloadOutlined />} onClick={load} loading={busy} /><Button type="primary" icon={<PlusOutlined />} disabled={busy || !!error} onClick={() => { if (tab === 'groups') openUnit('group'); else { addForm.resetFields(); setAddError(''); setDirty(false); setAdding(true); } }}>{tab === 'groups' ? '新建用户组' : '添加成员'}</Button></Space>} items={[{ key: 'members', label: '成员与部门' }, { key: 'groups', label: '用户组' }]} />
    {tab === 'members' ? <div className="organization-directory-layout">
      <aside className="organization-tree" aria-label="部门导航">
        <Button className="department-scope" block type={scope === 'all' ? 'primary' : 'text'} onClick={() => setScope('all')}>全部成员<span className="toolbar-spacer" /><span>{members.length}</span></Button>
        <Button className="department-scope" block type={scope === 'unassigned' ? 'primary' : 'text'} onClick={() => setScope('unassigned')}>未分配部门</Button>
        <div className="organization-tree-heading"><Typography.Text strong>部门</Typography.Text><Button type="text" size="small" aria-label="添加部门" icon={<PlusOutlined />} disabled={busy || !!error} onClick={() => openUnit('department')} /></div>
        <Tree blockNode showLine selectedKeys={[scope]} autoExpandParent treeData={treeData(departmentRows(units))} onSelect={keys => { if (keys.length) setScope(String(keys[0])); }} />
        {!busy && !departments.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无部门" />}
      </aside>
      <div className="organization-members-content">{filters}{memberTable}</div>
    </div> : <div className="organization-group-content">
      {group ? <><div className="organization-group-heading"><Button onClick={() => { setGroupId(undefined); setQuery(''); setRole(''); }}>返回用户组</Button><Typography.Text strong>{group.name}</Typography.Text><Button onClick={() => openUnit('group', group)}>管理组成员</Button></div>{filters}{memberTable}</> : <Table<Unit> size="small" rowKey="id" loading={busy} dataSource={groups} pagination={{ hideOnSinglePage: true }} columns={[
        { title: '用户组', render: (_, u) => <Button type="link" className="name-link" onClick={() => setGroupId(u.id)}>{u.name}</Button> },
        { title: '成员数', width: 120, render: (_, u) => u.member_ids.length },
        { title: '', width: 48, render: (_, u) => <Dropdown trigger={['click']} menu={unitMenu(u)}><Button type="text" size="small" aria-label={`${u.name}菜单`} icon={<MoreOutlined />} /></Dropdown> },
      ]} />}
    </div>}
    <Modal title="添加成员" open={adding} onCancel={() => close(() => setAdding(false))} okText="添加" onOk={() => addForm.submit()} confirmLoading={saving} closable={!saving} maskClosable={false} cancelButtonProps={{ disabled: saving }}>
      {addError && <Alert type="error" title={addError} className="block-gap" />}
      <Form form={addForm} layout="vertical" initialValues={{ role: 'member' }} onValuesChange={() => setDirty(true)} onFinish={async v => {
        if (members.some(m => m.username.toLowerCase() === v.username.trim().toLowerCase())) { setAddError('成员已在组织中，请从成员列表打开详情修改。'); return; }
        setSaving(true); setAddError(''); try { await api.request(`/organizations/${oid}/members`, 'PUT', v); setDirty(false); setAdding(false); await load(); onChanged(); } catch (e) { setAddError(errorMessage(e)); } finally { setSaving(false); }
      }}><Form.Item name="username" label="成员账号" rules={[{ required: true, whitespace: true }]}><Input placeholder="已启用的账号" maxLength={80} /></Form.Item><Form.Item name="role" label="组织角色"><Select options={roles} /></Form.Item></Form>
    </Modal>
    <Drawer title={profile ? profile.username : '成员详情'} open={!!memberId} size={440} maskClosable={false} closable={!saving} onClose={() => close(() => { ++profileGeneration.current; setMemberId(undefined); })} extra={<Button type="primary" onClick={() => memberForm.submit()} loading={saving} disabled={!profile || profileBusy}>保存</Button>}>
      {profileBusy && <Spin />}{profileError && <Alert type="error" title={profileError} className="block-gap" action={<Button disabled={saving} onClick={() => close(() => { if (memberId) void openMember(memberId); })}>重新加载</Button>} />}
      {profile && <Form form={memberForm} layout="vertical" onValuesChange={() => setDirty(true)} onFinish={async v => {
        setSaving(true); setProfileError(''); try { await api.request(`/organizations/${oid}/members/${profile.id}/profile`, 'PUT', { role: v.role, unit_ids: [...v.departments, ...v.groups], expected_role: profile.role, expected_unit_ids: profile.unit_ids }); setDirty(false); setMemberId(undefined); await load(); onChanged(); message.success('已保存成员资料'); } catch (e) { setProfileError(errorMessage(e)); } finally { setSaving(false); }
      }}><Form.Item name="role" label="组织角色"><Select options={roles} /></Form.Item><Form.Item name="departments" label="所属部门"><Select mode="multiple" showSearch optionFilterProp="label" options={departments.map(u => ({ value: u.id, label: unitPath(u, units) }))} /></Form.Item><Form.Item name="groups" label="用户组"><Select mode="multiple" showSearch optionFilterProp="label" options={groups.map(u => ({ value: u.id, label: u.name }))} /></Form.Item></Form>}
    </Drawer>
    <Modal title={`${unitDialog?.unit ? '管理' : '新建'}${unitDialog ? labels[unitDialog.kind] : ''}`} open={!!unitDialog} onCancel={() => close(() => setUnitDialog(undefined))} onOk={() => unitForm.submit()} okText="保存" confirmLoading={saving} closable={!saving} maskClosable={false} cancelButtonProps={{ disabled: saving }}>
      {unitError && <Alert type="error" title={unitError} className="block-gap" action={<Button disabled={saving} onClick={() => close(() => { setUnitDialog(undefined); void load(); })}>刷新列表</Button>} />}
      <Form form={unitForm} layout="vertical" onValuesChange={() => setDirty(true)} onFinish={async v => {
        if (!unitDialog) return; const { kind, unit } = unitDialog; setSaving(true); setUnitError('');
        try { await api.request(`/organizations/${oid}/units${unit ? '/' + unit.id : ''}`, unit ? 'PUT' : 'POST', { ...v, parent_id: kind === 'department' ? v.parent_id || null : null, ...(unit ? { expected_revision: unit.revision } : { kind }) }); setDirty(false); setUnitDialog(undefined); await load(); } catch (e) { setUnitError(errorMessage(e)); } finally { setSaving(false); }
      }}><Form.Item name="name" label="名称" rules={[{ required: true, whitespace: true }]}><Input maxLength={80} /></Form.Item>{unitDialog?.kind === 'department' && <Form.Item name="parent_id" label="上级部门"><Select allowClear placeholder="顶级部门" showSearch optionFilterProp="label" options={parentChoices(units, unitDialog.unit).map(u => ({ value: u.id, label: unitPath(u, units) }))} /></Form.Item>}<Form.Item name="member_ids" label={unitDialog?.kind === 'department' ? '直属成员' : '组成员'}><Select mode="multiple" showSearch optionFilterProp="label" options={members.map(m => ({ value: m.id, label: m.username }))} /></Form.Item></Form>
    </Modal>
  </div>;
}
