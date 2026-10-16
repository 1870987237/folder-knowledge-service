import { useEffect, useState } from 'react';
import { Alert, App, Button, Checkbox, Collapse, Drawer, Empty, Form, Input, InputNumber, Modal, Select, Space, Spin, Switch, Table, Tag, Typography } from 'antd';
import { useApiResource, useResource, useSession } from '../context';
import { AsyncForm, date, idRules, JsonView, LoadError, PageHeader, required, Surface } from '../components';
import { type Api, errorMessage, parseObject } from '../api';
import { ResourceAccess } from '../ResourceAccess';
import type { Binding, Library, Template } from '../types';

interface Model { id: string; name: string; base_url: string; model: string; api_key_env?: string | null; enabled: boolean }
interface Policy { enabled: boolean; auto_trigger: boolean; model_connection_id?: string | null; model_environment: Record<string, string>; max_batch_size: number; approved_package_sha256?: string | null }
interface PolicyForm extends Omit<Policy, 'model_environment'> { mapping: string; approved: boolean }
interface Job { id: string; library_id: string; operation: string; status: string; created_at: string; error_code?: string; error_message?: string; result?: { summary: string } }
const statuses: Record<string, string> = { queued: '排队中', running: '生成中', candidate_ready: '待审核', publish_queued: '等待发布', publishing: '发布中', published: '已发布', rejected: '已拒绝', failed: '失败' };

export function ModelsPage() {
  const { api, refresh } = useSession(), { message } = App.useApp();
  const resource = useResource<{ models: Model[] }>('/models');
  const [editing, setEditing] = useState<Partial<Model>>(), [access, setAccess] = useState<Model>();
  return <><PageHeader title="模型连接" action={<Button type="primary" onClick={() => setEditing({ enabled: true })}>添加连接</Button>} /><LoadError error={resource.error} retry={resource.refresh} />
    <Surface><Table<Model> rowKey="id" dataSource={resource.data?.models} loading={resource.loading} pagination={false} columns={[
      { title: '名称', dataIndex: 'name' }, { title: '模型', dataIndex: 'model' }, { title: '接口地址', dataIndex: 'base_url' },
      { title: '密钥环境变量', render: (_, r) => r.api_key_env || '无需密钥' },
      { title: '状态', render: (_, r) => <Tag color={r.enabled ? 'success' : 'default'}>{r.enabled ? '启用' : '停用'}</Tag> },
      { title: '操作', render: (_, r) => <Space><Button type="link" onClick={() => setEditing(r)}>编辑</Button><Button type="link" onClick={() => setAccess(r)}>使用授权</Button></Space> },
    ]} /></Surface>
    <Modal title="模型使用授权" open={!!access} footer={null} destroyOnHidden onCancel={() => setAccess(undefined)}>{access && <ResourceAccess api={api} kind="model" resourceId={access.id} />}</Modal>
    <Modal title="模型连接" open={!!editing} footer={null} destroyOnHidden onCancel={() => setEditing(undefined)}>{editing && <AsyncForm<Model> initial={editing} submit={async values => {
      await api.request('/models/' + values.id, 'PUT', { ...values, api_key_env: values.api_key_env || null }); refresh(); setEditing(undefined); message.success('连接已保存');
    }}><Form.Item name="id" label="连接 ID" rules={idRules}><Input disabled={!!editing.id} /></Form.Item><Form.Item name="name" label="名称" rules={required}><Input /></Form.Item><Form.Item name="base_url" label="OpenAI 兼容接口地址" rules={required}><Input placeholder="https://provider.example/v1" /></Form.Item><Form.Item name="model" label="模型名称" rules={required}><Input /></Form.Item><Form.Item name="api_key_env" label="密钥环境变量名" extra="填写更新进程已配置并允许使用的变量名，不填写密钥本身。"><Input placeholder="PROVIDER_API_KEY" /></Form.Item><Form.Item name="enabled" label="启用" valuePropName="checked"><Switch /></Form.Item></AsyncForm>}</Modal>
  </>;
}

