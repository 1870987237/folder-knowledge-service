import { useEffect, useState } from 'react';
import { Alert, App, Button, Collapse, Descriptions, Drawer, Empty, Form, Input, Modal, Select, Space, Spin, Table, Tag, Tabs, Typography, Upload } from 'antd';
import { PlusOutlined, UploadOutlined } from '@ant-design/icons';
import { useResource, useSession } from '../context';
import { errorMessage, parseObject } from '../api';
import { AsyncForm, JsonView, LoadError, PageHeader, PathInput, required, Surface } from '../components';
import type { PluginBindingValue, Library, Template, Workspace } from '../types';

const url = (t: Template) => `/plugins/${encodeURIComponent(t.definition.id)}/versions/${encodeURIComponent(t.definition.version)}`;
export function TemplatesPage() {
  const { api, refresh } = useSession(), { message } = App.useApp();
  const [offset, setOffset] = useState(0), [register, setRegister] = useState(false), [selected, setSelected] = useState<Template>();
  const [uploading, setUploading] = useState(false), [uploadError, setUploadError] = useState('');
  const resource = useResource<{ plugins: Template[]; next_offset: number | null }>(`/plugins?offset=${offset}&limit=50`);
  const [form] = Form.useForm<{ json: string }>();
  const [zip, setZip] = useState<File>();
  const [installing, setInstalling] = useState(false);
  const [installError, setInstallError] = useState('');
  const openInstall = () => { form.resetFields(); setZip(undefined); setInstallError(''); setRegister(true); };
  const detail = useResource<Template>(selected ? url(selected) : null);
  const current = detail.data || selected;
  return <><PageHeader title="插件" action={<Button type="primary" icon={<PlusOutlined />} onClick={openInstall}>安装插件</Button>} /><LoadError error={resource.error} retry={resource.refresh} />
    <Surface><Table<Template> rowKey={r => r.definition.id + '/' + r.definition.version} loading={resource.loading} dataSource={resource.data?.plugins} pagination={false} locale={{ emptyText: <Empty description="暂无插件" image={Empty.PRESENTED_IMAGE_SIMPLE}><Button onClick={openInstall}>安装第一个插件</Button></Empty> }} columns={[
      { title: '插件', render: (_, r) => <div className="name-cell"><Button type="link" className="name-link" onClick={() => { setSelected(r); setUploadError(''); }}>{r.definition.name}</Button><Typography.Text type="secondary" className="code-text">{r.definition.id}</Typography.Text></div> },
      { title: '版本', dataIndex: ['definition', 'version'], width: 100 },
      { title: '开发者', dataIndex: ['definition', 'maintainer'] },
      { title: '绑定知识库', width: 110, render: (_, r) => r.libraries?.length || 0 },
      { title: '能力', render: (_, r) => <Capabilities plugin={r} /> },
      { title: '状态', render: (_, r) => <Tag color={r.status === 'installed' ? 'success' : 'warning'}>{r.status === 'installed' ? '已安装' : '待上传'}</Tag> },
      { title: '操作', width: 120, render: (_, r) => <Button type="link" onClick={() => { setSelected(r); setUploadError(''); }}>{r.status === 'installed' ? '查看详情' : '上传程序包'}</Button> },
    ]} /></Surface>
    {(offset > 0 || resource.data?.next_offset != null) && <div className="pager"><Button disabled={!offset} onClick={() => setOffset(0)}>首页</Button><Button disabled={resource.data?.next_offset == null} onClick={() => setOffset(resource.data!.next_offset!)}>下一页</Button></div>}
    <Modal title="安装插件" open={register} onCancel={() => { if (!installing) setRegister(false); }} maskClosable={!installing} closable={!installing} footer={null} width={720} destroyOnHidden>
      <Form form={form} layout="vertical" onFinish={async values => {
        if (!zip) { setInstallError('请选择 package.zip'); return; }
        setInstalling(true); setInstallError('');
        try {
          const registered = await api.request<Template>('/plugins', 'POST', parseObject(values.json));
          const installed = await api.request<Template>(url(registered) + '/package', 'PUT', zip);
          setRegister(false); setSelected(installed); refresh(); message.success('插件已安装');
        } catch (e) { setInstallError(errorMessage(e)); refresh(); } finally { setInstalling(false); }
      }}>
        <Upload accept=".json,application/json" disabled={installing} showUploadList={false} beforeUpload={async file => { try { if (file.size > 65536) throw new Error('清单不能超过 64 KiB。'); form.setFieldValue('json', JSON.stringify(parseObject(await file.text()), null, 2)); } catch (e) { message.error(errorMessage(e)); } return false; }}><Button icon={<UploadOutlined />}>导入 plugin.json</Button></Upload>
        <Form.Item label="插件清单" name="json" rules={required} className="block-gap"><Input.TextArea disabled={installing} rows={10} className="json-input" placeholder="也可导入原有 registration.json" /></Form.Item>
        <Form.Item label="程序包" required><Upload accept=".zip" disabled={installing} maxCount={1} fileList={zip ? [{ uid: 'package', name: zip.name, status: 'done' }] : []} onRemove={() => { setZip(undefined); return true; }} beforeUpload={file => { if (file.size > 32 * 1024 * 1024) message.error('ZIP 不能超过 32 MiB。'); else setZip(file); return false; }}><Button icon={<UploadOutlined />}>选择 package.zip</Button></Upload></Form.Item>
        <LoadError error={installError} />
        <Typography.Paragraph type="secondary">版本不可覆盖；安装不会运行程序。上传失败可保留输入后重试。</Typography.Paragraph>
        <Button type="primary" htmlType="submit" loading={installing}>安装插件</Button>
      </Form>
    </Modal>
    <Drawer closable={{ placement: 'end' }} title={selected?.definition.name || '插件详情'} open={!!selected} onClose={() => setSelected(undefined)} size={680} destroyOnHidden>
      {selected && <><LoadError error={detail.error} retry={detail.refresh} /><Descriptions column={1} items={[
        { key: 'id', label: '插件 ID', children: selected.definition.id }, { key: 'version', label: '版本', children: selected.definition.version },
        { key: 'author', label: '开发者', children: selected.definition.maintainer }, { key: 'description', label: '说明', children: selected.definition.description || '—' },
        { key: 'status', label: '状态', children: <Tag color={selected.status === 'installed' ? 'success' : 'warning'}>{selected.status === 'installed' ? '已安装' : '待上传'}</Tag> },
      ]} /><LoadError error={uploadError} />
      {selected.status !== 'installed' && <Upload accept=".zip" showUploadList={false} disabled={uploading} beforeUpload={async file => {
        const target = selected; setUploading(true); setUploadError('');
        try { if (file.size > 32 * 1024 * 1024) throw new Error('ZIP 不能超过 32 MiB。'); const result = await api.request<Template>(url(target) + '/package', 'PUT', file); setSelected(current => current && url(current) === url(target) ? result : current); refresh(); message.success('程序包已安装'); }
        catch (e) { setUploadError(errorMessage(e)); } finally { setUploading(false); } return false;
      }}><Button type="primary" icon={<UploadOutlined />} loading={uploading}>上传 package.zip</Button></Upload>}
      {current && <><PluginDetails plugin={current} /><Typography.Title level={5}>绑定知识库</Typography.Title>
        {current.libraries?.length ? <Space wrap>{current.libraries.map(l => <Tag key={l.id}>{l.name}</Tag>)}</Space> : <Typography.Text type="secondary">尚未绑定</Typography.Text>}
        <Typography.Paragraph className="block-gap">在知识库的“插件配置”中绑定；更新程序需另行启用并审核发布。</Typography.Paragraph></>}
      <Collapse className="block-gap" items={[{ key: 'definition', label: '完整定义与校验摘要', children: <JsonView value={selected} /> }]} /></>}
    </Drawer>
  </>;
}

