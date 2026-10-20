import { describe, expect, it } from 'vitest';
import type { ProductionPlanV2 } from './productionPlanV2Model';
import { editPlanSpeechTakeSource, bindPlanPlacementResource, bindPlanSpeechSource, acceptPlanSpeechTimings, movePlanBeat, removePlanBeat, removePlanPlacement, removePlanTurn, setPlanPlacementRole, setPlanTurnText, countIndependentAudioSources } from './productionPlanV2Edits';

const plan = (): ProductionPlanV2 => ({
  schema_version: 2, goal: '双段短片', output: { width: 1080, height: 1920, fps_num: 25, fps_den: 1 },
  beats: [
    { id: 'first', purpose: '提问', duration_frames: 75, turns: [{ id: 'question', speaker: '主持人', text: '为什么？', speech_placement_id: 'voice', words: [{ id: 'why', text: '为什么', start_frame: 0, end_frame: 30 }] }] },
    { id: 'second', purpose: '回答', duration_frames: 100, turns: [{ id: 'answer', speaker: '嘉宾', text: '因为好喝。' }] },
  ],
  selections: [{ id: 'opening', label: '开场', start: { kind: 'beat', ref_id: 'first', edge: 'start' }, end: { kind: 'word', ref_id: 'why', edge: 'end' } }],
  resources: [],
  placements: [
    { id: 'voice', kind: 'audio', role: 'speech', track_id: 'audio:speech', start: { kind: 'beat', ref_id: 'first', edge: 'start' }, end: { kind: 'beat', ref_id: 'first', edge: 'end' } },
    { id: 'caption', kind: 'subtitle', role: 'dialogue', track_id: 'subtitles', turn_id: 'question', start: { kind: 'turn', ref_id: 'question', edge: 'start' }, end: { kind: 'turn', ref_id: 'question', edge: 'end' } },
    { id: 'highlight', kind: 'graphic', role: 'effect', track_id: 'graphic:effect', graphic: { template: 'keyword', text: '为什么' }, start: { kind: 'selection', ref_id: 'opening', edge: 'start' }, end: { kind: 'selection', ref_id: 'opening', edge: 'end' } },
    { id: 'answer-visual', kind: 'visual', role: 'primary', track_id: 'visual:primary', start: { kind: 'beat', ref_id: 'second', edge: 'start' }, end: { kind: 'beat', ref_id: 'second', edge: 'end' } },
  ], provenance: {},
});

