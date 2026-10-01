import { useState } from 'react';
import { Alert, App, Button, Empty, Form, Input, List, Space, Spin, Tag, Typography } from 'antd';
import { useResource, useSession } from '../context';
import { date, LoadError, required } from '../components';
import { errorMessage } from '../api';
import type { Library } from '../types';

interface IndexState { status: string; message?: string; files?: number; chunks?: number; built_at?: string }
interface Hit { path: string; start_line: number; end_line: number; title: string; text: string; score: number }
interface Result { results: Hit[]; notice: string }
const statuses: Record<string, string> = { ready: '已就绪', stale: '需要重建', missing: '尚未构建', unconfigured: '未配置', unsupported: '暂不支持', unavailable: '不可用' };

export function RetrievalSettings({ library }: { library: Library }) {
  const { api } = useSession(), { message } = App.useApp();
  const base = `/libraries/${encodeURIComponent(library.id)}`;
  const state = useResource<IndexState>(base + '/index');
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
    {blocked && !state.loading && <Alert className="block-gap" type="info" title="请先绑定简易 RAG 插件，或使用全文索引与内置分段规则。" />}
    {state.data?.status === 'stale' && <Alert className="block-gap" type="warning" title="知识文件或插件配置已变化，请重建索引后再检索。" />}
    {state.data?.status === 'unavailable' && <Alert className="block-gap" type="warning" title={state.data.message || '索引不可用'} />}
    <Form layout="vertical" className="block-gap" onFinish={async ({ query }: { query: string }) => {
      setSearching(true); setError(''); setResult(undefined);
      try { setResult(await api.request<Result>(base + '/search?query=' + encodeURIComponent(query) + '&limit=5')); }
      catch (e) { setError(errorMessage(e)); state.refresh(); } finally { setSearching(false); }
    }}>
      <Form.Item name="query" label="检索测试" rules={required}><Input maxLength={200} placeholder="输入关键词或问题" /></Form.Item>
      <Button htmlType="submit" disabled={busy || state.data?.status !== 'ready'} loading={searching}>检索</Button>
    </Form>
    {result && <List className="block-gap" dataSource={result.results} locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有找到匹配证据，可调整关键词再试" /> }} renderItem={hit => <List.Item><div>
      <Typography.Text strong>{hit.title || hit.path}</Typography.Text>
      <Typography.Paragraph type="secondary">{hit.path} · 第 {hit.start_line}–{hit.end_line} 行</Typography.Paragraph>
      <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{hit.text}</Typography.Paragraph>
    </div></List.Item>} />}
    <Typography.Paragraph className="block-gap" type="secondary">全文检索返回证据，由 Agent 生成回答。索引与知识文件分开保存；文件更新后需手动重建。</Typography.Paragraph>
  </>;
}
