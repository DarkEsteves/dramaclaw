import { describe, expect, it } from 'vitest';
import { fitBeatToSingleVoice, retimeBeat, voiceTimingHint } from './productionTimelineModel';
import type { PlanBeat } from './productionPlanModel';

const beat = (): PlanBeat => ({
  id: 'hook', purpose: '提出问题', duration_frames: 90,
  turns: [{ speaker: '甲', text: '今天喝什么？', start_frame: 0, end_frame: 90 }],
  visuals: [
    { id: 'a', role: 'primary', description: '开场', start_frame: 0, end_frame: 45 },
    { id: 'b', role: 'primary', description: '回答', start_frame: 45, end_frame: 90 },
  ],
  audio: [{ id: 'voice', role: 'narration', description: '人声', media: {
    node_id: 'audio-1', media_hash: 'a'.repeat(64), media_path: 'voice.wav',
    media_kind: 'audio', duration_us: 2_000_000, has_audio: true,
  } }],
});

describe('production timeline timing', () => {
  it('shows the actual voice duration beside an estimated beat', () => {
    expect(voiceTimingHint(beat(), 30)).toEqual({ sourceSeconds: 2, plannedSeconds: 3, differs: true });
  });

  it('fits one full-beat voice while preserving contiguous shot and subtitle boundaries', () => {
    const fitted = fitBeatToSingleVoice(beat(), 30);
    expect(fitted?.duration_frames).toBe(60);
    expect(fitted?.visuals.map((cue) => [cue.start_frame, cue.end_frame])).toEqual([[0, 30], [30, 60]]);
    expect(fitted?.turns[0]).toMatchObject({ start_frame: 0, end_frame: 60 });
  });

  it('does not guess duration for silent or multi-voice beats', () => {
    const silent = beat(); silent.audio = [];
    expect(fitBeatToSingleVoice(silent, 30)).toBeNull();
    const conversation = beat(); conversation.audio.push({ ...conversation.audio[0], id: 'voice-2' });
    expect(fitBeatToSingleVoice(conversation, 30)).toBeNull();
  });

  it('keeps adjacent shots contiguous when the user changes a beat duration', () => {
    const changed = retimeBeat(beat(), 120);
    expect(changed.visuals.map((cue) => [cue.start_frame, cue.end_frame])).toEqual([[0, 60], [60, 120]]);
    expect(changed.turns[0].end_frame).toBe(120);
  });
});
