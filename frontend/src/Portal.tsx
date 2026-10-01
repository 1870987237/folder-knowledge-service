import { useEffect, useRef, useState } from 'react';
import { Alert, App, Breadcrumb, Button, Card, Dropdown, Form, Input, Layout, Menu, Modal, Popover, Select, Space, Table, Tabs, Tag, Tooltip, Typography, theme } from 'antd';
import { ArrowLeftOutlined, DownOutlined, FolderOutlined, KeyOutlined, LogoutOutlined, MenuFoldOutlined, MenuUnfoldOutlined, ReloadOutlined, SearchOutlined, TeamOutlined } from '@ant-design/icons';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { KeyPolicy, LibraryAgentSettings } from './KeyPolicy';
import { Api, errorMessage, query } from './api';
import { PageHeader, date } from './components';
import { Organizations, type Identity } from './pages/People';
import { themeVariables } from './theme';
import { FileExplorer } from './FileExplorer';
import { MarkdownEditor } from './MarkdownEditor';
import type { DirectoryPage } from './types';

type Library = { id: string; name: string; role: number; editable: boolean; updated_at?: string; organization_id?: string; owner_id?: string };
type Doc = { path: string; content: string; version: string };
type Draft = { id: string; path: string; author_id: string; author: string; status: string; revision: number; updated_at: string; content: string; original: string; diff: string };
const statuses: Record<string, string> = { draft: '草稿', submitted: '待审核', published: '已发布', rejected: '已退回' };
const roles = ['', '读者', '编辑者', '发布者', '管理者'];
const emptyDigest = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855';

export function DocumentPreview({ content }: { content: string }) {
  const { token } = theme.useToken();
  return <article className="markdown-document" style={themeVariables(token)}><Markdown remarkPlugins={[remarkGfm]} skipHtml components={{
    img: ({ alt }) => <span>[图片：{alt || '附件暂不支持'}]</span>,
    a: ({ href, children }) => /^https?:\/\//i.test(href || '') ? <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span>,
  }}>{content}</Markdown></article>;
}

export function PortalApp() {
  const [client, setClient] = useState<Api>(), [identity, setIdentity] = useState<Identity>();
  const [mode, setMode] = useState('login'), [error, setError] = useState(''), [notice, setNotice] = useState(''), [busy, setBusy] = useState(false);
  const live = useRef<Api | undefined>(undefined); const [form] = Form.useForm();
  const logout = () => { live.current?.dispose(); live.current = undefined; setClient(undefined); setIdentity(undefined); form.resetFields(); };
  useEffect(() => () => live.current?.dispose(), []);
  if (!client || !identity) return <div className="login-screen"><Card className="login-panel"><Typography.Title level={2}><FolderOutlined /> Folder KB</Typography.Title><Tabs activeKey={mode} onChange={v => { setMode(v); setError(''); setNotice(''); }} items={[{ key: 'login', label: '登录' }, { key: 'register', label: '注册' }]} />
    <Form form={form} layout="vertical" onFinish={async v => { setBusy(true); setError(''); setNotice(''); const api = new Api('', () => {}, '/portal/api'); try {
      if (mode === 'register') { const r = await api.request<{ message: string }>('/register', 'POST', v); setNotice(r.message); setMode('login'); form.resetFields(['password']); }
      else { const { token } = await api.request<{ token: string }>('/login', 'POST', v); const next = new Api(token, logout, '/portal/api'); live.current = next; try { const me = await next.request<Identity>('/me'); setClient(next); setIdentity(me); form.resetFields(); } catch (e) { next.dispose(); throw e; } }
    } catch (e) { setError(errorMessage(e)); } finally { api.dispose(); setBusy(false); } }}>
      <Form.Item name="username" label="账号" rules={[{ required: true }]}><Input autoComplete="username" maxLength={80} /></Form.Item><Form.Item name="password" label="密码" rules={[{ required: true }, { min: 12, message: '至少 12 个字符' }]}><Input.Password autoComplete={mode === 'login' ? 'current-password' : 'new-password'} maxLength={256} /></Form.Item>
      {error && <Alert type="error" title={error} className="block-gap" />}{notice && <Alert type="success" title={notice} className="block-gap" />}<Button block type="primary" htmlType="submit" loading={busy}>{mode === 'login' ? '进入工作区' : '注册并等待审批'}</Button>
    </Form><Button type="link" href="/admin">平台管理</Button></Card></div>;
  return <PortalWorkspace api={client} identity={identity} setIdentity={setIdentity} logout={logout} />;
}

