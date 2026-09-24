import { useState } from 'react';
import { Button, DatePicker, Select, Space, Table, Tag, Typography } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { useResource, useSession } from '../context';
import { query } from '../api';
import { date, LoadError, NameCell, PageHeader, Surface } from '../components';
import type { Activity } from '../types';

export function EventTable({ events, loading = false }: { events: Activity[]; loading?: boolean }) {
  const { overview } = useSession();
  return <Table<Activity> rowKey={(r, i) => String(r.id ?? r.timestamp + '/' + i)} loading={loading} dataSource={events} pagination={false} scroll={{ x: 800 }} columns={[
    { title: '时间', render: (_, r) => date(r.timestamp) },
    { title: '操作方', render: (_, r) => <NameCell name={r.kind === 'admin' ? '管理员' : r.agent_name || overview.keys.find(k => k.id === r.key_id)?.agent_name || '—'} id={r.transport || '管理操作'} /> },
    { title: '操作', render: (_, r) => <Typography.Text className="code-text">{r.tool || r.action}</Typography.Text> },
    { title: '知识库 / 路径', render: (_, r) => <NameCell name={overview.libraries.find(l => l.id === r.library_id)?.name || r.library_id || '—'} id={r.path} /> },
    { title: '结果', render: (_, r) => <div className="name-cell"><span><Tag color={r.status === 'ok' ? 'success' : r.status === 'partial' ? 'warning' : 'error'}>{r.status === 'ok' ? '成功' : r.status === 'partial' ? '不完整' : '失败'}</Tag></span>{r.error && <Typography.Text type="secondary">{r.reason || r.error}</Typography.Text>}</div> },
    { title: '耗时', render: (_, r) => r.duration_ms == null ? '—' : `${r.duration_ms} ms` },
  ]} />;
}
export function ActivityPage() {
  const { overview } = useSession();
  const [filters, setFilters] = useState<Record<string, string>>({}), [before, setBefore] = useState<number>();
  const resource = useResource<{ events: Activity[]; next_before_id: number | null }>('/activity?' + query({ ...filters, before_id: before, limit: 100 }), 10000);
  function change(key: string, value?: string) { setFilters(f => ({ ...f, [key]: value || '' })); setBefore(undefined); }
  return <><PageHeader title="访问活动" action={<Button icon={<ReloadOutlined />} onClick={resource.refresh}>刷新</Button>} /><div className="toolbar">
    <Select aria-label="筛选操作方" placeholder="全部操作方" allowClear options={overview.keys.map(k => ({ label: k.agent_name, value: k.id }))} onChange={v => change('key_id', v)} />
    <Select aria-label="筛选知识库" placeholder="全部知识库" allowClear options={overview.libraries.map(l => ({ label: l.name, value: l.id }))} onChange={v => change('library_id', v)} />
    <Select aria-label="筛选结果" placeholder="全部结果" allowClear options={[{ value: 'ok', label: '成功' }, { value: 'error', label: '失败' }, { value: 'partial', label: '不完整' }]} onChange={v => change('status', v)} />
    <DatePicker.RangePicker placeholder={['开始时间', '结束时间']} showTime onChange={range => { setFilters(f => ({ ...f, start_time: range?.[0]?.toISOString() || '', end_time: range?.[1]?.toISOString() || '' })); setBefore(undefined); }} />
  </div><LoadError error={resource.error} retry={resource.refresh} />
  {overview.active_requests.length > 0 && <Surface className="padded block-gap"><Typography.Title level={5}>正在处理</Typography.Title>{overview.active_requests.map((r, i) => <div className="connection-row" key={r.request_id || i}><Space><Tag color="processing">处理中</Tag>{r.agent_name}<code>{r.tool}</code></Space><span>{overview.libraries.find(l => l.id === r.library_id)?.name || r.library_id}</span></div>)}</Surface>}
  <Surface><EventTable events={resource.data?.events || []} loading={resource.loading} /></Surface><div className="pager"><Button disabled={!before} onClick={() => setBefore(undefined)}>返回最新</Button><Button disabled={resource.data?.next_before_id == null} onClick={() => setBefore(resource.data!.next_before_id!)}>更早记录</Button></div></>;
}
