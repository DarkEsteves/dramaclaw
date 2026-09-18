import { describe, expect, it } from 'vitest';
import { cuesFromWordTimeline } from './transcriptionCues';

describe('cuesFromWordTimeline', () => {
  it('groups timed Chinese characters and keeps pauses separate', () => {
    const cues = cuesFromWordTimeline({ status: 'completed', transcript: '早上好今天好', words: [
      { text: '早', start_us: 0, end_us: 200_000 },
      { text: '上', start_us: 200_000, end_us: 400_000 },
      { text: '好', start_us: 400_000, end_us: 600_000 },
      { text: '今', start_us: 1_500_000, end_us: 1_700_000 },
      { text: '天', start_us: 1_700_000, end_us: 1_900_000 },
      { text: '好', start_us: 1_900_000, end_us: 2_100_000 },
    ] }, 3_000_000);
    expect(cues.map(({ text, startSeconds, endSeconds }) => ({ text, startSeconds, endSeconds }))).toEqual([
      { text: '早上好', startSeconds: 0, endSeconds: 0.6 },
      { text: '今天好', startSeconds: 1.5, endSeconds: 2.1 },
    ]);
  });

  it('preserves English spaces and rejects no-speech results', () => {
    const cues = cuesFromWordTimeline({ status: 'completed', transcript: 'hello world', words: [
      { text: 'hello', start_us: 0, end_us: 400_000 },
      { text: ' world', start_us: 400_000, end_us: 800_000 },
    ] }, 1_000_000);
    expect(cues[0].text).toBe('hello world');
    expect(cuesFromWordTimeline({ status: 'no_speech', words: [] }, 1_000_000)).toEqual([]);
  });

  it('restores spaces between character-aligned English words from source spans', () => {
    const cues = cuesFromWordTimeline({ status: 'completed', transcript: 'Hello world', words: [
      { text: 'H', start_char: 0, end_char: 1, start_us: 0, end_us: 100_000 },
      { text: 'e', start_char: 1, end_char: 2, start_us: 100_000, end_us: 200_000 },
      { text: 'l', start_char: 2, end_char: 3, start_us: 200_000, end_us: 300_000 },
      { text: 'l', start_char: 3, end_char: 4, start_us: 300_000, end_us: 400_000 },
      { text: 'o', start_char: 4, end_char: 5, start_us: 400_000, end_us: 500_000 },
      { text: 'w', start_char: 6, end_char: 7, start_us: 600_000, end_us: 700_000 },
      { text: 'o', start_char: 7, end_char: 8, start_us: 700_000, end_us: 800_000 },
      { text: 'r', start_char: 8, end_char: 9, start_us: 800_000, end_us: 900_000 },
      { text: 'l', start_char: 9, end_char: 10, start_us: 900_000, end_us: 1_000_000 },
      { text: 'd', start_char: 10, end_char: 11, start_us: 1_000_000, end_us: 1_100_000 },
    ] }, 2_000_000);
    expect(cues[0].text).toBe('Hello world');
  });

  it('clips the last caption to a renderable frame and keeps trailing punctuation', () => {
    const cues = cuesFromWordTimeline({ status: 'completed', transcript: '你好。', words: [
      { text: '你', start_char: 0, end_char: 1, start_us: 0, end_us: 1_000_000 },
      { text: '好', start_char: 1, end_char: 2, start_us: 1_000_000, end_us: 2_579_000 },
      { text: '。', start_char: 2, end_char: 3, start_us: 2_606_000, end_us: 2_627_000 },
    ] }, 2_561_000);
    expect(cues[0].text).toBe('你好。');
    expect(Math.round(cues[0].endSeconds * 30)).toBeLessThanOrEqual(Math.floor(2_561_000 * 30 / 1_000_000));
  });

  it('omits speech beyond the 60 second render limit', () => {
    const cues = cuesFromWordTimeline({ status: 'completed', transcript: '前后', words: [
      { text: '前', start_us: 1_000_000, end_us: 2_000_000 },
      { text: '后', start_us: 70_000_000, end_us: 71_000_000 },
    ] }, 90_000_000);
    expect(cues.map((cue) => cue.text)).toEqual(['前']);
  });
});
