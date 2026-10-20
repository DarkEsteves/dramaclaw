import { codePointOffset } from './authoredTextTiming';

export interface ProductionTextEndpoint { turn_id: string; char_offset: number }
export interface ProductionTextRange { start: ProductionTextEndpoint; end: ProductionTextEndpoint }

function endpoint(root: HTMLElement, container: Node, offset: number): ProductionTextEndpoint | null {
  const element = container.nodeType === Node.ELEMENT_NODE ? container as Element : container.parentElement;
  const turn = element?.closest<HTMLElement>('[data-turn-id]');
  if (!turn || !root.contains(turn) || !turn.dataset.turnId) return null;
  const prefix = document.createRange();
  prefix.selectNodeContents(turn);
  try { prefix.setEnd(container, offset); } catch { return null; }
  const utf16 = prefix.toString().length;
  return { turn_id: turn.dataset.turnId, char_offset: codePointOffset(turn.textContent ?? '', utf16) };
}

/** Read only a real selection inside the rendered script; Range sorts reverse drags. */
export function readProductionTextSelection(root: HTMLElement, selection: Selection | null): ProductionTextRange | null {
  if (!selection || selection.rangeCount !== 1 || selection.isCollapsed) return null;
  const range = selection.getRangeAt(0);
  const start = endpoint(root, range.startContainer, range.startOffset);
  const end = endpoint(root, range.endContainer, range.endOffset);
  if (!start || !end || start.turn_id === end.turn_id && start.char_offset === end.char_offset) return null;
  return { start, end };
}
