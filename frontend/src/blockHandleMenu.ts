import { TextSelection } from '@milkdown/kit/prose/state';
import type { EditorView } from '@milkdown/kit/prose/view';

export type ActiveBlock = { from: number; to: number };

// Crepe's six-dot control only implements dragging. Add click/keyboard activation
// without cancelling pointerdown, mousedown or dragstart used by its drag service.
export function bindBlockHandleMenu(host: HTMLElement, view: EditorView,
  activeBlock: () => ActiveBlock | undefined,
  open: (position: { x: number; y: number }) => void) {
  const selector = '.milkdown-block-handle .operation-item:nth-child(2)';
  let dragged = false;
  let pressed: ActiveBlock | undefined;
  const handle = (event: Event) => event.target instanceof Element ? event.target.closest<HTMLElement>(selector) : null;
  const annotate = () => {
    const control = host.querySelector<HTMLElement>(selector);
    if (!control) return;
    control.setAttribute('role', 'button');
    control.setAttribute('tabindex', '0');
    control.setAttribute('aria-label', '段落菜单');
    control.setAttribute('aria-haspopup', 'menu');
    control.title = '段落菜单（拖动可调整顺序）';
  };
  const observer = new MutationObserver(annotate);
  observer.observe(host, { childList: true, subtree: true });
  annotate();
  const pointerdown = (event: Event) => {
    if (handle(event)) { dragged = false; pressed = activeBlock(); }
  };
  const dragstart = () => { dragged = true; };
  const activate = (control: HTMLElement, block: ActiveBlock | undefined) => {
    if (!view.editable || !block || block.from < 0 || block.to > view.state.doc.content.size) return;
    const node = view.state.doc.nodeAt(block.from);
    if (!node || node.isLeaf) return;
    // Use the block under this handle, even when the caret was in another paragraph.
    const selection = TextSelection.between(view.state.doc.resolve(block.from + 1), view.state.doc.resolve(block.to - 1));
    view.dispatch(view.state.tr.setSelection(selection));
    const rect = control.getBoundingClientRect();
    open({ x: rect.left, y: rect.bottom });
  };
  const click = (event: MouseEvent) => {
    const control = handle(event);
    if (!control || dragged) return;
    event.preventDefault();
    activate(control, pressed || activeBlock());
    pressed = undefined;
  };
  const keydown = (event: KeyboardEvent) => {
    const control = handle(event);
    if (!control || !['Enter', ' '].includes(event.key)) return;
    event.preventDefault();
    activate(control, activeBlock());
  };
  host.addEventListener('pointerdown', pointerdown, true);
  host.addEventListener('dragstart', dragstart, true);
  host.addEventListener('click', click);
  host.addEventListener('keydown', keydown);
  return () => {
    observer.disconnect();
    host.removeEventListener('pointerdown', pointerdown, true);
    host.removeEventListener('dragstart', dragstart, true);
    host.removeEventListener('click', click);
    host.removeEventListener('keydown', keydown);
  };
}
