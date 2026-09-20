import { describe, expect, it } from 'vitest';
import { correctedTimeline, sourceMediaForTranscript, sourceScriptForTranscript, transcriptPlaybackParts, transcriptSrt, timelineWords } from './transcriptModel';

const timeline = {
  schema: 'word_timeline.v1', media_hash: 'hash', language: 'zh', status: 'completed', transcript: '你好',
  words: [
    { text: '你', start_char: 0, end_char: 1, start_us: 0, end_us: 500_000, confidence: 0.4 },
    { text: '好', start_char: 1, end_char: 2, start_us: 500_000, end_us: 1_000_000, confidence: 0.9 },
  ],
};

describe('transcript node model', () => {
  it('preserves separators and repeated words when preparing playback links', () => {
    expect(transcriptPlaybackParts('go, go!', [
      { text: 'go', startUs: 100_000, endUs: 200_000 },
      { text: 'go', startUs: 500_000, endUs: 600_000 },
    ])).toEqual([
      { text: 'go', startUs: 100_000, endUs: 200_000 }, { text: ', ' },
      { text: 'go', startUs: 500_000, endUs: 600_000 }, { text: '!' },
    ]);
    expect(transcriptPlaybackParts('corrected text', [{ text: 'old', startUs: 0, endUs: 100_000 }])).toEqual([{ text: 'corrected text' }]);
    expect(transcriptPlaybackParts('other the', [{ text: 'the', startUs: 500_000, endUs: 600_000, startChar: 6, endChar: 9 }])).toEqual([
      { text: 'other ' }, { text: 'the', startUs: 500_000, endUs: 600_000 },
    ]);
    expect(transcriptPlaybackParts('😀其他他', [{ text: '他', startUs: 500_000, endUs: 600_000, startChar: 3, endChar: 4 }])).toEqual([
      { text: '😀其他' }, { text: '他', startUs: 500_000, endUs: 600_000 },
    ]);
  });

  it('chooses connected narration before video and ignores sources without a remix media path', () => {
    const nodes = [
      { id: 'video', data: { remixMedia: { media_path: 'video.mp4', media_kind: 'video', has_audio: true } } },
      { id: 'audio', data: { remixMedia: { media_path: 'speech.wav', media_kind: 'audio' } } },
      { id: 'external', data: { audioUrl: 'https://example.com/speech.wav' } },
    ];
    const edges = [{ source: 'video', target: 'transcript' }, { source: 'audio', target: 'transcript' }, { source: 'external', target: 'transcript' }];
    expect(sourceMediaForTranscript('transcript', nodes, edges)).toEqual({ path: 'speech.wav', url: undefined, kind: 'audio' });
    expect(sourceMediaForTranscript('transcript', nodes.slice(0, 1), edges)).toEqual({ path: 'video.mp4', url: undefined, kind: 'video' });
  });

  it('turns artifact words into the canvas word representation', () => {
    expect(timelineWords(timeline)).toEqual([
      { text: '你', startUs: 0, endUs: 500_000, startChar: 0, endChar: 1, confidence: 0.4 },
      { text: '好', startUs: 500_000, endUs: 1_000_000, startChar: 1, endChar: 2, confidence: 0.9 },
    ]);
  });

  it('uses a connected script artifact for forced alignment', () => {
    const nodes = [{ id: 'script', data: { remixScriptRef: { artifactId: 'script-1', revision: 2 } } }];
    expect(sourceScriptForTranscript('transcript', nodes, [{ source: 'script', target: 'transcript' }])).toEqual({ artifactId: 'script-1', revision: 2 });
    expect(sourceScriptForTranscript('transcript', nodes, [])).toBeNull();
  });

  it('rebuilds character spans and transcript when a word is corrected', () => {
    expect(correctedTimeline(timeline, ['您', '好'], true).words).toEqual([
      expect.objectContaining({ text: '您', start_char: 0, end_char: 1, start_us: 0, end_us: 500_000 }),
      expect.objectContaining({ text: '好', start_char: 1, end_char: 2, start_us: 500_000, end_us: 1_000_000 }),
    ]);
    expect(correctedTimeline(timeline, ['您', '好'], true).transcript).toBe('您好');
    expect(correctedTimeline(timeline, ['您', '好'], true).words[0]).toMatchObject({ timing_status: 'manually_verified', original_text: '你' });
    expect(() => correctedTimeline(timeline, ['', '好'], true)).toThrow('字词不能为空');
  });

  it('preserves separators in word-level transcripts when a word is corrected', () => {
    const wordTimeline = {
      ...timeline,
      transcript: 'go home',
      words: [
        { text: 'go', start_char: 0, end_char: 2, start_us: 0, end_us: 200_000 },
        { text: 'home', start_char: 3, end_char: 7, start_us: 300_000, end_us: 700_000 },
      ],
    };

    expect(correctedTimeline(wordTimeline, ['Go', 'home'], true).transcript).toBe('Go home');
  });

  it('requires explicit same-speech confirmation even when a replacement has the same length', () => {
    expect(() => correctedTimeline(timeline, ['您', '好'], false)).toThrow('需要重新对齐');
  });

  it('allows a confirmed single English word spelling correction while rebuilding offsets', () => {
    const wordTimeline = {
      ...timeline,
      transcript: 'Christine, five grams',
      words: [
        { text: 'Christine,', start_char: 0, end_char: 10, start_us: 0, end_us: 400_000 },
        { text: 'five', start_char: 11, end_char: 15, start_us: 500_000, end_us: 700_000 },
        { text: 'grams', start_char: 16, end_char: 21, start_us: 800_000, end_us: 1_000_000 },
      ],
    };
    const revised = correctedTimeline(wordTimeline, ['Creatine,', 'five', 'grams'], true);
    expect(revised.transcript).toBe('Creatine, five grams');
    expect(revised.words[1]).toMatchObject({ start_char: 10, end_char: 14, start_us: 500_000 });
    expect(revised.words[0]).toMatchObject({ timing_status: 'manually_verified', original_text: 'Christine,' });
  });

  it('rejects changed word length or boundaries despite confirmation', () => {
    expect(() => correctedTimeline(timeline, ['您好', '好'], true)).toThrow('需要重新对齐');
    expect(() => correctedTimeline(timeline, ['你 好', '好'], true)).toThrow('需要重新对齐');
  });

  it('exports playable SRT timing', () => {
    expect(transcriptSrt(timelineWords(timeline))).toBe('1\n00:00:00,000 --> 00:00:00,500\n你\n\n2\n00:00:00,500 --> 00:00:01,000\n好\n');
  });
});
