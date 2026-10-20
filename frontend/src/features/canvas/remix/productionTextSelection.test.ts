import { expect, it } from 'vitest';
import { readProductionTextSelection } from './productionTextSelection';

it('reads a DOM selection across turns in Unicode code points', () => {
  const root = document.createElement('div');
  root.innerHTML = '<span data-turn-id="a">甲🙂乙</span><span data-turn-id="b">每天一杯</span>';
  document.body.append(root);
  const first = root.querySelector('[data-turn-id="a"]')!.firstChild!;
  const second = root.querySelector('[data-turn-id="b"]')!.firstChild!;
  const range = document.createRange();
  range.setStart(first, 3);
  range.setEnd(second, 2);
  const selection = window.getSelection()!;
  selection.removeAllRanges();
  selection.addRange(range);
  expect(readProductionTextSelection(root, selection)).toEqual({
    start: { turn_id: 'a', char_offset: 2 }, end: { turn_id: 'b', char_offset: 2 },
  });
  selection.removeAllRanges();
  root.remove();
});
