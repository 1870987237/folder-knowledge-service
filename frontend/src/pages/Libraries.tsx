import { errorMessage } from '../api';
import { useState } from 'react';
import { App, Button, Drawer, Empty, Form, Input, Modal, Select, Space, Switch, Table, Tabs, Tag, Typography } from 'antd';
import { FolderOutlined, PlusOutlined } from '@ant-design/icons';
import { useSession } from '../context';
import { AsyncForm, Availability, FileBrowser, idRules, NameCell, PageHeader, PathInput, required, Surface } from '../components';
import { UpdateSettings } from './Updates';
import { LibraryAgentSettings } from '../KeyPolicy';
import { RetrievalSettings } from './Retrieval';
import { TemplateBinding } from './Templates';
import type { Library } from '../types';

function LibraryForm({ library, done }: { library?: Library; done: () => void }) {
  const { api, refresh } = useSession(), { message } = App.useApp();
  return <AsyncForm initial={library || { enabled: true }} submitText="保存知识库" submit={async (values: { id: string; name: string; path: string; enabled: boolean }) => {
    await api.request('/libraries' + (library ? '/' + library.id : ''), library ? 'PUT' : 'POST', values); refresh(); message.success('知识库已保存'); done();
  }}><Form.Item name="name" label="名称" rules={required}><Input maxLength={80} placeholder="团队知识库" /></Form.Item><Form.Item name="id" label="知识库 ID" rules={idRules}><Input disabled={!!library} maxLength={64} placeholder="team-guide" /></Form.Item><Form.Item name="path" label="知识文件夹" rules={required}><PathInput disabled={!!library?.repository} /></Form.Item>{library?.repository && <Typography.Paragraph type="secondary">此目录由 Git 仓库关联管理。</Typography.Paragraph>}<Form.Item name="enabled" label="启用" valuePropName="checked"><Switch /></Form.Item></AsyncForm>;
}
export function LibrariesPage() {
  const { overview, api, refresh } = useSession(), { message, modal } = App.useApp();
  const [search, setSearch] = useState(''), [state, setState] = useState<string>();
  const [creating, setCreating] = useState(false), [selected, setSelected] = useState<string>(), [tab, setTab] = useState('files');
  const library = overview.libraries.find(l => l.id === selected);
  const rows = overview.libraries.filter(l => `${l.name} ${l.id} ${l.path}`.toLowerCase().includes(search.toLowerCase()) && (!state || (state === 'enabled' ? l.enabled : !l.enabled)));
  return <><PageHeader title="知识库" action={<Button type="primary" icon={<PlusOutlined />} onClick={() => setCreating(true)}>添加知识库</Button>} /><div className="toolbar"><Input.Search placeholder="搜索名称、ID 或目录" aria-label="搜索知识库" value={search} onChange={e => setSearch(e.target.value)} allowClear /><Select aria-label="筛选启用状态" placeholder="全部状态" allowClear value={state} onChange={setState} options={[{ value: 'enabled', label: '已启用' }, { value: 'disabled', label: '已停用' }]} /></div>
    <Surface><Table<Library> rowKey="id" dataSource={rows} pagination={rows.length > 20 ? { pageSize: 20 } : false} scroll={{ x: 840 }} locale={{ emptyText: <Empty description={search ? '未找到知识库' : '暂无知识库'} image={Empty.PRESENTED_IMAGE_SIMPLE} /> }} columns={[
      { title: '知识库', width: 250, render: (_, row) => <NameCell name={<Button type="link" className="name-link" icon={<FolderOutlined />} onClick={() => { setSelected(row.id); setTab('files'); }}>{row.name}</Button>} id={row.id} /> },
      { title: '检索目录', render: (_, row) => <div className="name-cell"><Typography.Text className="path-text">{row.path}</Typography.Text><span>{row.repository && <Tag>Git 托管</Tag>}</span></div> },
      { title: '状态', width: 110, render: (_, row) => <Availability library={row} /> },
      { title: '操作', width: 250, render: (_, row) => <Space><Button type="link" onClick={() => { setSelected(row.id); setTab('template'); }}>插件配置</Button><Button type="link" onClick={() => { setSelected(row.id); setTab('settings'); }}>设置</Button><Button type="text" onClick={() => modal.confirm({ title: row.enabled ? '停用此知识库？' : '启用此知识库？', content: row.enabled ? '后续访问与材料提交将被阻止，知识文件会保留。' : undefined, onOk: async () => { try { await api.request('/libraries/' + row.id, 'PUT', { id: row.id, name: row.name, path: row.path, enabled: !row.enabled }); refresh(); message.success('状态已更新'); } catch (e) { message.error(errorMessage(e)); throw e; } } })}>{row.enabled ? '停用' : '启用'}</Button></Space> },
    ]} /></Surface>
    <Modal title="添加知识库" open={creating} onCancel={() => setCreating(false)} footer={null} destroyOnHidden><LibraryForm done={() => setCreating(false)} /></Modal>
    <Drawer closable={{ placement: 'end' }} title={library?.name || '知识库详情'} open={!!library} onClose={() => setSelected(undefined)} size={800} destroyOnHidden>{library && <><div className="drawer-meta"><Typography.Text code>{library.id}</Typography.Text><Availability library={library} /></div><Tabs activeKey={tab} onChange={setTab} destroyOnHidden items={[
      { key: 'files', label: '知识文件', children: <FileBrowser key={library.id + library.path} library={library} /> },
      { key: 'template', label: '插件与工作范围', children: <TemplateBinding key={library.id} library={library} /> },
      { key: 'retrieval', label: '检索索引', children: <RetrievalSettings key={library.id} library={library} /> },
      { key: 'updates', label: '更新设置', children: <UpdateSettings key={library.id} library={library} /> },
      { key: 'agent', label: 'Agent 接入', children: <LibraryAgentSettings api={api} id={library.id} /> },
      { key: 'settings', label: '基本设置', children: <LibraryForm key={library.id} library={library} done={() => setSelected(undefined)} /> },
    ]} /></>}</Drawer>
  </>;
}