describe('production plan v2 edits', () => {
  it('counts each independent audio resource once while retaining separate unbound requests', () => {
    const draft = plan();
    draft.placements.push({ ...structuredClone(draft.placements[0]), id: 'voice-reuse' });
    draft.placements.push({ ...structuredClone(draft.placements[0]), id: 'music', role: 'bgm' });
    draft.placements.push({ ...structuredClone(draft.placements[0]), id: 'music-reuse', role: 'bgm' });
    draft.placements[0].resource_id = 'shared';
    draft.placements[4].resource_id = 'shared';
    expect(countIndependentAudioSources(draft)).toEqual({ ready: 1, total: 3 });
    draft.placements[5].resource_id = 'shared';
    expect(countIndependentAudioSources(draft)).toEqual({ ready: 1, total: 2 });
  });
  it('moves a beat while preserving stable references', () => {
    const draft = plan();
    movePlanBeat(draft, 1, -1);
    expect(draft.beats.map((beat) => beat.id)).toEqual(['second', 'first']);
    expect(draft.placements.find((placement) => placement.id === 'voice')?.start.ref_id).toBe('first');
  });

  it('deletes a beat and only its dependent anchors and placements', () => {
    const draft = plan();
    removePlanBeat(draft, 'first');
    expect(draft.beats.map((beat) => beat.id)).toEqual(['second']);
    expect(draft.selections).toEqual([]);
    expect(draft.placements.map((placement) => placement.id)).toEqual(['answer-visual']);
  });

  it('deletes a turn and dependent subtitle while keeping the beat visual', () => {
    const draft = plan();
    removePlanTurn(draft, 'question');
    expect(draft.beats[0].turns).toEqual([]);
    expect(draft.placements.map((placement) => placement.id)).toEqual(['voice', 'answer-visual']);
  });

  it('unlinks calibrated speech when its source placement is removed', () => {
    const draft = plan();
    draft.beats[0].turns[0].speech_timing = { placement_id: 'voice', media_hash: 'hash', text: '为什么？', text_hash: 'text-hash', evidence_ref: { artifact_id: 'words', revision: 1 }, words: [{ id: 'source-word', text: '为什么', start_us: 0, end_us: 1_000_000 }] };
    draft.selections!.push({ id: 'spoken', label: '口播', start: { kind: 'word', ref_id: 'source-word', edge: 'start' }, end: { kind: 'word', ref_id: 'source-word', edge: 'end' } });
    removePlanPlacement(draft, 'voice');
    expect(draft.beats[0].turns[0].speech_placement_id).toBeNull();
    expect(draft.beats[0].turns[0].speech_timing).toBeNull();
    expect(draft.placements.map((placement) => placement.id)).not.toContain('voice');
    expect(draft.selections).toEqual(expect.arrayContaining([expect.objectContaining({ id: 'spoken', start: { kind: 'turn', ref_id: 'question', edge: 'start' } })]));
  });

  it('protects dependent word anchors before editing calibrated dialogue', () => {
    const draft = plan();
    draft.beats[0].turns[0].timing_basis = 'aligned';
    draft.beats[0].turns[0].start_frame = 0;
    draft.beats[0].turns[0].end_frame = 30;
    draft.beats[0].turns[0].speech_timing = { placement_id: 'voice', media_hash: 'hash', text: '为什么？', text_hash: 'text-hash', evidence_ref: { artifact_id: 'words', revision: 1 }, words: [{ id: 'source-word', text: '为什么', start_us: 0, end_us: 1_000_000 }] };
    draft.selections!.push({ id: 'spoken', label: '口播', start: { kind: 'word', ref_id: 'source-word', edge: 'start' }, end: { kind: 'word', ref_id: 'source-word', edge: 'end' } });
    expect(() => setPlanTurnText(draft, 'question', '怎么回事？')).toThrow(/词锚点/);
    expect(draft.beats[0].turns[0]).toMatchObject({ text: '为什么？', timing_basis: 'aligned' });
    expect(draft.selections!.find((row) => row.id === 'spoken')?.start).toEqual({ kind: 'word', ref_id: 'source-word', edge: 'start' });
    expect(draft.placements.map((placement) => placement.id)).toEqual(['voice', 'caption', 'highlight', 'answer-visual']);
  });

  it('keeps speech evidence for narration but clears it when changed to music', () => {
    const draft = plan();
    draft.beats[0].turns[0].speech_timing = { placement_id: 'voice', media_hash: 'hash', text: '为什么？', text_hash: 'text-hash', evidence_ref: { artifact_id: 'words', revision: 1 }, words: [{ id: 'source-word', text: '为什么', start_us: 0, end_us: 1_000_000 }] };
    setPlanPlacementRole(draft, 'voice', 'narration');
    expect(draft.beats[0].turns[0].speech_timing?.words).toHaveLength(1);
    setPlanPlacementRole(draft, 'voice', 'bgm');
    expect(draft.beats[0].turns[0].speech_timing).toBeNull();
    expect(draft.beats[0].turns[0].speech_placement_id).toBeNull();
    expect(draft.placements.find((placement) => placement.id === 'voice')?.role).toBe('bgm');
  });
});

 it('creates one source take and keeps the stable cue id after recognition', () => {
 const draft = plan(); draft.placements[0].resource_id = 'r';
 const timing = { placement_id: 'voice', media_hash: 'hash', text: '为什么？', text_hash: 'text', evidence_ref: { artifact_id: 'words', revision: 1 }, words: [{ id: 'w', text: '为什么', start_us: 100000, end_us: 500000 }] };
 acceptPlanSpeechTimings(draft, { question: timing });
 const id = draft.speech_cues![0].id;
 acceptPlanSpeechTimings(draft, { question: { ...timing, words: [{ ...timing.words[0], end_us: 600000 }] } });
 expect(draft.speech_takes).toHaveLength(1); expect(draft.speech_cues).toHaveLength(1);
 expect(draft.speech_cues![0]).toMatchObject({ id, source_end_us: 600000 });
 draft.selections = [];
 setPlanTurnText(draft, 'question', '新台词');
 expect(draft.speech_cues![0]).toMatchObject({ timing_status: 'stale', word_ids: [] });
 });

