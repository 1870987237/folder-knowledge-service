import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Alert, Button, Dropdown, Input, Segmented, Spin, theme } from 'antd';
import { DownOutlined } from '@ant-design/icons';
import { visualMarkdownReason, sameMarkdown } from './markdownSafety';
import { themeVariables } from './theme';
import type { Crepe } from '@milkdown/crepe';

// The owner supplies a new key when switching documents or reloading a revision.
export function MarkdownEditor({ value, onChange, disabled = false, plain = false, toolbarContainer }: {
  value: string; onChange: (value: string) => void; disabled?: boolean; plain?: boolean; toolbarContainer?: HTMLElement | null;
}) {
  const { token } = theme.useToken();
  const [mode, setMode] = useState(plain || visualMarkdownReason(value) ? '源码' : '正文');
  const [reason, setReason] = useState(plain ? '' : visualMarkdownReason(value));
  const [loading, setLoading] = useState(false);
  const [format, setFormat] = useState('paragraph');
  const [formats, setFormats] = useState<Array<{ key: string; label: string; symbol: string }>>([]);
  const applyFormat = useRef<((key: string) => boolean) | undefined>(undefined);
  const root = useRef<HTMLDivElement>(null), editor = useRef<Crepe | undefined>(undefined);
  const latest = useRef(value), change = useRef(onChange), locked = useRef(disabled);
  latest.current = value; change.current = onChange; locked.current = disabled;
  useEffect(() => { editor.current?.setReadonly(disabled); }, [disabled]);
  useEffect(() => {
    if (mode !== '正文' || !root.current) return;
    const host = root.current; let cancelled = false; let created = false; let instance: Crepe | undefined;
    setLoading(true);
    void (async () => {
      try {
        const [{ Crepe }, { $prose }, { Plugin }, { EditorView }, { editorViewCtx }, blockFormatting] = await Promise.all([
          import('@milkdown/crepe'), import('@milkdown/kit/utils'), import('@milkdown/kit/prose/state'),
          import('@codemirror/view'), import('@milkdown/kit/core'), import('./blockFormats'),
          import('@milkdown/crepe/theme/common/style.css'), import('@milkdown/crepe/theme/frame.css'),
        ]);
        if (cancelled) return;
        setFormats(blockFormatting.blockFormats);
        const original = latest.current;
        instance = new Crepe({ root: host, defaultValue: original, features: {
          [Crepe.Feature.ImageBlock]: false, [Crepe.Feature.Latex]: false,
          [Crepe.Feature.AI]: false, [Crepe.Feature.TopBar]: false,
        }, featureConfigs: {
          // Native caret stays aligned during nested scrolling, zoom and Chinese input.
          [Crepe.Feature.Cursor]: { virtual: false },
          [Crepe.Feature.CodeMirror]: { theme: EditorView.theme({}, { dark: false }), extensions: [EditorView.cspNonce.of(document.querySelector<HTMLMetaElement>('meta[name="csp-nonce"]')?.content || '')], copyText: '复制', searchPlaceholder: '搜索语言', noResultText: '无匹配语言' },
          [Crepe.Feature.Placeholder]: { text: '输入正文，或输入 / 插入内容' },
          [Crepe.Feature.Toolbar]: { boldLabel: '加粗', italicLabel: '斜体', codeLabel: '代码', linkLabel: '链接', strikethroughLabel: '删除线' },
        } });
        let ready = false;
        instance.editor.use($prose(() => new Plugin({ filterTransaction(tr) {
          let safe = true;
          tr.doc.descendants(node => {
            if (['image', 'html'].includes(node.type.name)) safe = false;
            if (node.marks.some(mark => mark.type.name === 'link' && !/^https?:\/\//i.test(mark.attrs.href || ''))) safe = false;
          });
          return safe;
        }, view: () => ({ update(view, previous) {
          if (cancelled || !ready) return;
          setFormat(blockFormatting.currentBlockFormat(view.state));
          if (locked.current || view.state.doc.eq(previous.doc)) return;
          const markdown = instance!.getMarkdown();
          if (!sameMarkdown(latest.current, markdown)) {
            // Synchronous transaction updates prevent losing the last keystroke on save or mode switch.
            const next = sameMarkdown(original, markdown) ? original : markdown;
            latest.current = next; change.current(next);
          }
        } }) })));
        await instance.create();
        created = true;
        if (cancelled) { await instance.destroy(); return; }
        if (!sameMarkdown(original, instance.getMarkdown())) {
          setReason('此文档包含正文编辑器不能完整保留的格式，已切换到源码。'); setMode('源码'); return;
        }
        editor.current = instance; instance.setReadonly(locked.current); ready = true;
        const view = instance.editor.ctx.get(editorViewCtx);
        setFormat(blockFormatting.currentBlockFormat(view.state));
        applyFormat.current = key => !locked.current && blockFormatting.applyBlockFormat(view, key);
        host.querySelector('[contenteditable]')?.setAttribute('aria-label', '文档正文');
      } catch { if (!cancelled) { setReason('正文编辑器未能加载，可继续使用源码编辑。'); setMode('源码'); } }
      finally { if (!cancelled) setLoading(false); }
    })();
    return () => { cancelled = true; editor.current = undefined; applyFormat.current = undefined; if (created && instance) void instance.destroy().catch(() => {}); };
  }, [mode]);
  const modeControl = <div className="document-format-tools">
    {mode === '正文' && <Dropdown trigger={['click']} disabled={disabled || loading} menu={{
      selectedKeys: [format],
      items: formats.map(item => ({ key: item.key, label: item.label, icon: <span className="block-format-symbol">{item.symbol}</span> })),
      onClick: ({ key }) => { if (!applyFormat.current?.(key)) setReason('当前选区不能转换为此格式，请将光标放在一个段落内。'); else setReason(''); },
    }}><Button size="small" type="text" aria-label="段落格式" onMouseDown={e => e.preventDefault()} disabled={disabled || loading}>
      {formats.find(item => item.key === format)?.label || '段落格式'} <DownOutlined />
    </Button></Dropdown>}
    <Segmented size="small" value={mode} disabled={disabled} options={['正文', '源码']} onChange={v => {
      const why = visualMarkdownReason(latest.current);
      if (v === '正文' && why) { setReason(why); return; }
      setReason(''); setMode(v);
    }} /></div>;
  return <div className="markdown-editor" style={themeVariables(token)}>
    {!plain && (toolbarContainer ? createPortal(modeControl, toolbarContainer) : <div className="editor-mode">{modeControl}</div>)}
    {reason && <Alert type="info" title={reason} className="block-gap" />}
    {mode === '源码' ? <Input.TextArea aria-label="文档源码" className="document-source" value={value} disabled={disabled} onChange={e => onChange(e.target.value)} rows={18} /> : <Spin spinning={loading}><div ref={root} className="visual-editor" /></Spin>}
  </div>;
}
