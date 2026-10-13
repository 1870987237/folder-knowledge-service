import { useState } from 'react';
import { Alert, App, Button, Form, Input, Tabs } from 'antd';
import { type Api, errorMessage } from './api';
import { LibraryAgentSettings } from './KeyPolicy';
import { LibraryPluginSettings } from './LibraryUpdates';
import { RetrievalPanel } from './pages/Retrieval';

export function LibrarySettings({ api, library, onChanged }: {
  api: Api; library: { id: string; name: string }; onChanged: (name: string) => void;
}) {
  return <Tabs destroyOnHidden items={[
    { key: 'basic', label: '基本信息', children: <BasicSettings api={api} library={library} onChanged={onChanged} /> },
    { key: 'plugin', label: '模板与更新', children: <LibraryPluginSettings api={api} libraryId={library.id} onChanged={() => onChanged(library.name)} /> },
    { key: 'agent', label: 'Agent 接入', children: <LibraryAgentSettings api={api} id={library.id} /> },
    { key: 'index', label: '检索索引', children: <RetrievalPanel api={api} libraryId={library.id} searchPath="/index/search" /> },
  ]} />;
}

function BasicSettings({ api, library, onChanged }: {
  api: Api; library: { id: string; name: string }; onChanged: (name: string) => void;
}) {
  const { message } = App.useApp();
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  return <Form layout="vertical" initialValues={{ name: library.name }} onFinish={async ({ name }: { name: string }) => {
    setBusy(true); setError('');
    try {
      const saved = await api.request<{ name: string }>(`/libraries/${encodeURIComponent(library.id)}/settings`, 'PUT', { name });
      onChanged(saved.name); message.success('已保存');
    } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); }
  }}>
    {error && <Alert type="error" title={error} />}
    <Form.Item name="name" label="知识库名称" rules={[{ required: true, whitespace: true }]}><Input maxLength={80} /></Form.Item>
    <Button type="primary" htmlType="submit" loading={busy}>保存</Button>
  </Form>;
}
