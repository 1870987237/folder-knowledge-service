import { useEffect, useState } from 'react';
import { Alert, App, Button, Collapse, Descriptions, Drawer, Empty, Form, Input, Modal, Select, Space, Spin, Table, Tag, Typography, Upload } from 'antd';
import { PlusOutlined, UploadOutlined } from '@ant-design/icons';
import { useResource, useSession } from '../context';
import { errorMessage, parseObject } from '../api';
import { AsyncForm, JsonView, LoadError, PageHeader, PathInput, required, Surface } from '../components';
import type { Binding, Library, Template, Workspace } from '../types';

const url = (t: Template) => `/templates/${encodeURIComponent(t.definition.id)}/versions/${encodeURIComponent(t.definition.version)}`;
export function TemplatesPage() {
  const { api, refresh } = useSession(), { message } = App.useApp();
  const [offset, setOffset] = useState(0), [register, setRegister] = useState(false), [selected, setSelected] = useState<Template>();
  const [uploading, setUploading] = useState(false), [uploadError, setUploadError] = useState('');
  const resource = useResource<{ templates: Template[]; next_offset: number | null }>(`/templates?offset=${offset}&limit=50`);
  const [form] = Form.useForm<{ json: string }>();
  return <><PageHeader title="模板" action={<Button type="primary" icon={<PlusOutlined />} onClick={() => { form.resetFields(); setRegister(true); }}>注册模板</Button>} /><LoadError error={resource.error} retry={resource.refresh} />
    <Surface><Table<Template> rowKey={r => r.definition.id + '/' + r.definition.version} loading={resource.loading} dataSource={resource.data?.templates} pagination={false} locale={{ emptyText: <Empty description="暂无模板" image={Empty.PRESENTED_IMAGE_SIMPLE}><Button onClick={() => setRegister(true)}>注册第一个模板</Button></Empty> }} columns={[
      { title: '模板', render: (_, r) => <div className="name-cell"><Button type="link" className="name-link" onClick={() => { setSelected(r); setUploadError(''); }}>{r.definition.name}</Button><Typography.Text type="secondary" className="code-text">{r.definition.id}</Typography.Text></div> },
      { title: '版本', dataIndex: ['definition', 'version'], width: 100 },
      { title: '开发者', dataIndex: ['definition', 'maintainer'] },
      { title: '能力', render: (_, r) => <Space wrap>{r.definition.capabilities.map(c => <Tag key={c}>{c === 'update' ? '增量更新' : '初始化'}</Tag>)}</Space> },
      { title: '状态', render: (_, r) => <Tag color={r.status === 'installed' ? 'success' : 'warning'}>{r.status === 'installed' ? '已安装' : '待上传'}</Tag> },
      { title: '操作', width: 120, render: (_, r) => <Button type="link" onClick={() => { setSelected(r); setUploadError(''); }}>{r.status === 'installed' ? '查看详情' : '上传程序包'}</Button> },
    ]} /></Surface>
    <div className="pager"><Button disabled={!offset} onClick={() => setOffset(0)}>首页</Button><Button disabled={resource.data?.next_offset == null} onClick={() => setOffset(resource.data!.next_offset!)}>下一页</Button></div>
    <Modal title="注册模板" open={register} onCancel={() => setRegister(false)} footer={null} width={720} destroyOnHidden>
      <AsyncForm form={form} submitText="注册模板" submit={async values => { const result = await api.request<Template>('/templates', 'POST', parseObject(values.json)); setRegister(false); setSelected(result); setUploadError(''); refresh(); message.success('模板已注册'); }}>
        <Upload accept=".json,application/json" showUploadList={false} beforeUpload={async file => { try { if (file.size > 65536) throw new Error('元数据文件不能超过 64 KiB。'); form.setFieldValue('json', JSON.stringify(parseObject(await file.text()), null, 2)); } catch (e) { message.error(errorMessage(e)); } return false; }}><Button icon={<UploadOutlined />}>导入 registration.json</Button></Upload>
        <Form.Item label="模板元数据" name="json" rules={required} className="block-gap"><Input.TextArea rows={15} className="json-input" placeholder="粘贴开发者提供的 registration.json" /></Form.Item>
        <Typography.Text type="secondary">同一版本的定义固定，变更请注册新版本。</Typography.Text>
      </AsyncForm>
    </Modal>
    <Drawer closable={{ placement: 'end' }} title={selected?.definition.name || '模板详情'} open={!!selected} onClose={() => setSelected(undefined)} size={680} destroyOnHidden>
      {selected && <><Descriptions column={1} items={[
        { key: 'id', label: '模板 ID', children: selected.definition.id }, { key: 'version', label: '版本', children: selected.definition.version },
        { key: 'author', label: '开发者', children: selected.definition.maintainer }, { key: 'description', label: '说明', children: selected.definition.description || '—' },
        { key: 'status', label: '状态', children: <Tag color={selected.status === 'installed' ? 'success' : 'warning'}>{selected.status === 'installed' ? '已安装' : '待上传'}</Tag> },
      ]} /><LoadError error={uploadError} />
      {selected.status !== 'installed' && <Upload accept=".zip" showUploadList={false} disabled={uploading} beforeUpload={async file => {
        const target = selected; setUploading(true); setUploadError('');
        try { if (file.size > 32 * 1024 * 1024) throw new Error('ZIP 不能超过 32 MiB。'); const result = await api.request<Template>(url(target) + '/package', 'PUT', file); setSelected(current => current && url(current) === url(target) ? result : current); refresh(); message.success('程序包已安装'); }
        catch (e) { setUploadError(errorMessage(e)); } finally { setUploading(false); } return false;
      }}><Button type="primary" icon={<UploadOutlined />} loading={uploading}>上传 package.zip</Button></Upload>}
      <Alert className="block-gap" type="info" title="当前支持安装和绑定；程序自动调度与发布尚未开放。" />
      <Collapse className="block-gap" items={[{ key: 'definition', label: '完整定义与校验摘要', children: <JsonView value={selected} /> }]} /></>}
    </Drawer>
  </>;
}