export function UpdateSettings({ library }: { library: Library }) {
  const { api, refresh } = useSession(), { message, modal } = App.useApp();
  const resource = useResource<Policy>(`/libraries/${library.id}/update-policy`);
  const binding = useResource<{ binding: Binding | null }>(`/libraries/${library.id}/template`);
  const models = useResource<{ models: Model[] }>('/models');
  const [form] = Form.useForm<PolicyForm>();
  const [capabilities, setCapabilities] = useState<string[]>();
  const canUpdate = capabilities?.includes('update');
  const [digest, setDigest] = useState<string>(), [error, setError] = useState('');
  useEffect(() => { let alive = true; setDigest(undefined); setCapabilities(undefined); setError(''); const b = binding.data?.binding;
    if (b) void api.request<Template>(`/templates/${b.template_id}/versions/${b.template_version}`).then(t => { if (alive) { setDigest(t.package_sha256 || undefined); setCapabilities(t.definition.capabilities); setError(''); } }).catch(e => { if (alive) setError(errorMessage(e)); });
    return () => { alive = false; };
  }, [api, binding.data]);
  useEffect(() => { if (resource.data) form.setFieldsValue({ ...resource.data, mapping: JSON.stringify(resource.data.model_environment, null, 2), approved: !!digest && resource.data.approved_package_sha256 === digest }); }, [resource.data, digest, form]);
  async function run(operation: string) { try { await api.request(`/libraries/${library.id}/update-jobs`, 'POST', { operation }); refresh(); message.success('已加入更新队列，可前往更新任务查看'); } catch (e) { message.error(errorMessage(e)); throw e; } }
  if (resource.loading || binding.loading || models.loading || (binding.data?.binding && !capabilities && !error)) return <Spin />;
  return <><LoadError error={resource.error || binding.error || models.error || error} retry={() => { resource.refresh(); binding.refresh(); models.refresh(); }} />
    {!binding.data?.binding ? <Empty description="请先绑定插件" /> : !canUpdate ? <Alert type="info" title="当前插件未提供增量更新能力。" /> : <AsyncForm form={form} submitText="保存更新设置" submit={async values => {
      await api.request(`/libraries/${library.id}/update-policy`, 'PUT', { enabled: values.enabled, auto_trigger: values.auto_trigger, model_connection_id: values.model_connection_id || null, model_environment: parseObject(values.mapping), max_batch_size: values.max_batch_size, approved_package_sha256: values.approved ? digest : null }); refresh(); message.success('更新设置已保存');
    }}><Form.Item name="enabled" label="启用更新程序" valuePropName="checked"><Switch /></Form.Item><Form.Item name="auto_trigger" label="新材料自动触发" valuePropName="checked"><Switch /></Form.Item><Form.Item name="model_connection_id" label="模型连接"><Select allowClear placeholder="不使用大模型" options={models.data?.models.map(m => ({ value: m.id, label: m.name, disabled: !m.enabled }))} /></Form.Item><Form.Item name="max_batch_size" label="每批材料上限"><InputNumber min={1} max={100} /></Form.Item><Form.Item name="mapping" label="插件环境变量映射" rules={required}><Input.TextArea rows={5} className="json-input" /></Form.Item><Typography.Paragraph type="secondary">变量名按插件要求填写，值为 base_url、model 或 api_key。候选均需人工审核后发布。</Typography.Paragraph><Form.Item name="approved" valuePropName="checked"><Checkbox disabled={!digest}>已审阅并信任当前插件程序包</Checkbox></Form.Item><Typography.Text code>{digest || '程序包未就绪'}</Typography.Text>
    </AsyncForm>}
    <Space className="block-gap"><Button disabled={!canUpdate || !resource.data?.enabled} onClick={() => { void run('update').catch(() => {}); }}>处理待办材料</Button><Button disabled={!canUpdate || !capabilities?.includes('initialize') || !resource.data?.enabled} onClick={() => modal.confirm({ title: '生成初始化候选？', content: '用于首次创建知识库，仍需审核后发布。已有知识库请使用增量更新。', onOk: () => run('initialize') })}>初始化知识库</Button></Space>
  </>;
}

export function UpdatesPage() {
  const { api } = useSession();
  return <UpdatesPanel api={api} />;
}

