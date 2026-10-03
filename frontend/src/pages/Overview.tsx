import { Button, Col, Row, Statistic, Typography } from 'antd';
import { ArrowRightOutlined } from '@ant-design/icons';
import { useResource, useSession } from '../context';
import { keyState, LoadError, PageHeader, Surface } from '../components';
import { EventTable } from './Activity';
import type { Activity } from '../types';

export function OverviewPage({ navigate }: { navigate: (key: string) => void }) {
  const { overview } = useSession();
  const activity = useResource<{ events: Activity[] }>('/activity?limit=8', 10000);
  return <><PageHeader title="服务概览" /><Row gutter={[16, 16]} className="block-gap"><Col xs={12} lg={6}><Surface className="stat-panel"><Statistic title="已启用知识库" value={overview.libraries.filter(l => l.enabled).length} /></Surface></Col><Col xs={12} lg={6}><Surface className="stat-panel"><Statistic title="有效 Agent Key" value={overview.keys.filter(k => keyState(k)[0] === '有效').length} /></Surface></Col><Col xs={12} lg={6}><Surface className="stat-panel"><Statistic title="知识工具调用" value={overview.metrics.calls} /></Surface></Col><Col xs={12} lg={6}><Surface className="stat-panel"><Statistic title="失败调用" value={overview.metrics.errors} /></Surface></Col></Row>
  <Row gutter={[20, 20]} className="overview-links"><Col xs={24} lg={12}><Surface className="padded"><Typography.Title level={5}>知识工作区</Typography.Title>{[['libraries', '知识库', '目录、文件与插件绑定'], ['inbox', '增量收件箱', 'Agent 提交的材料与处理结果'], ['plugins', '插件', '能力、程序包与固定版本']].map(([key, label, desc]) => <div className="connection-row" key={key}><div><Typography.Text strong>{label}</Typography.Text><div><Typography.Text type="secondary">{desc}</Typography.Text></div></div><Button type="text" aria-label={'打开' + label} icon={<ArrowRightOutlined />} onClick={() => navigate(key)} /></div>)}</Surface></Col><Col xs={24} lg={12}><Surface className="padded"><Typography.Title level={5}>服务接入</Typography.Title><div className="endpoint"><Typography.Text type="secondary">MCP · Streamable HTTP</Typography.Text><Typography.Paragraph copyable code>{location.origin}/mcp</Typography.Paragraph></div><div className="endpoint"><Typography.Text type="secondary">REST · 知识库列表</Typography.Text><Typography.Paragraph copyable code>{location.origin}/api/v1/libraries</Typography.Paragraph></div><Button onClick={() => navigate('keys')}>管理连接密钥</Button></Surface></Col></Row>
  <Surface><div className="section-heading"><Typography.Title level={5}>最近活动</Typography.Title><Button type="link" onClick={() => navigate('activity')}>查看全部</Button></div><LoadError error={activity.error} retry={activity.refresh} /><EventTable events={activity.data?.events || []} loading={activity.loading} /></Surface>
  </>;
}
