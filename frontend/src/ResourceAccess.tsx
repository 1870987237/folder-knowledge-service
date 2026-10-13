import { Alert, App, Checkbox, Collapse, Form, Input, Select, Spin } from 'antd';
import { type Api, parseObject } from './api';
import { useApiResource } from './context';
import { AsyncForm, LoadError } from './components';

interface Access { organization_ids: string[]; user_ids: string[]; approved: boolean; model_environment: Record<string, string> }
interface Subjects { organizations: { id: string; name: string }[]; users: { id: string; username: string }[] }

export function ResourceAccess({ api, kind, resourceId }: { api: Api; kind: 'plugin' | 'model'; resourceId: string }) {
  const path = `/resource-access/${kind}/${encodeURIComponent(resourceId)}`;
  const access = useApiResource<Access>(api, path), subjects = useApiResource<Subjects>(api, '/resource-subjects');
  const { message, modal } = App.useApp();
  if (access.loading || subjects.loading) return <Spin />;
  if (access.error || subjects.error) return <LoadError error={access.error || subjects.error} retry={() => { access.refresh(); subjects.refresh(); }} />;
  if (!access.data || !subjects.data) return null;
  return <AsyncForm<Access & { mapping: string }> key={JSON.stringify(access.data)} initial={{ ...access.data, mapping: JSON.stringify(access.data.model_environment, null, 2) }} submitText="保存使用授权" submit={async values => {
    const save = async () => { await api.request(path, 'PUT', { organization_ids: values.organization_ids, user_ids: values.user_ids, approved: values.approved, model_environment: parseObject(values.mapping) }); access.refresh(); message.success('使用授权已保存'); };
    const removed = access.data!.approved && (!values.approved || access.data!.organization_ids.some(id => !values.organization_ids.includes(id)) || access.data!.user_ids.some(id => !values.user_ids.includes(id)));
    if (removed) await new Promise<void>((resolve, reject) => modal.confirm({ title: '撤销所选资源授权？', content: '相关知识库将无法继续运行该资源，待发布任务也会重新检查授权。', onOk: async () => { await save(); resolve(); }, onCancel: () => reject(new Error('已取消')) }));
    else await save();
  }}>
    <Form.Item name="approved" valuePropName="checked"><Checkbox>{kind === 'plugin' ? '已审阅并批准当前版本程序包' : '允许分配此模型连接'}</Checkbox></Form.Item>
    <Form.Item name="organization_ids" label="可使用的组织"><Select mode="multiple" optionFilterProp="label" options={subjects.data.organizations.map(o => ({ value: o.id, label: o.name }))} /></Form.Item>
    <Form.Item name="user_ids" label="可用于个人知识库的账号"><Select mode="multiple" optionFilterProp="label" options={subjects.data.users.map(u => ({ value: u.id, label: u.username }))} /></Form.Item>
    {kind === 'plugin' && <Collapse items={[{ key: 'mapping', label: '模型变量映射', forceRender: true, children: <><Alert type="info" title="变量名按插件要求填写；值为 base_url、model 或 api_key。" /><Form.Item name="mapping"><Input.TextArea rows={5} /></Form.Item></> }]} />}
    {kind === 'model' && <Form.Item name="mapping" hidden><Input /></Form.Item>}
  </AsyncForm>;
}