export function UpdatesPanel({ api, libraryId }: { api: Api; libraryId?: string }) {
  const { modal, message } = App.useApp();
  const base = libraryId ? `/libraries/${libraryId}/update-jobs` : '/update-jobs';
  const [offset, setOffset] = useState(0), [selected, setSelected] = useState<string>();
  const resource = useApiResource<{ jobs: Job[]; next_offset: number | null; worker_heartbeat: string | null; can_run?: boolean; can_initialize?: boolean; unavailable?: string; initialization?: string; pending_count?: number; retry_count?: number }>(api, `${base}?offset=${offset}`, 5000);
  const detail = useApiResource<Job>(api, selected ? base + '/' + selected : null, 5000);
  const row = detail.data;
  const preview = useApiResource<{ changes: { path: string; change: string; diff: string; truncated: boolean }[] }>(api, row && ['candidate_ready', 'published', 'publish_queued'].includes(row.status) ? `${base}/${row.id}/preview` : null);
  function refresh() { resource.refresh(); detail.refresh(); preview.refresh(); }
  function action(job: Job, name: string) { modal.confirm({ title: name === 'approve' ? '审核通过并发布此候选？' : name === 'reject' ? '拒绝此候选？' : '重新生成候选？', content: name === 'approve' ? '发布后 Agent 将读取新版本。' : undefined, onOk: async () => { try { await api.request(`${base}/${job.id}/${name}`, 'POST'); refresh(); message.success('操作已提交'); } catch (e) { message.error(errorMessage(e)); throw e; } } }); }
  async function run(operation: string) {
    try { await api.request(base, 'POST', { operation }); refresh(); message.success('已加入更新队列'); }
    catch (e) { message.error(errorMessage(e)); throw e; }
  }
  const online = resource.data?.worker_heartbeat && Date.now() - Date.parse(resource.data.worker_heartbeat) < 30000;
  return <>{libraryId ? <Space className="block-gap" wrap><Button type="primary" disabled={!resource.data?.can_run} onClick={() => { void run('update').catch(() => {}); }}>处理待办材料</Button>{resource.data?.can_initialize && <Button onClick={() => modal.confirm({ title: '生成初始化候选？', content: '初始化用于首次创建，候选仍需人工审核后发布。', onOk: () => run('initialize') })}>初始化知识库</Button>}{resource.data?.initialization === 'ready' && <Tag>已初始化</Tag>}<Typography.Text type="secondary">待处理 {resource.data?.pending_count ?? 0} 条{resource.data?.retry_count ? `（含失败待重试 ${resource.data.retry_count} 条）` : ''}</Typography.Text><Tag color={online ? 'success' : 'warning'}>{online ? '更新进程在线' : '更新进程未连接'}</Tag></Space> : <PageHeader title="更新任务" action={<Tag color={online ? 'success' : 'warning'}>{online ? '更新进程在线' : '更新进程未连接'}</Tag>} />}{libraryId && resource.data?.unavailable && <Alert type="info" title={resource.data.unavailable} />}<LoadError error={resource.error} retry={resource.refresh} />
    <Surface><Table<Job> rowKey="id" loading={resource.loading} dataSource={resource.data?.jobs} pagination={false} columns={[
      ...(!libraryId ? [{ title: '知识库', dataIndex: 'library_id' }] : []), { title: '任务', render: (_, r) => r.operation === 'initialize' ? '初始化' : '增量更新' },
      { title: '状态', render: (_, r) => <Tag color={r.status === 'published' ? 'success' : r.status === 'failed' ? 'error' : r.status === 'candidate_ready' ? 'warning' : 'default'}>{statuses[r.status] || r.status}</Tag> },
      { title: '创建时间', render: (_, r) => date(r.created_at) }, { title: '操作', render: (_, r) => <Button type="link" onClick={() => setSelected(r.id)}>查看</Button> },
    ]} /></Surface>{(offset > 0 || resource.data?.next_offset != null) && <div className="pager"><Button disabled={!offset} onClick={() => setOffset(0)}>首页</Button><Button disabled={resource.data?.next_offset == null} onClick={() => setOffset(resource.data!.next_offset!)}>下一页</Button></div>}
    <Drawer title="更新详情" open={!!selected} onClose={() => setSelected(undefined)} size={900} destroyOnHidden><LoadError error={detail.error || preview.error} retry={() => { detail.refresh(); preview.refresh(); }} />{detail.loading && <Spin />}{row && <><Tag>{statuses[row.status]}</Tag><Typography.Paragraph>{row.result && `生成时说明：${row.result.summary}`}</Typography.Paragraph>{row.error_code && <Alert type="error" title={row.error_message || row.error_code} className="block-gap" />}
      {preview.loading && <Spin />}{preview.data && <Collapse items={preview.data.changes.map(c => ({ key: c.path, label: `${({ added: '新增', modified: '修改', deleted: '删除' } as Record<string, string>)[c.change]} · ${c.path}`, children: <>{c.truncated && <Alert type="warning" title="差异预览已截断，请检查任务目录中的完整候选后再发布。" />}<pre className="code-block">{c.diff}</pre></> }))} />}
      <Collapse className="block-gap" items={[{ key: 'receipt', label: '材料处理结果', children: <JsonView value={row.result} /> }]} />
      <Space className="block-gap">{row.status === 'candidate_ready' && <><Button type="primary" disabled={!!preview.error || !preview.data} onClick={() => action(row, 'approve')}>审核通过并发布</Button><Button danger onClick={() => action(row, 'reject')}>拒绝候选</Button></>}{row.status === 'failed' && <Button onClick={() => action(row, 'retry')}>重试</Button>}</Space>
    </>}</Drawer>
  </>;
}
