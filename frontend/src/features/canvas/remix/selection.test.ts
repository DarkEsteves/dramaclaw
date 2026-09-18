import { describe, expect, it } from 'vitest';

import {
  codePointSpanFromUtf16Range,
  rebaseSelectionAfterEdit,
  utf16RangeFromCodePointSpan,
} from './selection';
import { rebaseNamedSelections } from './model';

describe('remix script selections', () => {
  it('converts DOM UTF-16 offsets to Unicode code point spans', () => {
    const text = '早😀喝咖啡';
    expect(codePointSpanFromUtf16Range(text, 3, 5)).toEqual({ start: 2, end: 4 });
    expect(utf16RangeFromCodePointSpan(text, 2, 4)).toEqual({ start: 3, end: 5 });
  });

  it('rejects offsets that split a surrogate pair', () => {
    expect(() => codePointSpanFromUtf16Range('早😀喝', 2, 3)).toThrow(/surrogate/);
  });

  it('moves a selection after a safe insertion before it', () => {
    expect(rebaseSelectionAfterEdit('早上喝咖啡', '今天早上喝咖啡', { start: 3, end: 5 }))
      .toEqual({ start: 5, end: 7 });
  });

  it('marks an edited semantic selection unresolved, even with repeated text', () => {
    expect(rebaseSelectionAfterEdit('咖啡和咖啡', '咖啡和茶饮', { start: 3, end: 5 })).toBeNull();
  });

  it('preserves a named selection or records its invalid reason after an edit', () => {
    expect(rebaseNamedSelections('咖啡和咖啡', '咖啡和茶饮', [{ id: 'reveal', label: '商品登场', start: 3, end: 5 }]))
      .toEqual([{ id: 'reveal', label: '商品登场', start: 3, end: 5, invalidReason: 'Script edit changed this selection' }]);
  });
});
