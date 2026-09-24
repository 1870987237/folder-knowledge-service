import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, App as AntApp, Button, Form, Input, Layout, Menu, Spin, Typography } from 'antd';
import { ApiOutlined, AppstoreOutlined, DashboardOutlined, DatabaseOutlined, FolderOutlined, HistoryOutlined, InboxOutlined, KeyOutlined, LogoutOutlined, MenuOutlined, BranchesOutlined } from '@ant-design/icons';
import { Api, errorMessage } from './api';
import { SessionContext, type Claim } from './context';
import type { Overview } from './types';
import { LibrariesPage } from './pages/Libraries';
import { InboxPage } from './pages/Inbox';
import { TemplatesPage } from './pages/Templates';
import { KeysPage } from './pages/Keys';
import { RepositoriesPage } from './pages/Repositories';
import { ActivityPage } from './pages/Activity';
import { OverviewPage } from './pages/Overview';

const knownPages = ['overview', 'libraries', 'inbox', 'templates', 'repositories', 'keys', 'activity'];
function initialPage() { const key = location.hash.slice(1); return knownPages.includes(key) ? key : 'overview'; }
export function AdminApp() {
  const [session, setSession] = useState<{ api: Api; overview: Overview }>();
  const [loginError, setLoginError] = useState(''), [busy, setBusy] = useState(false);
  const [form] = Form.useForm<{ token: string }>();
  const liveClient = useRef<Api | undefined>(undefined);
  const logout = useCallback(() => { liveClient.current?.dispose(); liveClient.current = undefined; setSession(undefined); form.resetFields(); }, [form]);
  useEffect(() => () => { liveClient.current?.dispose(); }, []);
  if (!session) return <div className="login-screen"><div className="login-panel"><div className="brand-mark"><FolderOutlined /></div><Typography.Title level={2}>Folder KB</Typography.Title><Typography.Paragraph type="secondary">知识库管理平台</Typography.Paragraph><Form form={form} layout="vertical" requiredMark={false} onFinish={async values => {
    setBusy(true); setLoginError('');
    const client = new Api(values.token.trim(), () => { logout(); setLoginError('会话已失效，请重新登录。'); });
    liveClient.current = client;
    try { const overview = await client.request<Overview>('/overview'); setSession({ api: client, overview }); form.resetFields(); }
    catch (e) { client.dispose(); setLoginError(errorMessage(e)); }
    finally { setBusy(false); }
  }}><Form.Item name="token" label="管理员令牌" rules={[{ required: true, whitespace: true, message: '请输入管理员令牌' }]}><Input.Password autoComplete="off" placeholder="KB_ADMIN_TOKEN" size="large" /></Form.Item>{loginError && <Alert type="error" title={loginError} className="block-gap" />}<Button type="primary" htmlType="submit" block size="large" loading={busy}>进入管理平台</Button></Form><Typography.Paragraph type="secondary" className="login-note">令牌仅保存在当前页面，刷新后需要重新登录。</Typography.Paragraph></div></div>;
  return <Workspace key="workspace" client={session.api} initial={session.overview} logout={logout} />;
}
function Workspace({ client, initial, logout }: { client: Api; initial: Overview; logout: () => void }) {
  const [page, setPage] = useState(initialPage), [overview, setOverview] = useState(initial), [revision, setRevision] = useState(0), [error, setError] = useState('');
  const [collapsed, setCollapsed] = useState(false), [mobile, setMobile] = useState(false);
  const claims = useRef(new Map<string, Claim>());
  const refresh = useCallback(() => setRevision(r => r + 1), []);
  function navigate(key: string) { location.hash = key; setPage(key); if (mobile) setCollapsed(true); }
  useEffect(() => { const fn = () => setPage(initialPage()); window.addEventListener('hashchange', fn); return () => window.removeEventListener('hashchange', fn); }, []);
  useEffect(() => {
    let alive = true, loading = false;
    async function load() { if (loading) return; loading = true; try { const next = await client.request<Overview>('/overview'); if (alive) { setOverview(next); setError(''); } } catch (e) { if (alive && !(e instanceof DOMException && e.name === 'AbortError')) setError(errorMessage(e)); } finally { loading = false; } }
    void load(); const timer = window.setInterval(() => { if (!document.hidden) void load(); }, 10000);
    return () => { alive = false; window.clearInterval(timer); };
  }, [client, revision]);
  useEffect(() => () => { claims.current.clear(); }, []);
  const content = ({ overview: <OverviewPage navigate={navigate} />, libraries: <LibrariesPage />, inbox: <InboxPage />, templates: <TemplatesPage />, repositories: <RepositoriesPage />, keys: <KeysPage />, activity: <ActivityPage /> } as Record<string, React.ReactNode>)[page];
  return <SessionContext.Provider value={{ api: client, overview, refresh, revision, claims: claims.current }}><Layout className="app-layout"><Layout.Sider theme="light" width={232} breakpoint="lg" collapsedWidth={0} collapsed={collapsed} trigger={null} onBreakpoint={broken => { setMobile(broken); setCollapsed(broken); }} className="app-sidebar">
    <div className="brand"><span className="brand-mark"><FolderOutlined /></span><Typography.Text strong>Folder KB</Typography.Text></div>
    <Menu mode="inline" selectedKeys={[page]} onClick={({ key }) => navigate(key)} items={[
      { key: 'overview', icon: <DashboardOutlined />, label: '服务概览' },
      { type: 'group', label: '知识工作区', children: [{ key: 'libraries', icon: <DatabaseOutlined />, label: '知识库' }, { key: 'inbox', icon: <InboxOutlined />, label: '增量收件箱' }, { key: 'templates', icon: <AppstoreOutlined />, label: '模板' }] },
      { type: 'group', label: '服务管理', children: [{ key: 'repositories', icon: <BranchesOutlined />, label: 'Git 仓库' }, { key: 'keys', icon: <KeyOutlined />, label: 'Agent 与密钥' }, { key: 'activity', icon: <HistoryOutlined />, label: '访问活动' }] },
    ]} /><div className="sidebar-footer"><Button type="text" icon={<LogoutOutlined />} onClick={logout}>退出登录</Button></div>
  </Layout.Sider><Layout.Content className="app-content">{mobile && <Button className="mobile-menu" icon={<MenuOutlined />} aria-label="展开导航" onClick={() => setCollapsed(!collapsed)} />}{error && <Alert className="block-gap" type="error" showIcon title={error} action={<Button onClick={refresh}>重试</Button>} />}<div className="page-container" key={page}>{content || <Spin />}</div></Layout.Content></Layout></SessionContext.Provider>;
}
