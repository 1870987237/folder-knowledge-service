import { useState } from 'react';
import { Alert, App, Button, Collapse, Drawer, Form, Input, InputNumber, Modal, Select, Space, Spin, Switch, Table, Tag, Typography } from 'antd';
import { type Api, parseObject, query } from './api';
import { useApiResource } from './context';
import { AsyncForm, date, JsonView, LoadError, required } from './components';
import { UpdatesPanel } from './pages/Updates';

interface Plugin { id: string; version: string; name: string; capabilities: string[]; config_schema: { properties?: Record<string, { default?: unknown }> } }
interface Resources { plugins: Plugin[]; models: { id: string; name: string }[] }
interface PluginValues { template_choice?: string; config_text: string; enabled: boolean; auto_trigger: boolean; model_connection_id?: string; max_batch_size: number }
interface Settings { binding: { plugin_id: string; plugin_version: string; config: object } | null; policy: Omit<PluginValues, 'template_choice' | 'config_text'>; unavailable?: string }

function pluginPayload(v: PluginValues) {
  const [plugin_id, plugin_version] = (v.template_choice || '').split('@');
  return { plugin_id, plugin_version, config: parseObject(v.config_text || '{}'), enabled: !!v.enabled, auto_trigger: !!v.auto_trigger,
    model_connection_id: v.model_connection_id || null, max_batch_size: v.max_batch_size || 10 };
}

function PluginFields({ resources, optional = false }: { resources: Resources; optional?: boolean }) {
  const form = Form.useFormInstance(), choice = Form.useWatch('template_choice', form);
  const plugin = resources.plugins.find(p => p.id + '@' + p.version === choice);
  const updates = plugin?.capabilities.includes('update');
  return <>
    <Form.Item name="template_choice" label="知识库模板" rules={optional ? [] : required}><Select allowClear={optional} placeholder={optional ? '普通文档库' : '选择已授权插件'} options={resources.plugins.map(p => ({ value: p.id + '@' + p.version, label: `${p.name} · ${p.version}` }))} onChange={v => {
      const p = resources.plugins.find(p => p.id + '@' + p.version === v);
      const defaults = Object.fromEntries(Object.entries(p?.config_schema.properties || {}).filter(([, v]) => v.default !== undefined).map(([k, v]) => [k, v.default]));
      form.setFieldsValue({ config_text: JSON.stringify(defaults, null, 2), enabled: !!p?.capabilities.includes('update'), auto_trigger: false, model_connection_id: undefined });
    }} /></Form.Item>
    {!resources.plugins.length && <Typography.Paragraph type="secondary">暂无可用模板，请平台管理员授权已安装的插件。</Typography.Paragraph>}
    {choice && <Collapse items={[{ key: 'config', label: '模板参数', forceRender: true, children: <><Form.Item name="config_text" rules={required}><Input.TextArea rows={5} aria-label="模板参数 JSON" /></Form.Item><JsonView value={plugin?.config_schema} /></> }]} />}
    {updates && <>
      <Typography.Paragraph type="secondary" className="block-gap">由模板处理增量材料，候选需审核后发布；文档通过收件箱更新。</Typography.Paragraph>
      <Form.Item name="enabled" label="启用更新程序" valuePropName="checked"><Switch /></Form.Item>
      <Form.Item name="model_connection_id" label="模型连接"><Select allowClear placeholder="不使用大模型" options={resources.models.map(m => ({ value: m.id, label: m.name }))} /></Form.Item>
      <Form.Item name="auto_trigger" label="收到材料后自动生成候选" valuePropName="checked"><Switch /></Form.Item>
      <Form.Item name="max_batch_size" label="每批材料上限"><InputNumber min={1} max={100} /></Form.Item>
    </>}
  </>;
}

export function NewLibraryDialog({ api, organizations, open, onClose, onCreated }: {
  api: Api; organizations: { id: string; name: string; role: string }[]; open: boolean; onClose: () => void; onCreated: () => Promise<void>;
}) {
  return <Modal centered title="创建知识库" open={open} footer={null} onCancel={onClose} destroyOnHidden><NewLibraryForm api={api} organizations={organizations} onCreated={onCreated} /></Modal>;
}
function NewLibraryForm({ api, organizations, onCreated }: { api: Api; organizations: { id: string; name: string; role: string }[]; onCreated: () => Promise<void> }) {
  const [form] = Form.useForm<PluginValues & { name: string; organization_id: string }>();
  const org = Form.useWatch('organization_id', form) || '';
  const resource = useApiResource<Resources>(api, '/resources?' + query({ organization_id: org }));
  return <AsyncForm form={form} initial={{ organization_id: '', config_text: '{}', max_batch_size: 10 }} submitText="创建" submit={async v => {
    await api.request('/libraries', 'POST', { name: v.name, organization_id: v.organization_id || null, template: v.template_choice ? pluginPayload(v) : null });
    await onCreated();
  }}>
    <Form.Item name="name" label="名称" rules={required}><Input maxLength={80} /></Form.Item>
    <Form.Item name="organization_id" label="归属"><Select options={[{ value: '', label: '个人私有' }, ...organizations.filter(o => o.role === 'admin').map(o => ({ value: o.id, label: o.name }))]} onChange={() => form.setFieldsValue({ template_choice: undefined })} /></Form.Item>
    <LoadError error={resource.error} retry={resource.refresh} />
    {resource.loading ? <Spin /> : resource.data && <PluginFields resources={resource.data} optional />}
  </AsyncForm>;
}