function PortalWorkspace({ api, identity, setIdentity, logout }: { api: Api; identity: Identity; setIdentity: (v: Identity) => void; logout: () => void }) {
  const [collapsed, setCollapsed] = useState(false);
  const [libraries, setLibraries] = useState<Library[]>([]), [page, setPage] = useState('libraries'), [library, setLibrary] = useState<Library>();
  const [error, setError] = useState(''), [busy, setBusy] = useState(false), [create, setCreate] = useState(false); const [form] = Form.useForm(); const { modal } = App.useApp(); const [dirty, setDirty] = useState(false);
  function navigate(fn: () => void) { if (dirty) modal.confirm({ title: '放弃未保存的修改？', onOk: fn }); else fn(); }
  async function load() { setBusy(true); setError(''); try { const [ls, me] = await Promise.all([api.request<Library[]>('/libraries'), api.request<Identity>('/me')]); setLibraries(ls); setIdentity(me); } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); } }
  useEffect(() => { void load(); }, [api]);
  return <Layout className="app-layout portal-layout"><Layout.Sider theme="light" width={190} breakpoint="lg" collapsedWidth={56} collapsed={collapsed} onBreakpoint={setCollapsed} className="app-sidebar"><div className="brand">{!collapsed && <Typography.Text strong>Folder KB</Typography.Text>}<Tooltip title={collapsed ? '展开侧栏' : '收起侧栏'}><Button type="text" size="small" aria-label={collapsed ? '展开侧栏' : '收起侧栏'} icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />} onClick={() => setCollapsed(v => !v)} /></Tooltip></div>
    <Menu selectedKeys={[page]} onClick={({ key }) => navigate(() => { setPage(key); setLibrary(undefined); void load(); })} items={[{ key: 'libraries', label: '知识库', icon: <FolderOutlined /> }, { key: 'organizations', label: '组织成员', icon: <TeamOutlined /> }, { key: 'keys', label: '我的 Agent Key', icon: <KeyOutlined /> }]} />
    <div className="sidebar-footer">{!collapsed && <Typography.Text ellipsis>{identity.username}</Typography.Text>}<Tooltip title="退出"><Button type="text" aria-label="退出" icon={<LogoutOutlined />} onClick={() => navigate(() => { void api.request('/logout', 'POST').then(logout).catch(e => setError(errorMessage(e))); })}>{!collapsed && '退出'}</Button></Tooltip></div>
  </Layout.Sider><Layout.Content className="app-content"><div className="page-container">{error && <Alert type="error" title={error} action={<Button onClick={load}>重试</Button>} />}
    {page === 'libraries' && (library ? <KnowledgeEditor key={library.id} api={api} identity={identity} library={library} onDirtyChange={setDirty} back={() => { setLibrary(undefined); void load(); }} /> : <><div className="workspace-topbar"><span className="workspace-location">知识库</span><div className="workspace-actions"><Tooltip title="刷新"><Button size="small" type="text" aria-label="刷新" icon={<ReloadOutlined />} onClick={load} loading={busy} /></Tooltip><Button size="small" type="primary" onClick={() => { form.resetFields(); setCreate(true); }}>创建知识库</Button></div></div><FileExplorer loading={busy} items={libraries.map(r => ({ id: r.id, name: r.name, folder: true, modified: r.updated_at, kind: r.owner_id ? '个人知识库' : '组织知识库' }))} onOpen={id => setLibrary(libraries.find(r => r.id === id))} /></>)}
    {page === 'organizations' && <><PageHeader title="组织成员" /><Card><Organizations api={api} identity={identity} reload={load} /></Card></>}
    {page === 'keys' && <UserKeys api={api} libraries={libraries} />}
    <Modal title="创建知识库" open={create} footer={null} onCancel={() => setCreate(false)}><Form form={form} layout="vertical" initialValues={{ organization_id: '' }} onFinish={async v => { setBusy(true); setError(''); try { await api.request('/libraries', 'POST', { ...v, organization_id: v.organization_id || null }); setCreate(false); await load(); } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); } }}><Form.Item name="name" label="名称" rules={[{ required: true, whitespace: true }]}><Input maxLength={80} /></Form.Item><Form.Item name="organization_id" label="归属"><Select options={[{ value: '', label: '个人私有' }, ...identity.organizations.filter(o => o.role === 'admin').map(o => ({ value: o.id, label: o.name }))]} /></Form.Item>{error && <Alert type="error" title={error} />}<Button type="primary" htmlType="submit" loading={busy}>创建</Button></Form></Modal>
  </div></Layout.Content></Layout>;
}

