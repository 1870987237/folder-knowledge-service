import { useEffect, useRef, useState, type ReactNode } from 'react';
import { Alert, App, Breadcrumb, Button, Empty, Form, Input, Modal, Segmented, Space, Spin, Table, Tag, Typography } from 'antd';
import type { FormInstance } from 'antd';
import { ArrowUpOutlined, FileTextOutlined, FolderOutlined, ReloadOutlined } from '@ant-design/icons';
import { errorMessage, query } from './api';
import { useSession } from './context';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { AgentKey, DirectoryPage, Entry, FilePage, Library } from './types';

export const date = (value?: string | number | null) => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—';
export const required = [{ required: true, whitespace: true, message: '请填写此项' }];
export const idRules = [...required, { pattern: /^[a-z0-9][a-z0-9_-]{0,63}$/, message: '小写字母或数字开头，可使用小写字母、数字、-、_' }];
export function keyState(key: AgentKey): [string, string] {
  return key.revoked_at ? ['已撤销', 'default'] : key.expires_at && Date.parse(key.expires_at) <= Date.now() ? ['已过期', 'warning'] : ['有效', 'success'];
}
export function PageHeader({ title, action }: { title: string; action?: ReactNode }) {
  return <div className="page-header"><Typography.Title level={4}>{title}</Typography.Title><Space wrap>{action}</Space></div>;
}
export function LoadError({ error, retry }: { error?: string; retry?: () => void }) {
  return error ? <Alert type="error" showIcon title={error} action={retry && <Button size="small" onClick={retry}>重试</Button>} className="load-error" /> : null;
}
export function Surface({ children, className = '' }: { children: ReactNode; className?: string }) { return <section className={'surface ' + className}>{children}</section>; }
export function NameCell({ name, id }: { name: ReactNode; id?: string }) { return <div className="name-cell"><div>{name}</div>{id && <Typography.Text type="secondary" className="code-text">{id}</Typography.Text>}</div>; }
export function Availability({ library }: { library: Library }) {
  return <Tag color={!library.enabled ? 'default' : library.availability.status === 'available' ? 'success' : 'error'}>{!library.enabled ? '已停用' : library.availability.message}</Tag>;
}
export function JsonView({ value }: { value: unknown }) { return <pre className="code-block">{JSON.stringify(value, null, 2)}</pre>; }
export function AsyncForm<T extends object>({ form, initial, children, submit, submitText = '保存', footer, onDone, disabled }: {
  form?: FormInstance<T>; initial?: Partial<T>; children: ReactNode; submit: (values: T) => Promise<unknown>;
  submitText?: string; footer?: ReactNode; onDone?: () => void; disabled?: boolean;
}) {
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const { message } = App.useApp();
  return <Form form={form} layout="vertical" initialValues={initial} requiredMark={false} disabled={busy} onFinish={async values => {
    setBusy(true); setError('');
    try { await submit(values); onDone?.(); } catch (e) { if (!(e instanceof DOMException && e.name === 'AbortError')) { setError(errorMessage(e)); if (e instanceof Error && e.message.includes('会话')) message.error(e.message); } }
    finally { setBusy(false); }
  }}>{children}<LoadError error={error} /><div className="form-actions">{footer}<Button htmlType="submit" type="primary" loading={busy} disabled={disabled}>{submitText}</Button></div></Form>;
}
export function DirectoryPicker({ open, initial = '.', onCancel, onSelect }: { open: boolean; initial?: string; onCancel: () => void; onSelect: (path: string) => void }) {
  const { api } = useSession();
  const [path, setPath] = useState('.'), [selected, setSelected] = useState<string>();
  const [entries, setEntries] = useState<Entry[]>([]), [next, setNext] = useState<number | null>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [search, setSearch] = useState('');
  const generation = useRef(0);
  async function load(target: string, offset = 0) {
    const request = ++generation.current; setBusy(true); setError('');
    if (!offset) { setSelected(undefined); setEntries([]); setNext(null); setSearch(''); }
    try { const data = await api.request<DirectoryPage>('/directories?' + query({ path: target, offset }));
      if (request !== generation.current) return;
      setPath(target); setSelected(target); setEntries(previous => offset ? [...previous, ...data.entries] : data.entries); setNext(data.next_offset);
    } catch (e) { if (request === generation.current) setError(errorMessage(e)); }
    finally { if (request === generation.current) setBusy(false); }
  }
  useEffect(() => { if (open) void load(initial || '.'); return () => { generation.current++; }; }, [open]);
  const parts = path === '.' ? [] : path.split('/');
  return <Modal title="选择文件夹" open={open} width={740} onCancel={onCancel} destroyOnHidden footer={<Space><Button onClick={onCancel}>取消</Button><Button type="primary" disabled={!selected || busy} onClick={() => selected && onSelect(selected)}>使用此文件夹</Button></Space>}>
    <div className="folder-toolbar"><Button icon={<ArrowUpOutlined />} disabled={busy || path === '.'} onClick={() => load(parts.slice(0, -1).join('/') || '.')}>上一级</Button><Breadcrumb items={[{ title: <Button type="link" size="small" onClick={() => load('.')}>挂载根目录</Button> }, ...parts.map((part, i) => ({ title: <Button type="link" size="small" onClick={() => load(parts.slice(0, i + 1).join('/'))}>{part}</Button> }))]} /></div>
    <Input.Search aria-label="筛选文件夹" placeholder="筛选已加载的文件夹" value={search} onChange={e => setSearch(e.target.value)} allowClear className="block-gap" />
    <LoadError error={error} retry={() => load(path)} />
    <Table<Entry> rowKey="path" size="small" loading={busy} pagination={false} scroll={{ y: 280 }} dataSource={entries.filter(e => e.path.toLowerCase().includes(search.toLowerCase()))} rowSelection={{ type: 'radio', selectedRowKeys: selected ? [selected] : [], onChange: keys => setSelected(String(keys[0])) }} onRow={row => ({ onClick: () => setSelected(row.path), onDoubleClick: () => !busy && load(row.path) })} columns={[{ title: '文件夹', render: (_, row) => <Space><FolderOutlined /><span>{row.path.split('/').pop()}</span></Space> }, { title: '', width: 80, render: (_, row) => <Button type="link" size="small" onClick={e => { e.stopPropagation(); void load(row.path); }}>打开</Button> }]} />
    {next !== null && <Button onClick={() => load(path, next)} loading={busy} className="block-gap">加载更多</Button>}
    <div className="selection-bar"><Button icon={<FolderOutlined />} onClick={() => setSelected(path)} disabled={busy}>选择当前目录</Button><Typography.Text>已选：<code>{selected || '未选择'}</code></Typography.Text></div>
  </Modal>;
}
export function PathInput({ id, value, onChange, disabled, placeholder }: { id?: string; value?: string; onChange?: (v: string) => void; disabled?: boolean; placeholder?: string }) {
  const [picker, setPicker] = useState(false);
  return <><Space.Compact block><Input id={id} value={value} onChange={e => onChange?.(e.target.value)} disabled={disabled} placeholder={placeholder || '挂载根目录下的相对路径'} /><Button icon={<FolderOutlined />} disabled={disabled} onClick={() => setPicker(true)}>选择目录</Button></Space.Compact><DirectoryPicker open={picker} initial={value} onCancel={() => setPicker(false)} onSelect={p => { onChange?.(p); setPicker(false); }} /></>;
}
export function FileBrowser({ library }: { library: Library }) {
  const { api } = useSession();
  const [path, setPath] = useState('.'), [directory, setDirectory] = useState<DirectoryPage>();
  const [file, setFile] = useState<FilePage>(), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [mode, setMode] = useState<'preview' | 'source'>('source'), [content, setContent] = useState('');
  const current = useRef(0);
  const isMarkdown = /\.md$/i.test(path);

  async function fetchFullContent(target: string, requestId: number): Promise<string> {
    const lines: string[] = [];
    let cursor: string | undefined;
    do {
      const response = await api.request<FilePage>(`/browse/${library.id}?` + query({ path: target, read: 'true', offset: 0, cursor }));
      if (requestId !== current.current) return '';
      for (const l of response.lines) lines.push(l.text + (l.line_complete === false ? '' : '\n'));
      cursor = response.next_cursor || undefined;
    } while (cursor);
    return lines.join('').replace(/\n$/, '');
  }

  async function load(target: string, read = false, offset = 0, cursor?: string) {
    const request = ++current.current; setBusy(true); setError('');
    try {
      const response = await api.request<DirectoryPage | FilePage>(`/browse/${library.id}?` + query({ path: target, read: String(read), offset, cursor }));
      if (request !== current.current) return;
      if (read) {
        setFile(response as FilePage); setDirectory(undefined); setPath(target);
        if (/\.md$/i.test(target)) { setMode('preview'); void fetchFullContent(target, request).then(c => { if (request === current.current) setContent(c); }); }
        else { setContent(''); }
      }
      else { setDirectory(response as DirectoryPage); setFile(undefined); setPath(target); setContent(''); }
    } catch (e) { if (request === current.current) setError(errorMessage(e)); }
    finally { if (request === current.current) setBusy(false); }
  }
  useEffect(() => { void load('.'); return () => { current.current++; }; }, [library.id, library.path]);
  const parts = path === '.' ? [] : path.split('/');
  return <div>
    <div className="folder-toolbar">
      <Button icon={<ArrowUpOutlined />} disabled={path === '.' || busy} onClick={() => load(parts.slice(0, -1).join('/') || '.')}>上一级</Button>
      <Typography.Text code>{path}</Typography.Text>
      {isMarkdown && file && <Segmented size="small" value={mode} options={[{ value: 'preview', label: '预览' }, { value: 'source', label: '源码' }]} onChange={v => setMode(v as 'preview' | 'source')} />}
      <Button aria-label="刷新目录" icon={<ReloadOutlined />} onClick={() => load(path, !!file)} />
    </div>
    <LoadError error={error} retry={() => load(path, !!file)} />
    <Spin spinning={busy}>
      {file ? (
        isMarkdown && mode === 'preview' && content
          ? <article className="markdown-document"><Markdown remarkPlugins={[remarkGfm]} skipHtml components={{
              img: ({ alt }) => <span>[图片：{alt || '附件暂不支持'}]</span>,
              a: ({ href, children }) => /^https?:\/\//i.test(href || '') ? <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span>,
            }}>{content}</Markdown></article>
          : <>
              <pre className="file-preview">{file.lines.map(l => `${l.line}${l.column && l.column > 1 ? ':' + l.column : ''}  ${l.text}${l.line_complete === false ? ' [本行待续]' : ''}`).join('\n')}</pre>
              {file.next_cursor && <Button className="block-gap" disabled={busy} onClick={() => load(path, true, 0, file.next_cursor || undefined)}>下一页</Button>}
              <Typography.Text type="secondary">文件只读</Typography.Text>
            </>
      ) : (
        <Table<Entry> pagination={false} rowKey="path" dataSource={directory?.entries} locale={{ emptyText: <Empty description="目录为空" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }} columns={[
          { title: '名称', render: (_, row) => <Button type="link" icon={row.type === 'directory' ? <FolderOutlined /> : <FileTextOutlined />} onClick={() => load(row.path, row.type === 'file')}>{row.path.split('/').pop()}</Button> },
          { title: '类型', width: 85, render: (_, row) => row.type === 'directory' ? '文件夹' : '文件' }
        ]} />
      )}
    </Spin>
    {directory?.partial && <Alert type="warning" title="本次目录列表不完整，请缩小范围后重试。" />}
    {directory?.next_offset != null && <Button className="block-gap" disabled={busy} onClick={() => load(path, false, directory.next_offset || 0)}>下一页</Button>}
  </div>;
}
