import { App, Button, Collapse, Form, Input, Select, Spin, Typography } from 'antd';
import { type Api, parseObject } from './api';
import { useApiResource } from './context';
import { AsyncForm, LoadError, required } from './components';

type Choice = { id: string; version: string; name: string; config_schema: { properties?: Record<string, { default?: unknown }> } };
type Binding = { plugin_id: string; plugin_version: string; config: object };

export function SearchPluginSettings({ api, libraryId, onChanged }: { api: Api; libraryId: string; onChanged: () => void }) {
  const base = `/libraries/${encodeURIComponent(libraryId)}`;
  const options = useApiResource<{ plugins: Choice[] }>(api, base + '/search-plugin-options');
  const current = useApiResource<{ binding: Binding | null; inherited: boolean }>(api, base + '/search-plugin');
  const { message, modal } = App.useApp();
  const [form] = Form.useForm<{ choice: string; config: string }>();
  if ((options.loading && !options.data) || (current.loading && !current.data)) return <Spin />;
  if (options.error || current.error) return <LoadError error={options.error || current.error} retry={() => { options.refresh(); current.refresh(); }} />;
  const b = current.data?.binding, items = options.data?.plugins || [];
  const refresh = () => { current.refresh(); onChanged(); };
  return <Collapse className="block-gap" items={[{ key: 'plugin', label: '检索插件', children: <>
    <Typography.Paragraph type="secondary">独立于知识管理插件，保存后手动构建索引。{current.data?.inherited && '当前沿用原插件的检索设置。'}</Typography.Paragraph>
    <AsyncForm key={JSON.stringify(current.data)} form={form} initial={{ choice: b ? `${b.plugin_id}@${b.plugin_version}` : undefined, config: JSON.stringify(b?.config || {}, null, 2) }} submitText="保存检索插件" submit={async v => {
      const [plugin_id, plugin_version] = v.choice.split('@');
      await api.request(base + '/search-plugin', 'PUT', { plugin_id, plugin_version, config: parseObject(v.config) });
      refresh(); message.success('检索插件已保存，请构建索引');
    }} footer={b && <Button danger onClick={() => modal.confirm({ title: '停用检索插件？', content: '知识文档和更新管线保留，Search 将不可用。', onOk: async () => { await api.request(base + '/search-plugin', 'DELETE'); refresh(); } })}>停用检索</Button>}>
      <Form.Item name="choice" label="检索插件版本" rules={required}><Select placeholder="选择可用的检索插件" options={items.map(p => ({ value: `${p.id}@${p.version}`, label: `${p.name} · ${p.version}` }))} onChange={v => {
        const p = items.find(p => `${p.id}@${p.version}` === v);
        form.setFieldValue('config', JSON.stringify(Object.fromEntries(Object.entries(p?.config_schema.properties || {}).filter(([, v]) => v.default !== undefined).map(([k, v]) => [k, v.default])), null, 2));
      }} /></Form.Item>
      <Form.Item name="config" label="检索参数（JSON）" rules={required}><Input.TextArea rows={4} /></Form.Item>
    </AsyncForm>
  </> }]} />;
}
