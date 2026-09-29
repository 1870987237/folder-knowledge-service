import { lift, setBlockType, wrapIn } from '@milkdown/kit/prose/commands';
import { liftListItem, wrapInList } from '@milkdown/kit/prose/schema-list';
import { closeHistory } from '@milkdown/kit/prose/history';
import type { Command, EditorState } from '@milkdown/kit/prose/state';
import type { EditorView } from '@milkdown/kit/prose/view';

// Menu order and labels live here; each entry must have a lossless Markdown command.
export const blockFormats = [
  { key: 'paragraph', label: '正文段落', symbol: 'T' },
  ...[1, 2, 3, 4, 5, 6].map(level => ({ key: `h${level}`, label: `${level} 级标题`, symbol: `H${level}` })),
  { key: 'bullet_list', label: '无序列表', symbol: '•' },
  { key: 'ordered_list', label: '有序列表', symbol: '1.' },
  { key: 'blockquote', label: '引用', symbol: '❝' },
  { key: 'code_block', label: '代码块', symbol: '{ }' },
];

export function currentBlockFormat(state: EditorState): string {
  const { $from } = state.selection;
  for (let depth = $from.depth; depth > 0; depth--) {
    const node = $from.node(depth), name = node.type.name;
    if (name === 'heading') return `h${node.attrs.level}`;
    if (['code_block', 'bullet_list', 'ordered_list', 'blockquote'].includes(name)) return name;
  }
  return 'paragraph';
}

// Stage conversions before dispatch: a failed command cannot partially alter content,
// and switching out of a list/quote is one undoable operation, not several edits.
export function blockFormatCommand(format: string): Command {
  return (initial, dispatch) => {
    if (!blockFormats.some(item => item.key === format)) return false;
    if (initial.selection.empty && currentBlockFormat(initial) === format) return true;
    let state = initial;
    const result = initial.tr;
    const run = (command: Command) => command(state, tr => {
      tr.steps.forEach(step => result.step(step));
      result.setSelection(tr.selection.getBookmark().resolve(result.doc));
      state = state.apply(tr);
    });
    // Unwrap only lists and quotes. Never lift through a table cell boundary.
    while (true) {
      const { $from } = state.selection;
      let wrapper = '';
      for (let depth = $from.depth; depth > 0; depth--) {
        const name = $from.node(depth).type.name;
        if (['table_cell', 'table_header'].includes(name)) break;
        if (['list_item', 'blockquote'].includes(name)) { wrapper = name; break; }
      }
      if (!wrapper) break;
      const before = state.doc;
      if (!run(wrapper === 'list_item' ? liftListItem(state.schema.nodes.list_item) : lift) || state.doc.eq(before)) return false;
    }
    const { nodes } = state.schema;
    const heading = /^h([1-6])$/.exec(format);
    if (heading) {
      if (!run(setBlockType(nodes.heading, { level: Number(heading[1]) }))) return false;
    } else if (format === 'code_block') {
      if (!run(setBlockType(nodes.code_block))) return false;
    } else {
      // Lists require paragraphs as their first child. Existing paragraphs are fine.
      run(setBlockType(nodes.paragraph));
      if (format === 'blockquote' && !run(wrapIn(nodes.blockquote))) return false;
      if (['bullet_list', 'ordered_list'].includes(format) && !run(wrapInList(nodes[format]))) return false;
    }
    if (result.docChanged) dispatch?.(closeHistory(result).scrollIntoView());
    return true;
  };
}

export function applyBlockFormat(view: EditorView, format: string): boolean {
  const applied = blockFormatCommand(format)(view.state, view.dispatch);
  if (applied) view.focus();
  return applied;
}