export function LibraryPluginSettings({ api, libraryId, onChanged }: { api: Api; libraryId: string; onChanged: () => void }) {
  const base = `/libraries/${libraryId}/plugin-settings`;
  const resource = useApiResource<Resources>(api, '/resources?' + query({ library_id: libraryId }));
  const settings = useApiResource<Settings>(api, base);
  const { message, modal } = App.useApp();
  if (resource.loading || settings.loading) return <Spin />;
  if (resource.error || settings.error) return <LoadError error={resource.error || settings.error} retry={() => { resource.refresh(); settings.refresh(); }} />;
  if (!resource.data || !settings.data) return null;
  const b = settings.data.binding, choice = b ? b.plugin_id + '@' + b.plugin_version : undefined;
  const available = !choice || resource.data.plugins.some(p => p.id + '@' + p.version === choice);
  return <>
    {(!available || settings.data.unavailable) && <Alert type="warning" title="当前绑定资源不可用，请重新选择授权资源或联系平台管理员。" />}
    <AsyncForm<PluginValues> key={JSON.stringify(settings.data)} initial={{ ...settings.data.policy, template_choice: choice, config_text: JSON.stringify(b?.config || {}, null, 2) }} submitText="保存模板与更新设置" submit={async v => {
      const save = async () => { await api.request(base, 'PUT', pluginPayload(v)); settings.refresh(); onChanged(); message.success('已保存'); };
      if (choice !== v.template_choice) await new Promise<void>((resolve, reject) => modal.confirm({ title: '更换知识库模板？', content: '更新模板接管后，文档改由增量收件箱更新；已有未完成草稿需先处理。', onOk: async () => { await save(); resolve(); }, onCancel: () => reject(new Error('已取消')) }));
      else await save();
    }}><PluginFields resources={resource.data} /></AsyncForm>
  </>;
}

interface InboxItem { id: string; title: string; kind: string; created_at: string; status: string; submitted_by: { type: string }; material?: { content: { text: string } }; result?: unknown; update_job?: { status: string } }
const inboxStates: Record<string, string> = { pending: '待处理', processing: '处理中', completed: '已处理' };
const kinds: Record<string, string> = { new_content: '新内容', correction: '纠错', observation: '观察记录' };

export function LibraryInbox({ api, libraryId }: { api: Api; libraryId: string }) {
  const [after, setAfter] = useState(0), [selected, setSelected] = useState<string>(), [create, setCreate] = useState(false);
  const [submissionId, setSubmissionId] = useState('');
  const base = `/libraries/${libraryId}/inbox`, { message } = App.useApp();
  const resource = useApiResource<{ items: InboxItem[]; next_after: number | null }>(api, base + '?after=' + after, 5000);
  const detail = useApiResource<InboxItem>(api, selected ? base + '/' + selected : null, 5000);
  return <><Space className="block-gap"><Button type="primary" onClick={() => { setSubmissionId(crypto.randomUUID()); setCreate(true); }}>提交材料</Button><Button onClick={resource.refresh}>刷新</Button></Space>
    <LoadError error={resource.error} retry={resource.refresh} />
    <Table rowKey="id" loading={resource.loading} dataSource={resource.data?.items} pagination={false} columns={[
      { title: '材料', render: (_, r) => <Button type="link" onClick={() => setSelected(r.id)}>{r.title || '未命名材料'}</Button> },
      { title: '类型', render: (_, r) => kinds[r.kind] }, { title: '来源', render: (_, r) => r.submitted_by.type === 'user' ? '人员' : 'Agent' },
      { title: '状态', render: (_, r) => <Tag>{inboxStates[r.status]}</Tag> }, { title: '提交时间', render: (_, r) => date(r.created_at) },
    ]} />
    {(after > 0 || resource.data?.next_after != null) && <Space><Button disabled={!after} onClick={() => setAfter(0)}>首页</Button><Button disabled={resource.data?.next_after == null} onClick={() => setAfter(resource.data!.next_after!)}>下一页</Button></Space>}
    <Modal title="提交增量材料" open={create} footer={null} onCancel={() => setCreate(false)} destroyOnHidden><AsyncForm<{ title: string; kind: string; text: string }> initial={{ kind: 'new_content' }} submitText="提交" submit={async v => {
      await api.request(base, 'POST', { submission_id: submissionId, library_id: libraryId, title: v.title, kind: v.kind, content: { format: 'markdown', text: v.text } }); setCreate(false); resource.refresh(); message.success('已存入收件箱，尚未发布到知识库');
    }}><Form.Item name="title" label="标题" rules={required}><Input maxLength={200} /></Form.Item><Form.Item name="kind" label="类型"><Select options={Object.entries(kinds).map(([value, label]) => ({ value, label }))} /></Form.Item><Form.Item name="text" label="内容" rules={required}><Input.TextArea rows={8} maxLength={32768} /></Form.Item></AsyncForm></Modal>
    <Drawer title="材料详情" open={!!selected} onClose={() => setSelected(undefined)} size={680} destroyOnHidden><LoadError error={detail.error} retry={detail.refresh} />{detail.loading && <Spin />}{detail.data && <><Typography.Title level={5}>{detail.data.title}</Typography.Title><Tag>{inboxStates[detail.data.status]}</Tag><pre className="code-block">{detail.data.material?.content.text}</pre><JsonView value={detail.data.result} /></>}</Drawer>
  </>;
}

export function LibraryUpdateJobs({ api, libraryId }: { api: Api; libraryId: string }) {
  return <UpdatesPanel api={api} libraryId={libraryId} />;
}
