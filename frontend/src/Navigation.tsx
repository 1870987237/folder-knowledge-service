import type { ComponentProps } from 'react';
import { Layout, Menu } from 'antd';

// Share the scroll boundary through public semantic slots, not Ant internals.
export function NavigationSider(props: ComponentProps<typeof Layout.Sider>) {
  return <Layout.Sider {...props} trigger={null} classNames={{ body: 'sidebar-body' }} />;
}

export function NavigationMenu(props: ComponentProps<typeof Menu>) {
  return <Menu {...props} mode="inline" className="sidebar-menu" styles={{ root: { borderInlineEnd: 0 } }} />;
}
