import { Component, type ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import { App, Button, ConfigProvider, Result, theme } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import dayjs from 'dayjs';
import 'dayjs/locale/zh-cn';
import { AdminApp } from './App';
import { PortalApp } from './Portal';
import { adminTheme, themeVariables } from './theme';
import './styles.css';

dayjs.locale('zh-cn');
const nonce = document.querySelector<HTMLMetaElement>('meta[name="csp-nonce"]')?.content;
class ErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() { return this.state.failed ? <Result status="error" title="页面加载失败" subTitle="请刷新页面后重试。" extra={<Button onClick={() => location.reload()}>刷新页面</Button>} /> : this.props.children; }
}
function ThemeBridge() {
  const { token } = theme.useToken();
  const style = themeVariables(token);
  return <div className="theme-root" style={style}><ErrorBoundary>{location.pathname.startsWith('/workspace') ? <PortalApp /> : <AdminApp />}</ErrorBoundary></div>;
}
createRoot(document.getElementById('root')!).render(<ConfigProvider locale={zhCN} theme={adminTheme} csp={nonce && nonce !== '__CSP_NONCE__' ? { nonce } : undefined}><App><ThemeBridge /></App></ConfigProvider>);