function Capabilities({ plugin }: { plugin: Template }) {
  const f = plugin.definition.plugin;
  return <Space wrap>{plugin.definition.capabilities.map(c => <Tag key={c}>{c === 'update' ? '增量更新' : '初始化'}</Tag>)}{f?.indexing && <Tag>索引定义</Tag>}{f?.instructions?.trim() && <Tag>Agent 指引</Tag>}</Space>;
}
function PluginDetails({ plugin }: { plugin: Template }) {
  const f = plugin.definition.plugin, index = f?.indexing;
  const searchable = index?.mode === 'fulltext' && index.splitter !== 'custom';
  return <Tabs items={[
    { key: 'capabilities', label: '能力', children: <><Capabilities plugin={plugin} /><Typography.Paragraph className="block-gap" type="secondary">{plugin.definition.capabilities.length ? '初始化与更新可接入现有任务流程。' : '此插件不修改知识文件。'}{index && (searchable ? ' 全文索引可在知识库的“检索索引”中构建。' : ' 该索引类型目前仅保存定义，尚未接入执行。')}{f?.instructions && ' Agent 指引尚未合入 MCP instructions。'}</Typography.Paragraph>{plugin.legacy_template && <Tag>兼容原有模板</Tag>}</> },
    ...(index ? [{ key: 'indexing', label: '索引定义', children: <Descriptions column={1} items={[
      { key: 'mode', label: '检索方式', children: ({ fulltext: '全文', semantic: '语义', hybrid: '混合' } as Record<string, string>)[index.mode] },
      { key: 'splitter', label: '切分方式', children: ({ document: '按文档分段', markdown_sections: 'Markdown 章节', custom: '插件程序' } as Record<string, string>)[index.splitter] },
      { key: 'include', label: '包含文件', children: index.include.join('、') }, { key: 'exclude', label: '排除文件', children: index.exclude.join('、') || '—' },
      { key: 'metadata', label: '元数据字段', children: index.metadata_fields.join('、') || '—' },
    ]} /> }] : []),
    ...(f?.instructions?.trim() ? [{ key: 'instructions', label: 'Agent 指引', children: <Typography.Paragraph style={{ whiteSpace: 'pre-wrap' }}>{f.instructions}</Typography.Paragraph> }] : []),
  ]} />;
}