function KnowledgeEditor({ api, identity, library, back, onDirtyChange }: { api: Api; identity: Identity; library: Library; back: () => void; onDirtyChange: (dirty: boolean) => void }) {
  const { modal, message } = App.useApp();
  const [agentSettings, setAgentSettings] = useState(false);
  const [editorTools, setEditorTools] = useState<HTMLSpanElement | null>(null), [searchOpen, setSearchOpen] = useState(false);
  const [tab, setTab] = useState('documents'), [directory, setDirectory] = useState<DirectoryPage>(), [path, setPath] = useState('.');
  const [doc, setDoc] = useState<Doc>(), [text, setText] = useState(''), [editorRevision, setEditorRevision] = useState(0);
  const [drafts, setDrafts] = useState<Draft[]>([]), [selected, setSelected] = useState<Draft>(), [draftText, setDraftText] = useState('');
  const [error, setError] = useState(''), [busy, setBusy] = useState(false), [newFile, setNewFile] = useState(false), [filename, setFilename] = useState('');
  const [search, setSearch] = useState<{ matches: { path: string; line: number; text: string }[]; partial?: boolean; next_cursor?: string }>(), [searchTerm, setSearchTerm] = useState('');
  const base = `/libraries/${library.id}`, generation = useRef(0);
  const canEdit = library.role >= 2 && library.editable;
  const dirty = (!!doc && canEdit && tab === 'documents' && text !== doc.content) || (!!selected && selected.status === 'draft' && draftText !== selected.content);
  useEffect(() => { onDirtyChange(dirty); return () => onDirtyChange(false); }, [dirty, onDirtyChange]);
  useEffect(() => { const handler = (e: BeforeUnloadEvent) => { if (dirty) e.preventDefault(); }; window.addEventListener('beforeunload', handler); return () => window.removeEventListener('beforeunload', handler); }, [dirty]);
  function navigate(fn: () => void) { if (dirty) modal.confirm({ title: '放弃未保存的修改？', onOk: fn }); else fn(); }
  async function browse(target = '.', offset = 0) {
    const gen = ++generation.current; setBusy(true); setError('');
    try { const data = await api.request<DirectoryPage>(base + '/browse?' + query({ path: target, offset })); if (gen !== generation.current) return;
      setDirectory(previous => offset && previous ? { ...data, entries: [...previous.entries, ...data.entries] } : data);
      setPath(target); setTab('documents'); setDoc(undefined); setSearch(undefined);
    } catch (e) { if (gen === generation.current) setError(errorMessage(e)); } finally { if (gen === generation.current) setBusy(false); }
  }
  async function open(target: string) {
    const gen = ++generation.current; setBusy(true); setError('');
    try { const d = await api.request<Doc>(base + '/document?' + query({ path: target })); if (gen !== generation.current) return;
      setDoc(d); setText(d.content); setTab('documents');
    } catch (e) { if (gen === generation.current) setError(errorMessage(e)); } finally { if (gen === generation.current) setBusy(false); }
  }
  async function loadDrafts() { setBusy(true); setError(''); try { setDrafts(await api.request<Draft[]>(base + '/drafts')); } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); } }
  async function openDraft(id: string) { setBusy(true); setError(''); try { const d = await api.request<Draft>('/drafts/' + id); setSelected(d); setDraftText(d.content); } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); } }
  async function change(action: string) {
    if (!selected) return; setBusy(true); setError('');
    try { await api.request('/drafts/' + selected.id, 'POST', { action, revision: selected.revision, ...(action === 'save' ? { content: draftText } : {}) });
      await openDraft(selected.id); await loadDrafts(); message.success(({ save: '已保存草稿', submit: '已送审', publish: '已发布', reject: '已退回' } as Record<string, string>)[action]);
      if (action === 'publish') { setDoc(undefined); setDirectory(undefined); }
    } catch (e) { setError(errorMessage(e)); throw e; } finally { setBusy(false); }
  }
  async function find(q: string, cursor?: string) {
    if (!q) { setSearch(undefined); return; } const gen = ++generation.current; setBusy(true); setError(''); setSearchTerm(q);
    try { const data = await api.request<NonNullable<typeof search>>(base + '/search?' + query({ query: q, cursor })); if (gen === generation.current) setSearch(data); }
    catch (e) { if (gen === generation.current) setError(errorMessage(e)); } finally { if (gen === generation.current) setBusy(false); }
  }
  useEffect(() => { void browse(); return () => { generation.current++; }; }, [library.id]);
  const crumbs = [{ title: <Button type="link" onClick={() => navigate(back)}>知识库</Button> },
    { title: <Button type="link" onClick={() => navigate(() => { void browse(); })}>{library.name}</Button> },
    ...(path === '.' ? [] : path.split('/').map((part, i, parts) => ({ title: <Button type="link" onClick={() => navigate(() => { void browse(parts.slice(0, i + 1).join('/')); })}>{part}</Button> }))),
    ...(doc && tab === 'documents' ? [{ title: doc.path.split('/').pop() }] : [])];
  function openEntry(id: string) { const entry = directory?.entries.find(r => r.path === id); if (entry?.type === 'directory') void browse(id); else void open(id); }
  function switchView(t: string) { navigate(() => { setText(doc?.content || ''); setEditorRevision(v => v + 1); setTab(t); if (t === 'drafts') void loadDrafts(); if (t === 'documents' && !directory) void browse(path); }); }
  return <div className="workspace-browser">
    <Modal title="Agent 接入" open={agentSettings} footer={null} onCancel={() => setAgentSettings(false)} destroyOnHidden><LibraryAgentSettings api={api} id={library.id} /></Modal>
    <div className="workspace-topbar">
      <Tooltip title={doc && tab === 'documents' ? '返回列表' : '上一级'}><Button size="small" type="text" aria-label={doc && tab === 'documents' ? '返回列表' : '上一级'} icon={<ArrowLeftOutlined />} disabled={busy} onClick={() => navigate(() => {
        if (tab !== 'documents') switchView('documents'); else if (doc) { setDoc(undefined); } else if (path === '.') back(); else void browse(path.split('/').slice(0, -1).join('/') || '.');
      })} /></Tooltip>
      <Breadcrumb className="explorer-breadcrumb" items={crumbs} />
      <div className="workspace-actions">
        {library.role >= 4 && <Button size="small" type="text" onClick={() => setAgentSettings(true)}>Agent 接入</Button>}
        <span ref={setEditorTools} className="workspace-editor-tools" />
        {tab === 'documents' && !doc && <><Popover trigger="click" open={searchOpen} onOpenChange={setSearchOpen} content={<Input.Search autoFocus placeholder="搜索库内正文" aria-label="搜索文档内容" allowClear onSearch={q => { setSearchOpen(false); void find(q); }} />}><Button size="small" type="text" aria-label="搜索" icon={<SearchOutlined />} /></Popover><Tooltip title="刷新"><Button size="small" type="text" aria-label="刷新" icon={<ReloadOutlined />} disabled={busy} onClick={() => { void browse(path); }} /></Tooltip>{canEdit && <Button size="small" type="primary" onClick={() => { setFilename(''); setNewFile(true); }}>新建文档</Button>}</>}
        {tab === 'documents' && doc && canEdit && <>{text !== doc.content && <Button size="small" disabled={busy} onClick={() => navigate(() => { setText(doc.content); setEditorRevision(v => v + 1); })}>撤销修改</Button>}<Button size="small" type="primary" loading={busy} disabled={text === doc.content} onClick={async () => {
            setBusy(true); setError(''); try { const d = await api.request<{ id: string }>(base + '/drafts', 'POST', { path: doc.path, content: text, expected_version: doc.version }); setText(doc.content); setEditorRevision(v => v + 1); setTab('drafts'); await loadDrafts(); await openDraft(d.id); } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); }
          }}>保存为草稿</Button></>}
        <Dropdown trigger={['click']} menu={{ selectedKeys: [tab], onClick: ({ key }) => switchView(key), items: [{ key: 'documents', label: '文档' }, { key: 'drafts', label: '草稿与版本' }, ...(library.role >= 4 && library.organization_id ? [{ key: 'members', label: '成员权限' }] : []), { type: 'divider' }, { key: 'role', label: `当前权限：${roles[library.role]}`, disabled: true }] }}><Button size="small" type="text" className="workspace-view-switch" aria-label="切换视图">{{ documents: '文档', drafts: '草稿与版本', members: '成员权限' }[tab]} <DownOutlined /></Button></Dropdown>
      </div>
    </div>
    {error && <Alert className="block-gap" type="error" title={error} closable onClose={() => setError('')} />}
    <Tabs destroyOnHidden className="workspace-panels" activeKey={tab} renderTabBar={() => <></>} items={[
      { key: 'documents', label: '文档', children: doc ? <>
        <div className="document-sheet surface">{canEdit ? <MarkdownEditor toolbarContainer={editorTools} key={doc.path + doc.version + editorRevision} value={text} onChange={setText} disabled={busy} plain={!/\.md$/i.test(doc.path)} /> : <DocumentPreview content={doc.content} />}</div>
      </> : <>
        {search ? <><Table rowKey={r => `${r.path}:${r.line}`} loading={busy} pagination={false} size="small" dataSource={search.matches} columns={[{ title: '文档', render: (_, r) => <Button type="link" onClick={() => { void open(r.path); }}>{r.path}:{r.line}</Button> }, { title: '内容', dataIndex: 'text' }]} />
          <Space className="block-gap"><Button onClick={() => setSearch(undefined)}>返回目录</Button>{search.partial && <Typography.Text type="secondary">扫描未完成</Typography.Text>}{search.next_cursor && <Button onClick={() => { void find(searchTerm, search.next_cursor); }}>下一页</Button>}</Space></> : <>
          <FileExplorer key={path} loading={busy} onOpen={openEntry} items={(directory?.entries || []).map(r => ({ id: r.path, name: r.path.split('/').pop()!, folder: r.type === 'directory', modified: r.modified_at, bytes: r.bytes }))} />
          {directory?.partial && <Alert type="info" title="目录结果不完整，请进入具体子目录查看" />}{directory?.next_offset != null && <Button className="block-gap" loading={busy} onClick={() => { void browse(path, directory.next_offset!); }}>加载更多</Button>}
        </>}
      </> },
      { key: 'drafts', label: '草稿与版本', children: <Table<Draft> size="small" rowKey="id" loading={busy} pagination={{ hideOnSinglePage: true }} dataSource={drafts} columns={[{ title: '文档', dataIndex: 'path' }, { title: '作者', dataIndex: 'author' }, { title: '状态', render: (_, r) => <Tag>{statuses[r.status]}</Tag> }, { title: '更新时间', render: (_, r) => date(r.updated_at) }, { title: '操作', render: (_, r) => <Button type="link" onClick={() => { void openDraft(r.id); }}>查看</Button> }]} /> },
      ...(library.role >= 4 && library.organization_id ? [{ key: 'members', label: '成员权限', children: <LibraryMembers api={api} library={library} /> }] : []),
    ]} />
    <Modal title="新建文档" open={newFile} onCancel={() => setNewFile(false)} onOk={() => { if (!/^[^/\\:.][^/\\:]*\.(md|txt)$/i.test(filename)) { message.error('请输入 .md 或 .txt 文件名'); return; } const file = (path === '.' ? '' : path + '/') + filename; if (directory?.entries.some(r => r.path === file)) { message.error('文件已存在'); return; } setDoc({ path: file, content: '', version: emptyDigest }); setText(''); setNewFile(false); }}><Input aria-label="文件名" placeholder="例如：使用指南.md" value={filename} onChange={e => setFilename(e.target.value)} /></Modal>
    <Modal title={selected?.path} open={!!selected} width={1000} onCancel={() => navigate(() => setSelected(undefined))} footer={selected && <Space>
      <Button disabled={busy} onClick={() => navigate(() => { void openDraft(selected.id); })}>刷新</Button>
      {selected.status === 'draft' && selected.author_id === identity.id && <><Button loading={busy} disabled={draftText === selected.content} onClick={() => { void change('save').catch(() => {}); }}>保存草稿</Button><Button type="primary" disabled={draftText !== selected.content} loading={busy} onClick={() => { void change('submit').catch(() => {}); }}>送审</Button></>}
      {selected.status === 'submitted' && library.role >= 3 && <><Button danger loading={busy} onClick={() => modal.confirm({ title: '退回此草稿？', onOk: () => change('reject') })}>退回</Button><Button type="primary" loading={busy} onClick={() => modal.confirm({ title: '发布此版本？', content: '发布后，用户与 Agent 将读到新内容。', onOk: () => change('publish') })}>审核并发布</Button></>}
    </Space>}>
      {error && <Alert type="error" title={error} />}{selected && <><Tag>{statuses[selected.status]}</Tag><Tabs key={selected.id} defaultActiveKey={selected.status === 'draft' ? 'content' : 'diff'} items={[
        { key: 'content', label: selected.status === 'draft' ? '编辑草稿' : '候选正文', children: selected.status === 'draft' && selected.author_id === identity.id ? <MarkdownEditor key={selected.id + ':' + selected.revision} value={draftText} onChange={setDraftText} disabled={busy} plain={!/\.md$/i.test(selected.path)} /> : <DocumentPreview content={selected.content} /> },
        { key: 'diff', label: '修改对比', children: <pre className="file-preview">{selected.diff || '无文本差异'}</pre> },
      ]} /></>}
    </Modal>
  </div>;
}

