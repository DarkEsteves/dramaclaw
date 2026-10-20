import { describe, expect, it } from 'vitest';
import { sourceSrtDialogue, parseSourceSrt, exportProgramSrt, recognizedWordsToSourceSrt, recognizedWordsToPlanCues } from './productionSrt';
describe('SRT source boundary', () => {
 it('preserves multiline cues and converts milliseconds once', () => {
  expect(parseSourceSrt('\uFEFF1\r\n00:00:00,500 --> 00:00:01,200\r\n第一行\r\n第二行')).toEqual([{ text: '第一行\n第二行', source_start_us: 500000, source_end_us: 1200000 }]);
 });
 it('rejects empty, invalid and reversed ranges', () => {
  for (const text of ['00:00:00,500 --> 00:00:01,000\nx', '1\n00:00:00.500 --> 00:00:01.000\nx', '', '1\n00:60:00,000 --> 00:61:00,000\nx', '1\n00:00:02,000 --> 00:00:01,000\nx']) expect(() => parseSourceSrt(text)).toThrow();
 });
 it('exports projected program time', () => expect(exportProgramSrt([{ text: '你好', start_us: 1250000, end_us: 2500000 }])).toBe('1\n00:00:01,250 --> 00:00:02,500\n你好\n'));
});

it('normalizes only the selected speaker prefix and validates dialogue content', () => {
 expect(sourceSrtDialogue('嘉宾：你好', '嘉宾', '你好世界')).toBe('你好');
 expect(sourceSrtDialogue('嘉宾: 你好', '嘉宾', '你好世界')).toBe('你好');
 expect(() => sourceSrtDialogue('主持：你好', '嘉宾', '你好世界')).toThrow();
 expect(() => sourceSrtDialogue('嘉宾：再见', '嘉宾', '你好世界')).toThrow();
});

it('exports chronological program cues with stable start/end ties without mutating author order', () => {
 const rows = [
  { text: 'late', start_us: 3000000, end_us: 4000000 },
  { text: 'long overlap', start_us: 1000000, end_us: 2500000 },
  { text: 'first tie', start_us: 1000000, end_us: 2000000 },
  { text: 'second tie', start_us: 1000000, end_us: 2000000 },
 ];
 const original = structuredClone(rows);
 const exported = parseSourceSrt(exportProgramSrt(rows));
 expect(exported.map((row) => row.text)).toEqual(['first tie', 'second tie', 'long overlap', 'late']);
 expect(rows).toEqual(original);
 expect(exported.map((row) => row.source_start_us)).toEqual([1000000, 1000000, 1000000, 3000000]);
});

it('preserves imported source cue order even when it differs from chronological order', () => {
 const imported = parseSourceSrt('1\n00:00:03,000 --> 00:00:04,000\nlate\n\n2\n00:00:01,000 --> 00:00:02,000\nearly');
 expect(imported.map((row) => row.text)).toEqual(['late', 'early']);
});

it('drafts editable source SRT only from actual recognized word boundaries', () => {
 const draft = recognizedWordsToSourceSrt([
  { text: '桌上那是空杯。', start_us: 1250000, end_us: 3000000 },
  { text: '行，下一题。', start_us: 3600000, end_us: 4700000 },
 ], 6000000);
 expect(parseSourceSrt(draft)).toEqual([
  { text: '桌上那是空杯。', source_start_us: 1250000, source_end_us: 3000000 },
  { text: '行，下一题。', source_start_us: 3600000, source_end_us: 4700000 },
 ]);
 expect(() => recognizedWordsToSourceSrt([], 6000000)).toThrow(/真实词时间/);
 expect(() => recognizedWordsToSourceSrt([{ text: '越界', start_us: 5900000, end_us: 6100000 }], 6000000)).toThrow(/超出/);
});

it('keeps spaces between timed English tokens when drafting recognized SRT', () => {
 expect(parseSourceSrt(recognizedWordsToSourceSrt([
  { text: 'go', start_char: 0, end_char: 2, start_us: 0, end_us: 200_000 },
  { text: 'now', start_char: 3, end_char: 6, start_us: 250_000, end_us: 600_000 },
 ], 1_000_000, 'go now'))[0].text).toBe('go now');
});

it('keeps source spacing when an individual recognized token is corrected', () => {
 expect(parseSourceSrt(recognizedWordsToSourceSrt([
  { text: '我', start_char: 0, end_char: 1, start_us: 0, end_us: 200_000 },
  { text: '审片', start_char: 2, end_char: 4, start_us: 300_000, end_us: 700_000 },
 ], 1_000_000, '我 省片'))[0].text).toBe('我 审片');
});

it('matches corrected words to ordered plan dialogue in one pass without inventing times', () => {
 const result = recognizedWordsToPlanCues([
  { text: '我', start_us: 0, end_us: 200_000 },
  { text: '审片', start_us: 250_000, end_us: 750_000 },
  { text: '行', start_us: 1_400_000, end_us: 1_700_000 },
  { text: '下一题', start_us: 1_700_000, end_us: 2_300_000 },
 ], [{ id: 'guest', text: '我审片？' }, { id: 'host', text: '……行，下一题。' }], 3_000_000);
 expect(result.rows).toEqual([
  { turn_id: 'guest', text: '我审片？', source_start_us: 0, source_end_us: 750_000 },
  { turn_id: 'host', text: '……行，下一题。', source_start_us: 1_400_000, source_end_us: 2_300_000 },
 ]);
 expect(parseSourceSrt(result.srt).map((row) => row.text)).toEqual(['我审片？', '……行，下一题。']);
});

it('blocks a one-click save if a word differs, crosses a turn boundary, or exceeds the source', () => {
 const turns = [{ id: 'one', text: '我' }, { id: 'two', text: '行' }];
 expect(() => recognizedWordsToPlanCues([{ text: '窝', start_us: 0, end_us: 200_000 }, { text: '行', start_us: 300_000, end_us: 500_000 }], turns, 1_000_000)).toThrow(/第 1 句/);
 expect(() => recognizedWordsToPlanCues([{ text: '我行', start_us: 0, end_us: 500_000 }], turns, 1_000_000)).toThrow(/跨越/);
 expect(() => recognizedWordsToPlanCues([{ text: '我', start_us: 0, end_us: 200_000 }, { text: '行', start_us: 900_000, end_us: 1_100_000 }], turns, 1_000_000)).toThrow(/超出/);
});

it('encloses microsecond word boundaries when serializing millisecond SRT cues', () => {
 const result = recognizedWordsToPlanCues([
  { text: '行', start_us: 5_099_999, end_us: 5_420_000 },
  { text: '下一题', start_us: 5_420_000, end_us: 6_059_999 },
 ], [{ id: 'host', text: '……行，下一题。' }], 6_080_000);
 expect(result.rows).toEqual([{ turn_id: 'host', text: '……行，下一题。', source_start_us: 5_099_000, source_end_us: 6_060_000 }]);
});