interface BindingValues { template: string; mode: 'default' | 'custom'; root: string; subpath: string; excludes: string; config: string }
export function TemplateBinding({ library }: { library: Library }) {
  const { api, refresh } = useSession(), { message, modal } = App.useApp();
  const resource = useResource<{ binding: PluginBindingValue | null }>(`/libraries/${library.id}/plugin`);
  const [templates, setTemplates] = useState<Template[]>([]), [loading, setLoading] = useState(true), [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  const [form] = Form.useForm<BindingValues>();
  const mode = Form.useWatch('mode', form), template = Form.useWatch('template', form);
  useEffect(() => {
    let alive = true;
    async function load() { setLoading(true); try { let offset: number | null = 0; const all: Template[] = [];
      do { const result: { plugins: Template[]; next_offset: number | null } = await api.request(`/plugins?offset=${offset}&limit=100`); all.push(...result.plugins); offset = result.next_offset; } while (offset !== null);
      if (alive) { setTemplates(all.filter(t => t.status === 'installed')); setError(''); }
    } catch (e) { if (alive) setError(errorMessage(e)); } finally { if (alive) setLoading(false); } }
    void load(); return () => { alive = false; };
  }, [api, retry]);
  useEffect(() => { if (resource.data) { const b = resource.data.binding, w = b?.workspace; form.setFieldsValue({ template: b ? b.plugin_id + '/' + b.plugin_version : undefined, mode: w ? 'custom' : 'default', root: w?.root_path || w?.workspace_subpath || (library.repository ? '.' : library.path), subpath: w?.knowledge_subpath || '.', excludes: w?.exclude_paths.join('\n') || '', config: JSON.stringify(b?.config || {}, null, 2) }); } }, [resource.data, library.path, form]);
  const selected = templates.find(t => t.definition.id + '/' + t.definition.version === template);
  if (loading || resource.loading) return <Spin />;
  if (error || resource.error) return <LoadError error={error || resource.error} retry={() => { resource.refresh(); setRetry(v => v + 1); }} />;
  return <><div className="scope-note"><Typography.Text type="secondary">Agent 检索目录</Typography.Text><Typography.Text code>{library.path}</Typography.Text></div>
    {!templates.length && <Alert type="info" title="请先在插件页面安装程序包。" className="block-gap" />}
    <AsyncForm form={form} disabled={!templates.length} submitText="保存绑定" submit={async values => {
      const t = templates.find(t => t.definition.id + '/' + t.definition.version === values.template); if (!t) throw new Error('请选择已安装的插件版本。');
      let workspace: Workspace | undefined;
      if (t.definition.capabilities.length && values.mode === 'custom') {
        if (t.definition.protocol_version !== '1.1') throw new Error('完整工作范围需要协议 1.1 的插件。');
        workspace = { ...(library.repository ? { type: 'git' as const, repository_id: library.repository.repository_id, workspace_subpath: values.root.trim() } : { type: 'local' as const, root_path: values.root.trim() }), knowledge_subpath: values.subpath.trim(), exclude_paths: (values.excludes || '').split(/\r?\n/).map(s => s.trim()).filter(Boolean) };
      }
      await api.request(`/libraries/${library.id}/plugin`, 'PUT', { plugin_id: t.definition.id, plugin_version: t.definition.version, config: parseObject(values.config), ...(workspace ? { workspace } : {}) }); refresh(); message.success('插件绑定已保存');
    }} footer={resource.data?.binding && <Button danger onClick={() => modal.confirm({ title: '解除插件绑定？', content: '知识文件与插件程序包会保留。', onOk: async () => { try { await api.request(`/libraries/${library.id}/plugin`, 'DELETE'); refresh(); message.success('已解除绑定'); } catch (e) { message.error(errorMessage(e)); throw e; } } })}>解除绑定</Button>}>
      <Form.Item name="template" label="插件版本" rules={required}><Select showSearch optionFilterProp="label" placeholder="选择已安装的版本" options={templates.map(t => ({ value: t.definition.id + '/' + t.definition.version, label: `${t.definition.name} · ${t.definition.version}` }))} /></Form.Item>
      {!!selected?.definition.capabilities.length && <Form.Item name="mode" label="更新程序工作范围"><Select options={[{ value: 'default', label: '仅当前检索目录' }, { value: 'custom', label: '指定完整工作目录' }]} /></Form.Item>}
      {!!selected?.definition.capabilities.length && mode === 'custom' && <><Form.Item name="root" label={library.repository ? '工作根（相对 Git 仓库根）' : '工作根（相对挂载根）'} rules={required}>{library.repository ? <Input /> : <PathInput />}</Form.Item><Form.Item name="subpath" label="检索子目录（相对工作根）" rules={required}><Input /></Form.Item><Form.Item name="excludes" label="额外排除路径（每行一项）"><Input.TextArea rows={3} /></Form.Item><Alert type="info" title="工作根与检索子目录须对应当前挂载；.git、.env、私钥等默认排除。" className="block-gap" /></>}
      <Form.Item name="config" label="实例配置（JSON）" rules={required}><Input.TextArea rows={6} className="json-input" /></Form.Item>
      {selected && <PluginDetails plugin={selected} />}
      {selected && <Collapse items={[{ key: 'schema', label: '配置规范', children: <JsonView value={selected.definition.config_schema} /> }]} />}
      <Typography.Paragraph type="secondary" className="block-gap">保存仅更新绑定，不启动程序或发布知识。</Typography.Paragraph>
    </AsyncForm>
  </>;
}
