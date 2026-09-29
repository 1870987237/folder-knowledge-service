import { describe, expect, it } from 'vitest';
import { Schema } from '@milkdown/kit/prose/model';
import { EditorState, TextSelection } from '@milkdown/kit/prose/state';
import { history, undo } from '@milkdown/kit/prose/history';
import { blockFormatCommand, currentBlockFormat } from './blockFormats';

const schema = new Schema({ nodes: {
  doc: { content: 'block+' }, text: { group: 'inline' },
  paragraph: { group: 'block', content: 'inline*' },
  heading: { group: 'block', content: 'inline*', attrs: { level: { default: 1 } } },
  blockquote: { group: 'block', content: 'block+' },
  code_block: { group: 'block', content: 'text*', code: true },
  bullet_list: { group: 'block', content: 'list_item+' },
  ordered_list: { group: 'block', content: 'list_item+' },
  list_item: { content: 'paragraph block*' },
} });
const p = (text: string) => schema.node('paragraph', null, schema.text(text));
const heading = (level: number) => schema.node('heading', { level }, schema.text('保留标题内容'));
function documentState(nodes: Parameters<typeof schema.node>[2]) {
  return EditorState.create({ schema, doc: schema.node('doc', null, nodes), plugins: [history()] });
}
function convert(state: EditorState, kind: string) {
  let result = state;
  expect(blockFormatCommand(kind)(state, tr => { result = state.apply(tr); })).toBe(true);
  return result;
}

describe('paragraph format conversions', () => {
  it('changes an existing heading level and turns it back into body without losing text', () => {
    let state = documentState([heading(2), p('下一段')]);
    state = convert(state, 'h4');
    expect(currentBlockFormat(state)).toBe('h4');
    state = convert(state, 'paragraph');
    expect(state.doc.firstChild?.type.name).toBe('paragraph');
    expect(state.doc.textContent).toBe('保留标题内容下一段');
  });
  it('unwraps just the selected list item and preserves neighbouring items', () => {
    const list = schema.node('bullet_list', null, ['甲', '乙', '丙'].map(text => schema.node('list_item', null, p(text))));
    let state = documentState([list]);
    state = state.apply(state.tr.setSelection(TextSelection.create(state.doc, 8)));
    state = convert(state, 'h2');
    expect(state.doc.childCount).toBe(3);
    expect(state.doc.child(1).type.name).toBe('heading');
    expect(state.doc.child(1).textContent).toBe('乙');
    expect(state.doc.textContent).toBe('甲乙丙');
  });
  it('converts nested quote/list to paragraph in one undo step', () => {
    const initial = documentState([schema.node('blockquote', null, schema.node('bullet_list', null, schema.node('list_item', null, p('内容'))))]);
    let state = convert(initial, 'paragraph');
    expect(state.doc.firstChild?.type.name).toBe('paragraph');
    expect(undo(state, tr => { state = state.apply(tr); })).toBe(true);
    expect(state.doc.eq(initial.doc)).toBe(true);
  });
  it('supports list, quote and code conversion without changing content', () => {
    let state = documentState([heading(2)]);
    for (const kind of ['bullet_list', 'ordered_list', 'blockquote', 'code_block', 'paragraph']) {
      state = convert(state, kind);
      expect(currentBlockFormat(state)).toBe(kind);
      expect(state.doc.textContent).toBe('保留标题内容');
    }
  });
  it('converts mixed multi-paragraph selections even when the first block is already body', () => {
    let state = documentState([p('正文'), heading(2)]);
    state = state.apply(state.tr.setSelection(TextSelection.create(state.doc, 1, state.doc.content.size - 1)));
    state = convert(state, 'paragraph');
    expect(state.doc.child(1).type.name).toBe('paragraph');
  });
  it('rejects unknown formats without dispatching any changes', () => {
    let dispatched = false;
    expect(blockFormatCommand('unknown')(documentState([p('原文')]), () => { dispatched = true; })).toBe(false);
    expect(dispatched).toBe(false);
  });
});
