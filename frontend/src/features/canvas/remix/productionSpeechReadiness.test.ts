import { describe, expect, it } from 'vitest';
import type { ProductionPlanV2 } from './productionPlanV2Model';
import { inspectProductionSpeechReadiness, plannedSpeechSourcePlacementsForTurn } from './productionSpeechReadiness';

const hash = 'a'.repeat(64);
const planWithSpeech = (): ProductionPlanV2 => ({
  schema_version: 2, goal: '对话短片',
  output: { width: 1080, height: 1920, fps_num: 25, fps_den: 1 },
  beats: [{ id: 'beat', purpose: '提出问题', duration_frames: 100,
    turns: [{ id: 'turn', speaker: '主持人', text: '你好', timing_basis: 'estimated' }] }],
  resources: [{ id: 'voice', node_id: 'voice-node', media_kind: 'audio', media_path: 'media/voice.wav', media_hash: hash,
    duration_us: 5_000_000, has_audio: true }],
  placements: [{ id: 'speech', kind: 'audio', role: 'speech', track_id: 'audio:speech', resource_id: 'voice',
    start: { kind: 'program_frame', frame: 0 }, end: { kind: 'program_frame', frame: 100 } }],
  provenance: {},
});

describe('production speech readiness', () => {
  it('offers unbound visual and speech placements for planning, but keeps recognition unavailable', () => {
    const plan = planWithSpeech();
    plan.resources = [];
    plan.placements[0].resource_id = null;
    plan.placements.push({ id: 'picture', kind: 'visual', role: 'primary', track_id: 'visual:primary',
      start: { kind: 'beat', ref_id: 'beat', edge: 'start' }, end: { kind: 'beat', ref_id: 'beat', edge: 'end' } });
    expect(plannedSpeechSourcePlacementsForTurn(plan, 'turn').map((row) => row.id)).toEqual(['speech', 'picture']);
    expect(inspectProductionSpeechReadiness(plan, {}, {})).toEqual([]);
    plan.beats[0].turns[0].speech_placement_id = 'picture';
    expect(inspectProductionSpeechReadiness(plan, {}, {})).toEqual([expect.objectContaining({ reason: 'invalid_source' })]);
  });
  it('asks for a source when a bound speech track overlaps a turn', () => {
    expect(inspectProductionSpeechReadiness(planWithSpeech(), {}, {})).toEqual([
      expect.objectContaining({ turnId: 'turn', beatIndex: 0, turnIndex: 0, reason: 'select_source' }),
    ]);
  });

  it('asks for timing after the turn selects its spoken source', () => {
    const plan = planWithSpeech();
    plan.beats[0].turns[0].speech_placement_id = 'speech';
    expect(inspectProductionSpeechReadiness(plan, {}, {})).toEqual([
      expect.objectContaining({ turnId: 'turn', reason: 'calibrate' }),
    ]);
  });

  it('reuses full-media word timing across edit changes and invalidates changed text, media, or cut words', () => {
    const plan = planWithSpeech();
    const turn = plan.beats[0].turns[0];
    turn.speech_placement_id = 'speech';
    turn.speech_timing = { placement_id: 'speech', media_hash: hash, text: '你好', text_hash: 'b'.repeat(64),
      source_in_us: 0, source_out_us: 5_000_000, evidence_ref: { artifact_id: 'words', revision: 1 },
      words: [{ id: 'timed-word', text: '你好', start_char: 0, end_char: 2, start_us: 100_000, end_us: 500_000 }] };
    expect(inspectProductionSpeechReadiness(plan, {}, {})).toEqual([]);
    turn.text = '您好';
    expect(inspectProductionSpeechReadiness(plan, {}, {})).toHaveLength(1);
    turn.text = '你好';
    plan.resources[0].media_hash = 'c'.repeat(64);
    expect(inspectProductionSpeechReadiness(plan, {}, {})).toHaveLength(1);
    plan.resources[0].media_hash = hash;
    plan.placements[0].end.frame = 80;
    expect(inspectProductionSpeechReadiness(plan, {}, {})).toEqual([]);
    plan.placements[0].source_in_us = 200_000;
    expect(inspectProductionSpeechReadiness(plan, {}, {})).toHaveLength(1);
  });

  it('recognizes an overlong generated line once and becomes ready when its edit is extended', () => {
    const plan = planWithSpeech();
    const turn = plan.beats[0].turns[0];
    turn.speech_placement_id = 'speech';
    turn.speech_timing = { placement_id: 'speech', media_hash: hash, text: '你好', text_hash: 'b'.repeat(64),
      source_in_us: 0, source_out_us: 5_000_000, evidence_ref: { artifact_id: 'words', revision: 1 },
      words: [{ id: 'timed-word', text: '你好', start_char: 0, end_char: 2, start_us: 4_000_000, end_us: 4_500_000 }] };
    expect(inspectProductionSpeechReadiness(plan, {}, {})).toEqual([expect.objectContaining({ reason: 'calibrate' })]);
    plan.beats[0].duration_frames = 125;
    plan.placements[0].end.frame = 125;
    expect(inspectProductionSpeechReadiness(plan, {}, {})).toEqual([]);
  });

  it('lets a reviewed no-speech result continue with estimated timing', () => {
    const plan = planWithSpeech();
    const turn = plan.beats[0].turns[0];
    turn.speech_placement_id = 'speech';
    const review = { status: 'no_speech' as const, placementId: 'speech', mediaHash: hash,
      text: '你好', sourceInUs: 0, sourceOutUs: 5_000_000 };
    expect(inspectProductionSpeechReadiness(plan, { turn: review }, {})).toEqual([]);
    review.text = '您好';
    expect(inspectProductionSpeechReadiness(plan, { turn: review }, {})).toHaveLength(1);
  });

  it('keeps a no-voice work and an unbound voice placeholder eligible for direct compose', () => {
    const plan = planWithSpeech();
    plan.beats[0].turns = [];
    expect(inspectProductionSpeechReadiness(plan, {}, {})).toEqual([]);
    plan.beats[0].turns = [{ id: 'turn', speaker: '主持人', text: '你好' }];
    plan.placements[0].resource_id = null;
    expect(inspectProductionSpeechReadiness(plan, {}, {})).toEqual([]);
  });

  it('requires main video speech timing even when speaker names are empty', () => {
    const plan = planWithSpeech();
    plan.resources[0].media_kind = 'video';
    plan.placements[0].kind = 'visual';
    plan.placements[0].role = 'primary';
    expect(inspectProductionSpeechReadiness(plan, {}, {})).toEqual([
      expect.objectContaining({ turnId: 'turn', reason: 'select_source' }),
    ]);
    plan.beats[0].turns[0].speaker = '';
    expect(inspectProductionSpeechReadiness(plan, {}, {})).toEqual([
      expect.objectContaining({ turnId: 'turn', reason: 'select_source' }),
    ]);
    plan.beats[0].turns[0].speech_placement_id = 'speech';
    expect(inspectProductionSpeechReadiness(plan, {}, {})).toEqual([
      expect.objectContaining({ turnId: 'turn', reason: 'calibrate' }),
    ]);
  });

  it('allows explicit no-speech turns and independent subtitle placements to continue', () => {
    const plan = planWithSpeech();
    plan.beats[0].turns[0].speech_placement_id = null;
    expect(inspectProductionSpeechReadiness(plan, {}, {})).toEqual([]);
    plan.resources[0].media_kind = 'video';
    plan.placements[0].kind = 'visual';
    plan.placements[0].role = 'primary';
    expect(inspectProductionSpeechReadiness(plan, {}, {})).toEqual([]);
    plan.beats[0].turns = [];
    plan.placements.push({ id: 'caption', kind: 'subtitle', role: 'dialogue', track_id: 'subtitles', text: '字幕文案',
      start: { kind: 'beat', ref_id: 'beat', edge: 'start' }, end: { kind: 'beat', ref_id: 'beat', edge: 'end' } });
    expect(inspectProductionSpeechReadiness(plan, {}, {})).toEqual([]);
  });

  it('does not turn an overlay video or muted main picture into an implicit speech source', () => {
    const plan = planWithSpeech();
    plan.resources[0].media_kind = 'video';
    plan.placements[0].kind = 'visual';
    plan.placements[0].role = 'overlay';
    expect(inspectProductionSpeechReadiness(plan, {}, {})).toEqual([]);
    plan.placements[0].role = 'primary';
    plan.placements[0].volume = 0;
    expect(inspectProductionSpeechReadiness(plan, {}, {})).toEqual([]);
    plan.placements[0].volume = 1;
    plan.resources[0].has_audio = false;
    expect(inspectProductionSpeechReadiness(plan, {}, {})).toEqual([]);
  });

  it('requires reselection when a turn points to another beat’s material', () => {
    const plan = planWithSpeech();
    plan.beats.push({ id: 'answer', purpose: '回答', duration_frames: 100,
      turns: [{ id: 'answer-turn', speaker: '嘉宾', text: '黑咖啡', speech_placement_id: 'speech' }] });
    expect(inspectProductionSpeechReadiness(plan, {}, {})).toContainEqual(
      expect.objectContaining({ turnId: 'answer-turn', beatIndex: 1, reason: 'select_source' }),
    );
  });

  it('accepts ready cues for one full take even after its display placement is split away', () => {
    const plan = planWithSpeech();
    plan.beats[0].turns = [
      { id: 'host', speaker: '主持人', text: '你喝什么？' },
      { id: 'guest', speaker: '嘉宾', text: '黑咖啡。' },
    ];
    plan.placements = [];
    plan.speech_takes = [{ id: 'take', beat_id: 'beat', resource_id: 'voice', turn_ids: ['host', 'guest'],
      origin_placement_id: 'old-main-video', source_in_us: 0, source_out_us: 5_000_000, speed_num: 1, speed_den: 1, lead_in_frames: 0 }];
    plan.speech_cues = [
      { id: 'host-cue', turn_id: 'host', take_id: 'take', text: '你喝什么？', source_start_us: 200_000, source_end_us: 1_500_000,
        timing_source: 'srt_import', timing_status: 'ready', word_ids: [], evidence_ref: { artifact_id: 'srt', revision: 1 } },
      { id: 'guest-cue', turn_id: 'guest', take_id: 'take', text: '黑咖啡。', source_start_us: 2_000_000, source_end_us: 3_800_000,
        timing_source: 'srt_import', timing_status: 'ready', word_ids: [], evidence_ref: { artifact_id: 'srt', revision: 1 } },
    ];
    expect(inspectProductionSpeechReadiness(plan, {}, {})).toEqual([]);
    plan.speech_cues[1].timing_status = 'stale';
    expect(inspectProductionSpeechReadiness(plan, {}, {})).toContainEqual(expect.objectContaining({ turnId: 'guest', reason: 'calibrate' }));
  });
});
