import { useState } from 'react';
import { Alert, App, Button, Empty, Form, Input, List, Space, Spin, Tag, Typography } from 'antd';
import { useApiResource, useSession } from '../context';
import { date, LoadError, required } from '../components';
import { type Api, errorMessage } from '../api';
import type { Library } from '../types';

interface IndexState { status: string; message?: string; files?: number; chunks?: number; built_at?: string; supports_path_filter?: boolean }
interface Hit { path: string; start_line: number; end_line: number; title: string; text: string; score: number }
interface Result { results: Hit[]; notice: string; has_more?: boolean }
const statuses: Record<string, string> = { ready: '已就绪', stale: '需要重建', missing: '尚未构建', unconfigured: '未配置', unsupported: '暂不支持', unavailable: '不可用' };

export function RetrievalSettings({ library }: { library: Library }) {
  const { api } = useSession();
  return <RetrievalPanel api={api} libraryId={library.id} />;
}

export function RetrievalPanel({ api, libraryId, searchPath = '/search' }: { api: Api; libraryId: string; searchPath?: string }) {
  const { message } = App.useApp();
  const base = `/libraries/${encodeURIComponent(libraryId)}`;
  const state = useApiResource<IndexState>(api, base + '/index');
  const [busy, setBusy] = useState(false), [searching, setSearching] = useState(false), [error, setError] = useState('');
  const [result, setResult] = useState<Result>();
  const blocked = !state.data || ['unconfigured', 'unsupported'].includes(state.data.status);
  return <><LoadError error={state.error || error} retry={state.refresh} />
    {state.loading ? <Spin /> : <Space wrap>
      <Tag color={state.data?.status === 'ready' ? 'success' : 'default'}>{statuses[state.data?.status || ''] || '未知'}</Tag>
      {state.data?.built_at && <Typography.Text type="secondary">{state.data.files} 个文件 · {state.data.chunks} 个片段 · {date(state.data.built_at)}</Typography.Text>}
      <Button type="primary" disabled={blocked} loading={busy} onClick={async () => {
        setBusy(true); setError(''); setResult(undefined);
        try { await api.request(base + '/index/build', 'POST'); state.refresh(); message.success('索引已构建'); }
        catch (e) { setError(errorMessage(e)); } finally { setBusy(false); }
      }}>{state.data?.built_at ? '重建索引' : '构建索引'}</Button>
      <Button disabled={busy} onClick={() => { setResult(undefined); state.refresh(); }}>检查状态</Button>
    </Space>}
    {blocked && !state.loading && <Alert className="block-gap" type="info" title="请先绑定支持检索的插件。" />}
    {state.data?.status === 'stale' && <Alert className="block-gap" type="warning" title="知识文件或插件配置已变化，请重建索引后再检索。" />}
    {state.data?.status === 'unavailable' && <Alert className="block-gap" type="warning" title={state.data.message || '索引不可用'} />}
    <Form layout="vertical" className="block-gap" onFinish={async ({ query, path }: { query: string; path?: string }) => {
      setSearching(true); setError(''); setResult(undefined);
      try { setResult(await api.request<Result>(base + searchPath + '?query=' + encodeURIComponent(query) + '&limit=5&path=' + encodeURIComponent(state.data?.supports_path_filter ? path?.trim() || '.' : '.'))); }
      catch (e) { setError(errorMessage(e)); state.refresh(); } finally { setSearching(false); }
    }}>
      <Form.Item name="query" label="检索测试" rules={required}><Input maxLength={200} placeholder="输入关键词或问题" /></Form.Item>
      {state.data?.supports_path_filter && <Form.Item name="path" label="目录范围"><Input maxLength={240} placeholder="全部目录；可填 docs 等相对路径" /></Form.Item>}
      <Button htmlType="submit" disabled={busy || state.data?.status !== 'ready'} loading={searching}>检索</Button>
    </Form>
    {result?.has_more && <Typography.Paragraph className="block-gap" type="secondary">当前显示前 5 条结果，可缩小目录范围或调整问题。</Typography.Paragraph>}
    {result && <List className="block-gap" dataSource={result.results} locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有找到匹配证据，可调整关键词再试" /> }} renderItem={hit => <List.Item><div>
      <Typography.Text strong>{hit.title || hit.path}</Typography.Text>
      <Typography.Paragraph type="secondary">{hit.path} · 第 {hit.start_line}–{hit.end_line} 行</Typography.Paragraph>
      <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{hit.text}</Typography.Paragraph>
    </div></List.Item>} />}
    <Typography.Paragraph className="block-gap" type="secondary">检索由绑定插件执行；文件更新后需重建索引。</Typography.Paragraph>
  </>;
}
