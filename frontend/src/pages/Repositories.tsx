import { useState } from 'react';
import { Alert, App, Button, Drawer, Dropdown, Form, Input, InputNumber, Modal, Select, Space, Switch, Table, Tag, Typography } from 'antd';
import { DownOutlined, PlusOutlined, SyncOutlined } from '@ant-design/icons';
import { useResource, useSession } from '../context';
import { errorMessage } from '../api';
import { AsyncForm, date, idRules, LoadError, NameCell, PageHeader, PathInput, required, Surface } from '../components';
import type { RepoList, Repository, RepositoryJob } from '../types';

const jobLabels: Record<string, string> = { queued: '等待同步', running: '同步中', succeeded: '已发布', unchanged: '已是最新', failed: '同步失败' };
export function RepositoriesPage() {
  const { api, refresh } = useSession(), { message } = App.useApp();
  const resource = useResource<RepoList>('/repositories', 10000);
  const [formRepo, setFormRepo] = useState<Repository | 'new'>(), [binding, setBinding] = useState<string>(), [history, setHistory] = useState<Repository>();
  const [syncing, setSyncing] = useState<string>();
  const boundRepo = resource.data?.repositories.find(r => r.id === binding);
  return <><PageHeader title="Git 仓库" action={<Button type="primary" icon={<PlusOutlined />} onClick={() => setFormRepo('new')}>添加仓库</Button>} /><LoadError error={resource.error} retry={resource.refresh} />
    {resource.data && !resource.data.worker_online && <Alert className="block-gap" type="warning" showIcon title="同步进程未在线，更新任务将保留在队列中。" />}
    <Surface><Table<Repository> rowKey="id" loading={resource.loading} pagination={false} dataSource={resource.data?.repositories} scroll={{ x: 950 }} columns={[
      { title: '仓库', width: 300, render: (_, r) => <NameCell name={<Typography.Text strong>{r.name}</Typography.Text>} id={r.url || r.import_path} /> },
      { title: '分支 / 版本', render: (_, r) => <NameCell name={r.branch || '待识别分支'} id={r.current_commit?.slice(0, 12) || '尚未发布'} /> },
      { title: '关联知识库', render: (_, r) => <Space wrap>{r.libraries.map(l => <Tag key={l.library_id}>{l.name}</Tag>)}</Space> },
      { title: '同步状态', render: (_, r) => <div className="name-cell"><span><Tag color={r.job?.status === 'failed' ? 'error' : ['queued', 'running'].includes(r.job?.status || '') ? 'processing' : 'success'}>{jobLabels[r.job?.status || ''] || '尚未同步'}</Tag></span><Typography.Text type="secondary">{!r.enabled ? '自动更新已暂停' : r.interval_seconds ? `每 ${r.interval_seconds / 60} 分钟` : '手动更新'}</Typography.Text>{r.next_sync && r.enabled && <Typography.Text type="secondary">下次 {date(r.next_sync * 1000)}</Typography.Text>}{r.last_error && <Typography.Text type="danger">{r.last_error}</Typography.Text>}</div> },
      { title: '操作', width: 190, render: (_, r) => <Space size={0} wrap={false}><Button type="link" icon={<SyncOutlined />} loading={syncing === r.id} disabled={['queued', 'running'].includes(r.job?.status || '')} onClick={async () => { setSyncing(r.id); try { await api.request(`/repositories/${r.id}/sync`, 'POST'); refresh(); message.success('已加入同步队列'); } catch (e) { message.error(errorMessage(e)); } finally { setSyncing(undefined); } }}>更新</Button><Dropdown trigger={['click']} menu={{ items: [
        { key: 'bind', label: '关联知识库', disabled: !r.release_path },
        { key: 'settings', label: '仓库设置' },
        { key: 'history', label: '同步记录' },
      ], onClick: ({ key }) => { if (key === 'bind') setBinding(r.id); else if (key === 'settings') setFormRepo(r); else if (key === 'history') setHistory(r); } }}><Button type="link">更多 <DownOutlined /></Button></Dropdown></Space> },
    ]} /></Surface>
    <Modal title={formRepo === 'new' ? '添加 Git 仓库' : '仓库设置'} open={!!formRepo} onCancel={() => setFormRepo(undefined)} footer={null} destroyOnHidden width={600}>{formRepo && <RepositoryForm key={formRepo === 'new' ? 'new' : formRepo.id} repository={formRepo === 'new' ? undefined : formRepo} done={() => setFormRepo(undefined)} />}</Modal>
    <Drawer closable={{ placement: 'end' }} title={'关联知识库 · ' + (boundRepo?.name || '')} open={!!boundRepo} onClose={() => setBinding(undefined)} destroyOnHidden size={620}>{boundRepo && <RepositoryBindings repository={boundRepo} />}</Drawer>
    <Drawer closable={{ placement: 'end' }} title={'同步记录 · ' + (history?.name || '')} open={!!history} onClose={() => setHistory(undefined)} destroyOnHidden size={700}>{history && <SyncHistory repository={history} />}</Drawer>
  </>;
}
interface RepoValues { mode: string; name: string; url: string; branch: string; import_path: string; credential: string; interval: number; enabled: boolean }
function RepositoryForm({ repository: r, done }: { repository?: Repository; done: () => void }) {
  const { api, refresh } = useSession(), { message } = App.useApp(), [form] = Form.useForm<RepoValues>();
  const mode = Form.useWatch('mode', form);
  return <AsyncForm form={form} initial={{ mode: 'remote', branch: 'main', interval: (r?.interval_seconds || 0) / 60, ...r }} submitText={r ? '保存设置' : '添加并同步'} submit={async values => {
    const body = { name: values.name, credential: values.credential?.trim() || '', interval_seconds: (values.interval || 0) * 60, ...(r ? { enabled: values.enabled } : values.mode === 'import' ? { import_path: values.import_path } : { url: values.url.trim(), branch: values.branch.trim() }) };
    await api.request('/repositories' + (r ? '/' + r.id : ''), r ? 'PUT' : 'POST', body); refresh(); done(); message.success(r ? '设置已保存' : '仓库已加入同步队列');
  }}>
    {!r && <Form.Item name="mode" label="来源"><Select options={[{ value: 'remote', label: '拉取远端仓库' }, { value: 'import', label: '接管已有目录' }]} /></Form.Item>}
    <Form.Item name="name" label="名称" rules={required}><Input maxLength={80} /></Form.Item>
    {r ? <Typography.Paragraph type="secondary" className="path-text">{r.url || r.import_path} · {r.branch}</Typography.Paragraph> : mode === 'import' ? <Form.Item name="import_path" label="已有 Git 目录" rules={required}><PathInput /></Form.Item> : <><Form.Item name="url" label="仓库地址" rules={required}><Input placeholder="https://git.example.com/team/knowledge.git" /></Form.Item><Form.Item name="branch" label="分支" rules={required}><Input /></Form.Item></>}
    <Form.Item name="credential" label="凭据配置名（公开仓库可留空）" rules={[{ pattern: /^[a-z0-9][a-z0-9_-]{0,63}$/, message: '使用小写字母、数字、- 或 _' }]}><Input placeholder="team-readonly" /></Form.Item>
    <Form.Item name="interval" label="自动更新间隔（分钟，0 表示手动）" rules={[{ required: true }]}><InputNumber min={0} max={43200} precision={0} className="full-width" /></Form.Item>
    {r && <Form.Item name="enabled" label="启用自动更新" valuePropName="checked"><Switch /></Form.Item>}
  </AsyncForm>;
}
function RepositoryBindings({ repository }: { repository: Repository }) {
  const { api, refresh, overview } = useSession(), { message, modal } = App.useApp();
  const [form] = Form.useForm<{ existing: string; library_id: string; name: string; subpath: string }>();
  const existing = Form.useWatch('existing', form);
  return <><AsyncForm form={form} initial={{ existing: '', subpath: '.' }} submitText="保存关联" submit={async values => { await api.request(`/repositories/${repository.id}/libraries`, 'POST', { library_id: values.library_id, name: values.name, subpath: values.subpath, existing: !!values.existing }); refresh(); message.success('关联已保存'); }}>
    <Form.Item name="existing" label="知识库"><Select onChange={id => { const lib = overview.libraries.find(l => l.id === id); form.setFieldsValue({ library_id: lib?.id || '', name: lib?.name || '', subpath: lib?.repository?.subpath || '.' }); }} options={[{ value: '', label: '新建知识库' }, ...overview.libraries.filter(l => !l.repository || l.repository.repository_id === repository.id).map(l => ({ value: l.id, label: l.name }))]} /></Form.Item>
    <Form.Item name="library_id" label="知识库 ID" rules={idRules}><Input disabled={!!existing} maxLength={64} /></Form.Item><Form.Item name="name" label="名称" rules={required}><Input maxLength={80} /></Form.Item><Form.Item name="subpath" label="仓库内检索目录" rules={required}><Input placeholder=". 表示整个仓库" /></Form.Item>
  </AsyncForm>{repository.libraries.length > 0 && <div className="result-section"><Typography.Title level={5}>已有关联</Typography.Title>{repository.libraries.map(l => <div key={l.library_id} className="connection-row"><NameCell name={l.name} id={l.subpath} /><Button type="link" onClick={() => modal.confirm({ title: '解除 Git 关联？', content: '保留当前目录与授权，后续不再跟随仓库更新。', onOk: async () => { try { await api.request(`/repositories/${repository.id}/libraries/${l.library_id}`, 'DELETE'); refresh(); } catch (e) { message.error(errorMessage(e)); throw e; } } })}>解除关联</Button></div>)}</div>}</>;
}
function SyncHistory({ repository }: { repository: Repository }) {
  const resource = useResource<{ jobs: RepositoryJob[] }>(`/repositories/${repository.id}/jobs`, 10000);
  return <><LoadError error={resource.error} retry={resource.refresh} /><Table<RepositoryJob> rowKey="id" loading={resource.loading} pagination={false} dataSource={resource.data?.jobs} columns={[
    { title: '时间', render: (_, j) => <NameCell name={date(j.finished_at || j.created_at)} id={({ initial: '首次同步', manual: '手动更新', scheduled: '定时更新' } as Record<string, string>)[j.trigger]} /> },
    { title: '结果', render: (_, j) => <div className="name-cell"><span><Tag color={j.status === 'failed' ? 'error' : 'default'}>{jobLabels[j.status]}</Tag></span><Typography.Text code>{j.after_commit?.slice(0, 12) || j.before_commit?.slice(0, 12) || '—'}</Typography.Text>{j.error && <Typography.Text type="danger">{j.error}</Typography.Text>}</div> },
  ]} /></>;
}
