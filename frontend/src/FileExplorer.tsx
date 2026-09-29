import { useState } from 'react';
import { Button, Empty, Table } from 'antd';
import { FileTextOutlined, FolderOutlined } from '@ant-design/icons';
import { date } from './components';

export type ExplorerItem = { id: string; name: string; folder: boolean; modified?: string; bytes?: number; kind?: string };
export function FileExplorer({ items, loading, onOpen }: { items: ExplorerItem[]; loading: boolean; onOpen: (id: string) => void }) {
  const [selected, setSelected] = useState('');
  const rows = [...items].sort((a, b) => Number(b.folder) - Number(a.folder) || a.name.localeCompare(b.name, 'zh-CN', { numeric: true }));
  return <div className="file-explorer surface">
    <Table<ExplorerItem> rowKey="id" size="small" loading={loading} dataSource={rows} pagination={false} scroll={{ x: 650 }} locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="此处暂无内容" /> }}
      rowClassName={r => selected === r.id ? 'explorer-selected' : ''}
      onRow={r => ({ tabIndex: 0, 'aria-label': r.name, 'aria-selected': selected === r.id,
        onClick: () => setSelected(r.id), onDoubleClick: () => !loading && onOpen(r.id),
        onKeyDown: e => { if (e.key === 'Enter' && !loading) onOpen(r.id); if (e.key === ' ') { e.preventDefault(); setSelected(r.id); } },
      })}
      columns={[
        { title: '名称', key: 'name', render: (_, r) => <span className="explorer-name">{r.folder ? <FolderOutlined /> : <FileTextOutlined />}<span>{r.name}</span>{selected === r.id && <Button size="small" type="text" disabled={loading} onDoubleClick={e => e.stopPropagation()} onClick={e => { e.stopPropagation(); onOpen(r.id); }}>打开</Button>}</span> },
        { title: '修改日期', width: 190, render: (_, r) => r.modified ? date(r.modified) : '—' },
        { title: '类型', width: 125, render: (_, r) => r.kind || (r.folder ? '文件夹' : /\.md$/i.test(r.name) ? 'Markdown' : '文本文件') },
        { title: '大小', width: 95, render: (_, r) => r.bytes == null ? '—' : r.bytes < 1024 ? `${r.bytes} B` : `${(r.bytes / 1024).toFixed(1)} KB` },
      ]} />
  </div>;
}
