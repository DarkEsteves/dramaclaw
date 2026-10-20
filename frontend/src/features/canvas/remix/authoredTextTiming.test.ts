import { expect, it } from 'vitest';
import { codePointOffset, hashAuthorText, locateTextBoundary, utf16Offset } from './authoredTextTiming';

it('hashes the exact author text for a saved text anchor', async () => {
  expect(await hashAuthorText('abc')).toBe('ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
});

it('converts DOM UTF-16 offsets to persisted Unicode code points', () => {
  expect(codePointOffset('甲🙂乙', 3)).toBe(2);
  expect(utf16Offset('甲🙂乙', 2)).toBe(3);
  expect(() => codePointOffset('甲🙂乙', 2)).toThrow(/surrogate/);
});

it('uses measured neighbors without swallowing a pause', () => {
  const words = [
    { id: 'w1', text: '甲', start_char: 0, end_char: 1, start_us: 100_000, end_us: 300_000 },
    { id: 'w2', text: '乙', start_char: 1, end_char: 2, start_us: 600_000, end_us: 900_000 },
  ];
  expect(locateTextBoundary({ text: '甲乙', charOffset: 1, affinity: 'before', words }).sourceUs).toBe(300_000);
  expect(locateTextBoundary({ text: '甲乙', charOffset: 1, affinity: 'after', words }).sourceUs).toBe(600_000);
  expect(locateTextBoundary({ text: '甲乙', charOffset: 1, affinity: 'after', words }).quality).toBe('measured');
});

it('uses sentence cues only at sentence boundaries', () => {
  expect(locateTextBoundary({ text: '每天一杯', charOffset: 0, affinity: 'after', words: [], cueStartUs: 200_000 }).quality).toBe('manual');
  expect(locateTextBoundary({ text: '每天一杯', charOffset: 2, affinity: 'after', words: [], cueStartUs: 200_000 }).issueCode).toBe('missing_word_evidence');
});