interface BindingValues { template: string; mode: 'default' | 'custom'; root: string; subpath: string; excludes: string; config: string }
export function TemplateBinding({ library }: { library: Library }) {
  const { api, refresh } = useSession(), { message, modal } = App.useApp();
  const resource = useResource<{ binding: Binding | null }>(`/libraries/${library.id}/template`);
  const [templates, setTemplates] = useState<Template[]>([]), [loading, setLoading] = useState(true), [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  const [form] = Form.useForm<BindingValues>();
  const mode = Form.useWatch('mode', form), template = Form.useWatch('template', form);
  useEffect(() => {
    let alive = true;
    async function load() { setLoading(true); try { let offset: number | null = 0; const all: Template[] = [];
      do { const result: { templates: Template[]; next_offset: number | null } = await api.request(`/templates?offset=${offset}&limit=100`); all.push(...result.templates); offset = result.next_offset; } while (offset !== null);
      if (alive) { setTemplates(all.filter(t => t.status === 'installed' && t.definition.capabilities.includes('update'))); setError(''); }
    } catch (e) { if (alive) setError(errorMessage(e)); } finally { if (alive) setLoading(false); } }
    void load(); return () => { alive = false; };
  }, [api, retry]);
  useEffect(() => { if (resource.data) { const b = resource.data.binding, w = b?.workspace; form.setFieldsValue({ template: b ? b.template_id + '/' + b.template_version : undefined, mode: w ? 'custom' : 'default', root: w?.root_path || w?.workspace_subpath || (library.repository ? '.' : library.path), subpath: w?.knowledge_subpath || '.', excludes: w?.exclude_paths.join('\n') || '', config: JSON.stringify(b?.config || {}, null, 2) }); } }, [resource.data, library.path, form]);
  const selected = templates.find(t => t.definition.id + '/' + t.definition.version === template);
  if (loading || resource.loading) return <Spin />;
  if (error || resource.error) return <LoadError error={error || resource.error} retry={() => { resource.refresh(); setRetry(v => v + 1); }} />;
  return <><div className="scope-note"><Typography.Text type="secondary">Agent 检索目录</Typography.Text><Typography.Text code>{library.path}</Typography.Text></div>
    {!templates.length && <Alert type="info" title="请先在模板页面安装支持增量更新的程序包。" className="block-gap" />}
    <AsyncForm form={form} disabled={!templates.length} submitText="保存绑定" submit={async values => {
      const t = templates.find(t => t.definition.id + '/' + t.definition.version === values.template); if (!t) throw new Error('请选择已安装的模板版本。');
      let workspace: Workspace | undefined;
      if (values.mode === 'custom') {
        if (t.definition.protocol_version !== '1.1') throw new Error('完整工作范围需要协议 1.1 的模板。');
        workspace = { ...(library.repository ? { type: 'git' as const, repository_id: library.repository.repository_id, workspace_subpath: values.root.trim() } : { type: 'local' as const, root_path: values.root.trim() }), knowledge_subpath: values.subpath.trim(), exclude_paths: (values.excludes || '').split(/\r?\n/).map(s => s.trim()).filter(Boolean) };
      }
      await api.request(`/libraries/${library.id}/template`, 'PUT', { template_id: t.definition.id, template_version: t.definition.version, config: parseObject(values.config), ...(workspace ? { workspace } : {}) }); refresh(); message.success('模板绑定已保存');
    }} footer={resource.data?.binding && <Button danger onClick={() => modal.confirm({ title: '解除模板绑定？', content: '知识文件与模板程序包会保留。', onOk: async () => { try { await api.request(`/libraries/${library.id}/template`, 'DELETE'); refresh(); message.success('已解除绑定'); } catch (e) { message.error(errorMessage(e)); throw e; } } })}>解除绑定</Button>}>
      <Form.Item name="template" label="模板版本" rules={required}><Select showSearch optionFilterProp="label" placeholder="选择已安装的版本" options={templates.map(t => ({ value: t.definition.id + '/' + t.definition.version, label: `${t.definition.name} · ${t.definition.version}${t.definition.required_secrets.length ? '（凭据注入暂未开放）' : ''}`, disabled: t.definition.required_secrets.length > 0 }))} /></Form.Item>
      <Form.Item name="mode" label="更新程序工作范围"><Select options={[{ value: 'default', label: '仅当前检索目录' }, { value: 'custom', label: '指定完整工作目录' }]} /></Form.Item>
      {mode === 'custom' && <><Form.Item name="root" label={library.repository ? '工作根（相对 Git 仓库根）' : '工作根（相对挂载根）'} rules={required}>{library.repository ? <Input /> : <PathInput />}</Form.Item><Form.Item name="subpath" label="检索子目录（相对工作根）" rules={required}><Input /></Form.Item><Form.Item name="excludes" label="额外排除路径（每行一项）"><Input.TextArea rows={3} /></Form.Item><Alert type="info" title="工作根与检索子目录须对应当前挂载；.git、.env、私钥等默认排除。" className="block-gap" /></>}
      <Form.Item name="config" label="实例配置（JSON）" rules={required}><Input.TextArea rows={6} className="json-input" /></Form.Item>
      {selected && <Collapse items={[{ key: 'schema', label: '配置规范', children: <JsonView value={selected.definition.config_schema} /> }]} />}
      <Typography.Paragraph type="secondary" className="block-gap">保存仅更新绑定，不启动程序或发布知识。</Typography.Paragraph>
    </AsyncForm>
  </>;
}
