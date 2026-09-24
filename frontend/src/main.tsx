import { Component, type CSSProperties, type ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import { App, Button, ConfigProvider, Result, theme } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import dayjs from 'dayjs';
import 'dayjs/locale/zh-cn';
import { AdminApp } from './App';
import { adminTheme } from './theme';
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
  const style = { '--app-bg': token.colorBgLayout, '--app-surface': token.colorBgContainer, '--app-border': token.colorBorderSecondary, '--app-primary': token.colorPrimary, '--app-selected': token.colorPrimaryBg, '--app-muted': token.colorTextSecondary, '--app-radius': `${token.borderRadius}px`, '--app-gap': `${token.marginLG}px` } as CSSProperties;
  return <div className="theme-root" style={style}><ErrorBoundary><AdminApp /></ErrorBoundary></div>;
}
createRoot(document.getElementById('root')!).render(<ConfigProvider locale={zhCN} theme={adminTheme} csp={nonce && nonce !== '__CSP_NONCE__' ? { nonce } : undefined}><App><ThemeBridge /></App></ConfigProvider>);