function LibraryMembers({ api, library }: { api: Api; library: Library }) {
  const [members, setMembers] = useState<{ id: string; username: string; role: string }[]>([]), [candidates, setCandidates] = useState<{ id: string; username: string }[]>([]), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const [form] = Form.useForm();
  async function load() { setBusy(true); try { const [a, b] = await Promise.all([api.request<typeof members>(`/libraries/${library.id}/members`), api.request<typeof candidates>(`/libraries/${library.id}/candidates`)]); setMembers(a); setCandidates(b); } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); } }
  useEffect(() => { void load(); }, [library.id]);
  return <Card>{error && <Alert type="error" title={error} />}<Form form={form} layout="inline" initialValues={{ role: 'reader' }} onFinish={async v => { setBusy(true); setError(''); try { await api.request(`/libraries/${library.id}/members`, 'PUT', v); await load(); } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); } }}><Form.Item name="user_id" rules={[{ required: true }]}><Select className="organization-select" aria-label="组织成员" placeholder="组织成员" options={candidates.map(m => ({ value: m.id, label: m.username }))} /></Form.Item><Form.Item name="role"><Select className="role-select" aria-label="知识库角色" options={[{ value: 'reader', label: '读者' }, { value: 'editor', label: '编辑者' }, { value: 'publisher', label: '发布者' }, { value: 'manager', label: '管理者' }, { value: 'remove', label: '取消授权' }]} /></Form.Item><Button htmlType="submit" loading={busy}>保存权限</Button></Form><Table rowKey="id" dataSource={members} columns={[{ title: '成员', dataIndex: 'username' }, { title: '授权', dataIndex: 'role' }, { title: '操作', render: (_, r) => <Button type="link" onClick={() => form.setFieldsValue({ user_id: r.id, role: r.role })}>设置</Button> }]} /></Card>;
}

