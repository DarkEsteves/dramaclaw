import { describe, expect, it } from 'vitest';
import { applyAcceptedSpeechCues, applyCorrectedSpeechWords } from './productionSpeechCorrection';
import type { ProductionPlanV2 } from './productionPlanV2Model';

const basePlan = (): ProductionPlanV2 => ({
  schema_version: 2, goal: '对白', output: { width: 1920, height: 1080, fps_num: 24, fps_den: 1 },
  beats: [{ id: 'beat', purpose: '对话', duration_frames: 48, turns: [{ id: 'turn', speaker: '甲', text: '黑咖啡？' }] }],
  resources: [{ id: 'media', node_id: 'video', media_kind: 'video', media_path: 'a.mp4', media_hash: 'b'.repeat(64), duration_us: 3_000_000, has_audio: true }],
  placements: [], provenance: {},
  speech_takes: [{ id: 'take', beat_id: 'beat', resource_id: 'media', turn_ids: ['turn'], origin_placement_id: 'main', source_in_us: 0, source_out_us: null, speed_num: 1, speed_den: 1, lead_in_frames: 0 }],
  speech_cues: [{ id: 'cue', turn_id: 'turn', take_id: 'take', text: '黑咖啡？', source_start_us: 100_000, source_end_us: 500_000, timing_source: 'srt_import', timing_status: 'ready', word_ids: [], evidence_ref: { artifact_id: 'old', revision: 1 } }],
});

describe('corrected source words', () => {
  it('rejects a sentence-only import that would erase an existing word anchor', () => {
    const plan = basePlan();
    plan.speech_cues![0].word_ids = ['keyword'];
    plan.placements.push({ id: 'title', kind: 'subtitle', role: 'dialogue', track_id: 'sub', text: '咖啡',
      start: { kind: 'word', ref_id: 'keyword', edge: 'start' }, end: { kind: 'word', ref_id: 'keyword', edge: 'end' } });
    expect(() => applyAcceptedSpeechCues(plan, 'take', [{ id: 'cue', turn_id: 'turn', text: '黑咖啡？', start_us: 200_000, end_us: 800_000 }], 'srt_import', { artifact_id: 'new', revision: 1 })).toThrow(/词锚点/);
    expect(plan.placements[0].start).toEqual({ kind: 'word', ref_id: 'keyword', edge: 'start' });
    expect(plan.speech_cues![0].source_start_us).toBe(100_000);
  });
  it('uses exact corrected word times and links them to the accepted cue', () => {
    const plan = basePlan();
    applyCorrectedSpeechWords(plan, 'take', [
      { id: 'cue', turn_id: 'turn', text: '黑咖啡？', start_us: 100_000, end_us: 500_000 },
    ], [
      { text: '黑', start_us: 100_001, end_us: 200_333 },
      { text: '咖啡', start_us: 200_333, end_us: 499_999 },
    ], { artifact_id: 'corrected', revision: 1 }, { turn: 'hash' });
    const turn = plan.beats[0].turns[0];
    expect(turn.speech_timing?.words.map((word) => [word.text, word.start_us, word.end_us])).toEqual([
      ['黑', 100_001, 200_333], ['咖啡', 200_333, 499_999],
    ]);
    expect(plan.speech_cues?.[0].word_ids).toEqual(turn.speech_timing?.words.map((word) => word.id));
    expect(turn.speech_timing?.evidence_ref).toEqual({ artifact_id: 'corrected', revision: 1 });
  });

  it('blocks replacing a referenced word when the new correction cannot preserve its identity', () => {
    const plan = basePlan();
    plan.beats[0].turns[0].speech_timing = {
      placement_id: 'main', media_hash: 'b'.repeat(64), text: '黑咖啡？', text_hash: 'hash',
      evidence_ref: { artifact_id: 'old', revision: 1 }, words: [{ id: 'keyword', text: '黑咖啡', start_us: 100_000, end_us: 500_000 }],
    };
    plan.placements.push({ id: 'title', kind: 'subtitle', role: 'dialogue', track_id: 'sub', text: '黑咖啡',
      start: { kind: 'word', ref_id: 'keyword', edge: 'start' }, end: { kind: 'word', ref_id: 'keyword', edge: 'end' } });
    expect(() => applyCorrectedSpeechWords(plan, 'take', [
      { id: 'cue', turn_id: 'turn', text: '黑咖啡？', start_us: 100_000, end_us: 500_000 },
    ], [{ text: '黑', start_us: 100_000, end_us: 200_000 }, { text: '咖啡', start_us: 200_000, end_us: 500_000 }],
    { artifact_id: 'corrected', revision: 1 }, { turn: 'hash' })).toThrow(/词锚点/);
    expect(plan.beats[0].turns[0].speech_timing?.words[0].id).toBe('keyword');
  });
});