it('keeps take backed speech evidence when its display is removed or changes role', () => {
 const draft = plan(); draft.placements[0].resource_id = 'r';
 const timing = { placement_id: 'voice', media_hash: 'hash', text: '为什么？', text_hash: 'text', evidence_ref: { artifact_id: 'words', revision: 1 }, words: [{ id: 'w', text: '为什么', start_us: 100000, end_us: 500000 }] };
 acceptPlanSpeechTimings(draft, { question: timing });
 draft.placements[1].start = { kind: 'word', ref_id: 'w', edge: 'start' };
 setPlanPlacementRole(draft, 'voice', 'bgm');
 expect(draft.beats[0].turns[0].speech_timing).toEqual(timing);
 removePlanPlacement(draft, 'voice');
 expect(draft.beats[0].turns[0].speech_timing).toEqual(timing);
 expect(draft.placements.find((item) => item.id === 'caption')?.start).toMatchObject({ kind: 'word', ref_id: 'w' });
 expect(draft.speech_cues![0].word_ids).toEqual(['w']);
});

it('marks an ASR cue for review when a referenced word cannot be matched on recalibration', () => {
 const draft = plan(); draft.placements[0].resource_id = 'r';
 const timing = { placement_id: 'voice', media_hash: 'hash', text: '为什么？', text_hash: 'text',
   evidence_ref: { artifact_id: 'words', revision: 1 }, words: [{ id: 'keyword', text: '为什么', start_us: 100000, end_us: 500000 }] };
 acceptPlanSpeechTimings(draft, { question: timing });
 acceptPlanSpeechTimings(draft, { question: { ...timing, evidence_ref: { artifact_id: 'new-words', revision: 1 },
   words: [{ id: 'different-word', text: '为', start_us: 150000, end_us: 250000 }] } });
 expect(draft.speech_cues![0].timing_status).toBe('review');
 expect(draft.speech_cues![0].word_ids).toEqual(['different-word']);
});

it('unbinds a take without leaving orphan cues or consuming its old duration', () => {
 const draft = plan(); draft.placements[0].resource_id = 'r';
 acceptPlanSpeechTimings(draft, { question: { placement_id: 'voice', media_hash: 'hash', text: '为什么？', text_hash: 't', evidence_ref: { artifact_id: 'e', revision: 1 }, words: [{ id: 'w', text: '为什么', start_us: 1, end_us: 500 }] } });
 draft.placements[1].start = { kind: 'cue', ref_id: draft.speech_cues![0].id, edge: 'start' };
 bindPlanSpeechSource(draft, 'question', null);
 expect(draft.speech_takes).toEqual([]); expect(draft.speech_cues).toEqual([]);
 expect(draft.placements[1].start).toMatchObject({ kind: 'turn', ref_id: 'question' });
});
it('records an explicit no-speech choice for a turn that never selected a source', () => {
 const draft = plan();
 bindPlanSpeechSource(draft, 'answer', null);
 expect(draft.beats[1].turns[0].speech_placement_id).toBeNull();
});
it('invalidates accepted ASR cues when recognition returns no evidence', () => {
 const draft = plan(); draft.placements[0].resource_id = 'r';
 acceptPlanSpeechTimings(draft, { question: { placement_id: 'voice', media_hash: 'hash', text: '为什么？', text_hash: 't', evidence_ref: { artifact_id: 'e', revision: 1 }, words: [{ id: 'w', text: '为什么', start_us: 1, end_us: 500 }] } });
 acceptPlanSpeechTimings(draft, { question: null });
 expect(draft.speech_cues![0]).toMatchObject({ timing_status: 'review', word_ids: [] });
 expect(draft.beats[0].turns[0].speech_timing).toBeNull();
});

