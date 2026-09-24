import { useEffect, useState } from 'react';
import { Alert, App, Button, Collapse, Descriptions, Empty, Form, Input, Select, Space, Spin, Tag, Typography } from 'antd';
import { CheckOutlined, InboxOutlined, ReloadOutlined } from '@ant-design/icons';
import { query, errorMessage } from '../api';
import { useResource, useSession } from '../context';
import { AsyncForm, date, JsonView, LoadError, PageHeader, required, Surface } from '../components';
import type { InboxItem } from '../types';

const statuses: Record<string, string> = { pending: '待处理', processing: '处理中', completed: '已处理' };
const kinds: Record<string, string> = { new_content: '新增内容', correction: '纠错', observation: '观察记录' };
const outcomes: Record<string, string> = { accepted: '采纳', partially_accepted: '部分采纳', rejected: '不采纳' };
function Status({ status }: { status: string }) { return <Tag color={status === 'completed' ? 'success' : status === 'processing' ? 'processing' : 'default'}>{statuses[status]}</Tag>; }

export function InboxPage() {
  const { overview } = useSession();
  const [library, setLibrary] = useState<string>(), [status, setStatus] = useState<string>(), [after, setAfter] = useState(0), [selected, setSelected] = useState<string>();
  const resource = useResource<{ items: InboxItem[]; next_after: number | null }>('/inbox?' + query({ library_id: library, status, after, limit: 50 }), 10000);
  useEffect(() => { if (resource.data && !resource.data.items.some(r => r.id === selected)) setSelected(resource.data.items[0]?.id); }, [resource.data, selected]);
  return <><PageHeader title="增量收件箱" action={<Button icon={<ReloadOutlined />} onClick={resource.refresh} loading={resource.loading}>刷新</Button>} /><div className="toolbar"><Select aria-label="筛选知识库" placeholder="全部知识库" allowClear value={library} options={overview.libraries.map(l => ({ value: l.id, label: l.name }))} onChange={v => { setLibrary(v); setAfter(0); }} /><Select aria-label="筛选处理状态" placeholder="全部状态" allowClear value={status} options={Object.entries(statuses).map(([value, label]) => ({ value, label }))} onChange={v => { setStatus(v); setAfter(0); }} /></div><LoadError error={resource.error} retry={resource.refresh} />
    <div className="inbox-layout"><Surface className="inbox-list"><div className="section-heading"><Typography.Text strong>提交材料</Typography.Text><Typography.Text type="secondary">本页 {resource.data?.items.length || 0} 条</Typography.Text></div>
      <Spin spinning={resource.loading}>{resource.data?.items.length ? resource.data.items.map(row => <button type="button" className={'inbox-item ' + (row.id === selected ? 'selected' : '')} key={row.id} onClick={() => setSelected(row.id)} aria-pressed={row.id === selected}>
        <div className="inbox-item-title"><span>{row.title || row.submission_id}</span><Status status={row.status} /></div><div className="inbox-item-meta"><span>{overview.libraries.find(l => l.id === row.library_id)?.name || row.library_id}</span><span>{kinds[row.kind || ''] || '材料'}</span></div><div className="inbox-item-meta"><span>{date(row.created_at)}</span></div>
      </button>) : <Empty image={<InboxOutlined className="empty-icon" />} description="暂无材料" className="padded-empty" />}</Spin>
      {(after > 0 || resource.data?.next_after != null) && <div className="pager padded"><Button disabled={!after} onClick={() => setAfter(0)}>首页</Button><Button disabled={resource.data?.next_after == null} onClick={() => setAfter(resource.data!.next_after!)}>下一页</Button></div>}
    </Surface><Surface className="inbox-detail">{selected ? <MaterialDetail key={selected} id={selected} /> : <Empty description="选择材料查看详情" image={Empty.PRESENTED_IMAGE_SIMPLE} className="padded-empty" />}</Surface></div>
  </>;
}