function UserKeys({ api, libraries }: { api: Api; libraries: Library[] }) {
  const [editing, setEditing] = useState<string>();
  const [keys, setKeys] = useState<{ id: string; agent_name: string; revoked_at?: string }[]>([]), [token, setToken] = useState(''), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const [create, setCreate] = useState(false), [creating, setCreating] = useState(false), [createError, setCreateError] = useState('');
  const [form] = Form.useForm(); const { modal } = App.useApp();
  async function load() { setBusy(true); setError(''); try { setKeys(await api.request('/keys')); } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); } }
  useEffect(() => { void load(); }, [api]);
  return <>
    <div className="workspace-topbar"><span className="workspace-location">我的 Agent Key</span><div className="workspace-actions">
      <Tooltip title="刷新"><Button size="small" type="text" aria-label="刷新" icon={<ReloadOutlined />} onClick={load} loading={busy} /></Tooltip>
      <Button size="small" type="primary" onClick={() => { setCreateError(''); setCreate(true); }}>添加 Key</Button>
    </div></div>
    <div className="workspace-key-list">{error && <Alert className="block-gap" type="error" title={error} />}
      <Table size="small" loading={busy} pagination={{ hideOnSinglePage: true }} rowKey="id" dataSource={keys} columns={[{ title: '名称', dataIndex: 'agent_name' }, { title: '状态', render: (_, r) => r.revoked_at ? '已撤销' : '有效' }, { title: '操作', render: (_, r) => !r.revoked_at && <Space><Button type="link" onClick={() => setEditing(r.id)}>权限设置</Button><Button type="link" danger onClick={() => modal.confirm({ title: '撤销此 Key？', onOk: async () => { try { await api.request(`/keys/${r.id}/revoke`, 'POST'); await load(); } catch (e) { setError(errorMessage(e)); throw e; } } })}>撤销</Button></Space> }]} />
    </div>
    <Modal title="权限设置" open={!!editing} onCancel={() => setEditing(undefined)} footer={null} width={880} destroyOnHidden>{editing && <KeyPolicy api={api} id={editing} libraries={libraries} done={() => { setEditing(undefined); void load(); }} />}</Modal>
    <Modal title="添加 Agent Key" open={create} destroyOnHidden onCancel={() => setCreate(false)} onOk={() => form.submit()} okText="创建 Key" confirmLoading={creating} cancelButtonProps={{ disabled: creating }} closable={!creating} maskClosable={!creating} keyboard={!creating}>
      <Form form={form} clearOnDestroy layout="vertical" onFinish={async v => {
        setCreating(true); setCreateError('');
        try { const r = await api.request<{ token: string }>('/keys', 'POST', v); setCreate(false); setToken(r.token); await load(); }
        catch (e) { setCreateError(errorMessage(e)); } finally { setCreating(false); }
      }}>
        <Form.Item name="name" label="名称" rules={[{ required: true, whitespace: true }]}><Input autoFocus maxLength={80} /></Form.Item>
        <Form.Item name="library_ids" label="可读取知识库" rules={[{ required: true }]}><Select mode="multiple" options={libraries.map(l => ({ label: l.name, value: l.id }))} /></Form.Item>
        <Form.Item name="submit_library_ids" label="可提交材料的知识库"><Select mode="multiple" options={libraries.filter(l => l.role >= 2).map(l => ({ label: l.name, value: l.id }))} /></Form.Item>
        {createError && <Alert type="error" title={createError} />}
      </Form>
    </Modal>
    <Modal open={!!token} title="复制 Agent Key" footer={<Button onClick={() => setToken('')}>完成</Button>} onCancel={() => setToken('')}><Typography.Paragraph copyable={{ text: token }} code>{token}</Typography.Paragraph><Typography.Text type="secondary">完整 Key 仅在本窗口展示。</Typography.Text></Modal>
  </>;
}