it('binding a speech origin replaces the authoritative take source and invalidates all evidence', () => {
 const draft = plan(); draft.resources = [{ id: 'old', node_id: 'old', media_kind: 'video', media_path: 'old.mp4', media_hash: 'oldhash', duration_us: 3000000, has_audio: true }, { id: 'new', node_id: 'new', media_kind: 'video', media_path: 'new.mp4', media_hash: 'newhash', duration_us: 2000000, has_audio: true }]; draft.placements[0].resource_id = 'old';
 acceptPlanSpeechTimings(draft, { question: { placement_id: 'voice', media_hash: 'oldhash', text: '为什么？', text_hash: 'text', evidence_ref: { artifact_id: 'e', revision: 1 }, words: [{ id: 'w', text: '为什么', start_us: 100000, end_us: 500000 }] } });
 const take = draft.speech_takes![0];
 draft.placements.push({ id: 'split', kind: 'visual', role: 'primary', track_id: 'visual:primary', speech_take_id: take.id, resource_id: 'old', start: { kind: 'take', ref_id: take.id, edge: 'start' }, end: { kind: 'take', ref_id: take.id, edge: 'end' } });
 draft.placements[1].start = { kind: 'word', ref_id: 'w', edge: 'start' };
 bindPlanPlacementResource(draft, 'voice', 'new');
 expect(take.resource_id).toBe('new'); expect(draft.placements.find((row) => row.id === 'split')!.resource_id).toBe('new');
 expect(draft.beats[0].turns[0].speech_timing).toBeNull(); expect(draft.beats[0].turns[0].words).toEqual([]);
 expect(draft.speech_cues![0]).toMatchObject({ timing_status: 'stale', word_ids: [], evidence_ref: null, source_start_us: null });
 expect(draft.placements[1].start).toMatchObject({ kind: 'turn', ref_id: 'question' });
});
it('binding the same media hash preserves accepted evidence', () => {
 const draft = plan(); draft.resources = [{ id: 'old', node_id: 'old', media_kind: 'audio', media_path: 'old.wav', media_hash: 'hash', duration_us: 3000000, has_audio: true }, { id: 'new', node_id: 'new', media_kind: 'audio', media_path: 'new.wav', media_hash: 'hash', duration_us: 3000000, has_audio: true }]; draft.placements[0].resource_id = 'old';
 const timing = { placement_id: 'voice', media_hash: 'hash', text: '为什么？', text_hash: 'text', evidence_ref: { artifact_id: 'e', revision: 1 }, words: [{ id: 'w', text: '为什么', start_us: 100000, end_us: 500000 }] };
 acceptPlanSpeechTimings(draft, { question: timing }); bindPlanPlacementResource(draft, 'voice', 'new');
 expect(draft.speech_takes![0].resource_id).toBe('new'); expect(draft.beats[0].turns[0].speech_timing).toEqual(timing); expect(draft.speech_cues![0].timing_status).toBe('ready');
});

it('editing take source invalidates evidence and reanchors consumers with their offsets', () => {
 const draft = plan(); draft.placements[0].resource_id = 'r';
 acceptPlanSpeechTimings(draft, { question: { placement_id: 'voice', media_hash: 'hash', text: '为什么？', text_hash: 'text', evidence_ref: { artifact_id: 'e', revision: 1 }, words: [{ id: 'w', text: '为什么', start_us: 100000, end_us: 500000 }] } });
 const take = draft.speech_takes![0]; draft.placements[1].start = { kind: 'word', ref_id: 'w', edge: 'start', offset_frames: 3 };
 draft.selections![0].end = { kind: 'word', ref_id: 'w', edge: 'end', offset_frames: -2 };
 expect(editPlanSpeechTakeSource(draft, take.id, (source) => { source.speed_num = 2; })).toBe(true);
 expect(draft.placements[1].start).toMatchObject({ kind: 'turn', ref_id: 'question', edge: 'start', offset_frames: 3 });
 expect(draft.selections![0].end).toMatchObject({ kind: 'turn', ref_id: 'question', edge: 'end', offset_frames: -2 });
 expect(draft.beats[0].turns[0].speech_timing).toBeNull(); expect(draft.speech_cues![0]).toMatchObject({ timing_status: 'stale', word_ids: [], evidence_ref: null, source_start_us: null });
});
it('an unchanged source edit preserves accepted evidence', () => {
 const draft = plan(); draft.placements[0].resource_id = 'r'; const timing = { placement_id: 'voice', media_hash: 'hash', text: '为什么？', text_hash: 'text', evidence_ref: { artifact_id: 'e', revision: 1 }, words: [{ id: 'w', text: '为什么', start_us: 100000, end_us: 500000 }] };
 acceptPlanSpeechTimings(draft, { question: timing });
 expect(editPlanSpeechTakeSource(draft, draft.speech_takes![0].id, (source) => { source.speed_num = source.speed_num; })).toBe(false);
 expect(draft.beats[0].turns[0].speech_timing).toEqual(timing); expect(draft.speech_cues![0].timing_status).toBe('ready');
});