function MaterialDetail({ id }: { id: string }) {
  const { api, overview, refresh, claims } = useSession(), { message } = App.useApp();
  const resource = useResource<InboxItem>('/inbox/' + id, 10000);
  const [clock, setClock] = useState(Date.now()), [busy, setBusy] = useState(false), [error, setError] = useState('');
  useEffect(() => { const timer = window.setInterval(() => setClock(Date.now()), 1000); return () => window.clearInterval(timer); }, []);
  const row = resource.data, material = row?.material;
  if (!row || !material) return <><LoadError error={resource.error} retry={resource.refresh} />{resource.loading && <Spin />}</>;
  const managed = row.update_job && ['queued','running','candidate_ready','publish_queued','publishing'].includes(row.update_job.status);
  const active = row.status === 'processing' && (row.lease_until || 0) * 1000 > clock;
  const own = active && claims.get(id)?.leaseUntil === row.lease_until, claimable = row.status !== 'completed' && !active && !managed;
  async function action(name: 'claim' | 'release') {
    setBusy(true); setError('');
    try { const result = await api.request<InboxItem>(`/inbox/${id}/${name}`, 'POST', name === 'claim' ? { lease_seconds: 900 } : { claim_token: claims.get(id)?.token });
      if (result.claim_token && result.lease_until) claims.set(id, { token: result.claim_token, leaseUntil: result.lease_until }); else claims.delete(id); refresh();
    } catch (e) { setError(errorMessage(e)); resource.refresh(); } finally { setBusy(false); }
  }
  return <><div className="detail-heading"><Typography.Title level={4}>{material.title || '未命名材料'}</Typography.Title><Status status={row.status} /></div><Descriptions size="small" column={1} items={[
    { key: 'library', label: '知识库', children: overview.libraries.find(l => l.id === row.library_id)?.name || row.library_id },
    { key: 'kind', label: '类型', children: kinds[material.kind] },
    { key: 'agent', label: '提交者', children: overview.keys.find(k => k.id === row.submitted_by.id)?.agent_name || row.submitted_by.id },
    { key: 'time', label: '提交时间', children: date(row.created_at) },
  ]} /><div className="material-content">{material.content.text}</div>
  <Collapse ghost items={[{ key: 'source', label: '来源与场景', children: <JsonView value={{ submission_id: row.submission_id, source: material.source, context: material.context }} /> }]} /><LoadError error={resource.error || error} retry={resource.refresh} />
  {row.result ? <div className="result-section"><Space><CheckOutlined /><Typography.Text strong>处理结果 · {outcomes[row.result.outcome]}</Typography.Text></Space><Typography.Paragraph className="pre-wrap block-gap">{row.result.summary}</Typography.Paragraph>{row.result.references.length > 0 && <Typography.Paragraph>引用：{row.result.references.join('、')}</Typography.Paragraph>}{row.result.version && <Typography.Text>版本：{row.result.version}</Typography.Text>}</div> : <>
    {active && <Alert className="block-gap" type={own ? 'info' : 'warning'} title={`${own ? '已在当前会话领取' : '已被领取，当前会话没有处理凭证'} · ${date((row.lease_until || 0) * 1000)} 到期`} description={own ? '刷新整个页面或退出后，需等待租约到期再领取。' : undefined} />}
    {managed && <Alert type="info" title="由更新任务处理" action={<Button href="#updates">查看更新任务</Button>} />}
    {row.status === 'processing' && !active && !managed && <Alert className="block-gap" type="warning" title="租约已过期，可重新领取。" />}
    {own && <div className="result-section"><AsyncForm<{ outcome: string; summary: string; references: string; version: string }> initial={{ outcome: 'accepted' }} submitText="保存处理结果" submit={async values => {
      await api.request(`/inbox/${id}/complete`, 'POST', { claim_token: claims.get(id)?.token, outcome: values.outcome, summary: values.summary.trim(), references: (values.references || '').split(/\r?\n/).map(s => s.trim()).filter(Boolean), version: values.version || null }); claims.delete(id); refresh(); message.success('处理结果已保存');
    }} footer={<Button onClick={() => action('release')} loading={busy}>放回待处理</Button>}>
      <Form.Item name="outcome" label="处理结论" rules={required}><Select options={Object.entries(outcomes).map(([value, label]) => ({ value, label }))} /></Form.Item><Form.Item name="summary" label="处理说明" rules={required}><Input.TextArea rows={4} maxLength={4000} /></Form.Item><Form.Item name="references" label="引用路径（可选，每行一项）"><Input.TextArea rows={2} /></Form.Item><Form.Item name="version" label="知识版本（可选）"><Input maxLength={200} /></Form.Item><Typography.Text type="secondary">保存结论不会修改或发布知识文件。</Typography.Text>
    </AsyncForm></div>}
    {claimable && <div className="form-actions"><Button type="primary" loading={busy} onClick={() => action('claim')}>{row.status === 'processing' ? '重新领取' : '领取处理'}</Button></div>}
  </>}
  </>;
}
