import { useState } from 'react';
import { App, Button, Space, Table, Tag, Typography } from 'antd';
import { useResource, useSession } from '../context';
import { date, LoadError, PageHeader, Surface } from '../components';
import { errorMessage } from '../api';

interface Failure { id: string; resource_id: string; resource_name: string; error: string; failed_at: string }
export interface Worker { id: 'git' | 'updates'; name: string; status: 'online' | 'offline'; last_seen: string | null; queued: number; running: number; awaiting_review: number; failed: number; recent_failures: Failure[] }
export function retryRequest(worker: Worker['id'], failure: Failure): [string, unknown] {
  return worker === 'git' ? [`/repositories/${encodeURIComponent(failure.resource_id)}/sync`, undefined]
    : [`/update-jobs/${encodeURIComponent(failure.id)}/retry`, undefined];
}

export function WorkersPage() {
  const { api, refresh } = useSession(), { message } = App.useApp();
  const resource = useResource<{ workers: Worker[] }>('/workers', 5000);
  const [retrying, setRetrying] = useState<string>();
  return <><PageHeader title="后台进程" action={<Button onClick={resource.refresh}>刷新</Button>} />
    <LoadError error={resource.error} retry={resource.refresh} />
    <Surface><Table<Worker> rowKey="id" loading={resource.loading} dataSource={resource.data?.workers} pagination={false} scroll={{ x: 760 }} columns={[
      { title: '组件', dataIndex: 'name' },
      { title: '心跳', render: (_, w) => <Tag color={w.status === 'online' ? 'success' : 'error'}>{w.status === 'online' ? '在线' : '离线或心跳超时'}</Tag> },
      { title: '最近心跳', render: (_, w) => w.last_seen ? date(w.last_seen) : '尚未收到' },
      { title: '等待', dataIndex: 'queued' }, { title: '执行中', dataIndex: 'running' },
      { title: '待审核', dataIndex: 'awaiting_review' }, { title: '历史失败', dataIndex: 'failed' },
      { title: '任务', render: (_, w) => <Button type="link" href={w.id === 'git' ? '#repositories' : '#updates'}>查看任务</Button> },
    ]} /></Surface>
    <Typography.Paragraph type="secondary" className="block-gap">心跳表示进程存活，任务进度以队列状态为准。离线时请检查统一启动器及进程日志。</Typography.Paragraph>
    {(resource.data?.workers || []).map(worker => <Surface key={worker.id} className="block-gap">
      <div className="section-heading"><Typography.Title level={5}>{worker.name} · 最近失败</Typography.Title></div>
      <Table<Failure> rowKey="id" dataSource={worker.recent_failures} pagination={false} locale={{ emptyText: '暂无失败记录' }} columns={[
        { title: '资源', dataIndex: 'resource_name' }, { title: '时间', render: (_, f) => date(f.failed_at) },
        { title: '原因', render: (_, f) => <Typography.Text type="danger">{f.error || '未知错误'}</Typography.Text> },
        { title: '操作', render: (_, f) => <Space><Button disabled={worker.status !== 'online' || !!retrying} loading={retrying === f.id} onClick={async () => {
          setRetrying(f.id);
          try { const [path, body] = retryRequest(worker.id, f); await api.request(path, 'POST', body); refresh(); resource.refresh(); message.success('已加入任务队列'); }
          catch (e) { message.error(errorMessage(e)); } finally { setRetrying(undefined); }
        }}>{worker.id === 'git' ? '重新同步' : '重试'}</Button></Space> },
      ]} />
    </Surface>)}
  </>;
}
