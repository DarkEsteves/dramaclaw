import { describe, expect, it } from 'vitest';
import { correctedTimeline, sourceMediaForTranscript, sourceScriptForTranscript, transcriptSrt, timelineWords } from './transcriptModel';

const timeline = {
  schema: 'word_timeline.v1', media_hash: 'hash', language: 'zh', status: 'completed', transcript: '你好',
  words: [
    { text: '你', start_char: 0, end_char: 1, start_us: 0, end_us: 500_000, confidence: 0.4 },
    { text: '好', start_char: 1, end_char: 2, start_us: 500_000, end_us: 1_000_000, confidence: 0.9 },
  ],
};

describe('transcript node model', () => {
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
      { text: '你', startUs: 0, endUs: 500_000, confidence: 0.4 },
      { text: '好', startUs: 500_000, endUs: 1_000_000, confidence: 0.9 },
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

  it('requires explicit same-speech confirmation even when a replacement has the same length', () => {
    expect(() => correctedTimeline(timeline, ['您', '好'], false)).toThrow('需要重新对齐');
  });

  it('rejects changed word length or boundaries despite confirmation', () => {
    expect(() => correctedTimeline(timeline, ['您好', '好'], true)).toThrow('需要重新对齐');
    expect(() => correctedTimeline(timeline, ['你 好', '好'], true)).toThrow('需要重新对齐');
  });

  it('exports playable SRT timing', () => {
    expect(transcriptSrt(timelineWords(timeline))).toBe('1\n00:00:00,000 --> 00:00:00,500\n你\n\n2\n00:00:00,500 --> 00:00:01,000\n好\n');
  });
});
