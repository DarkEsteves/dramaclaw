import { describe, expect, it } from 'vitest';
import type { ProductionPlanV2 } from '@/features/canvas/remix/productionPlanV2Model';
import { canvasBindingIssues, movePlacement, projectProductionTimeline, resizePlacement, sourceRangeForPlacement, splitPlacement } from './productionComposeModel';

it('projects a silent beat from its selected source media instead of its estimate', () => {
  const silent: ProductionPlanV2 = {
    schema_version: 2, goal: '动作', output: { width: 1080, height: 1920, fps_num: 30, fps_den: 1 },
    beats: [{ id: 'motion', purpose: '动作', duration_frames: 150, turns: [], duration_source_placement_id: 'clip' },
      { id: 'title', purpose: '片尾', duration_frames: 60, timing_mode: 'fixed', turns: [] }],
    resources: [{ id: 'video', node_id: 'node', media_kind: 'video', media_path: 'clip.mp4', media_hash: 'a'.repeat(64), duration_us: 7_200_000, has_audio: false }],
    placements: [{ id: 'clip', kind: 'visual', role: 'primary', track_id: 'visual:primary', resource_id: 'video',
      start: { kind: 'beat', ref_id: 'motion', edge: 'start' }, end: { kind: 'beat', ref_id: 'motion', edge: 'end' } }],
    provenance: {},
  };
  expect(projectProductionTimeline(silent).beats.map((beat) => [beat.startFrame, beat.endFrame])).toEqual([[0, 216], [216, 276]]);
});

it('projects authored text boundaries from adopted word evidence', () => {
  const text = '甲乙';
  const anchored: ProductionPlanV2 = {
    schema_version: 2, goal: '对话', output: { width: 1080, height: 1920, fps_num: 30, fps_den: 1 },
    beats: [{ id: 'b', purpose: '停顿', duration_frames: 30, turns: [{ id: 't', speaker: '甲', text,
      speech_placement_id: 'voice', speech_timing: { placement_id: 'voice', media_hash: 'a'.repeat(64), text,
        text_hash: 'text-hash', evidence_ref: { artifact_id: 'speech', revision: 1 }, words: [
          { id: 'w1', text: '甲', start_char: 0, end_char: 1, start_us: 100_000, end_us: 300_000 },
          { id: 'w2', text: '乙', start_char: 1, end_char: 2, start_us: 600_000, end_us: 900_000 },
        ] } }] }],
    selections: [{ id: 'pause', label: '停顿', start: { kind: 'text', ref_id: 't', char_offset: 1, affinity: 'before', text_hash: 'text-hash' },
      end: { kind: 'text', ref_id: 't', char_offset: 1, affinity: 'after', text_hash: 'text-hash' } }],
    resources: [{ id: 'media', node_id: 'n', media_kind: 'audio', media_path: 'voice.wav', media_hash: 'a'.repeat(64), duration_us: 1_000_000, has_audio: true }],
    placements: [{ id: 'voice', kind: 'audio', role: 'speech', track_id: 'speech', resource_id: 'media',
      start: { kind: 'beat', ref_id: 'b', edge: 'start' }, end: { kind: 'beat', ref_id: 'b', edge: 'end' } }],
    provenance: {},
  };
  expect(projectProductionTimeline(anchored).selections[0]).toMatchObject({ startFrame: 9, endFrame: 18, issue: null });
});

const plan: ProductionPlanV2 = {
  schema_version: 2, goal: '故事', output: { width: 1920, height: 1080, fps_num: 25, fps_den: 1 },
  beats: [{ id: 'beat', purpose: '开场', duration_frames: 150, turns: [{ id: 'turn', speaker: 'A', text: '你好', start_frame: 20, end_frame: 80, timing_basis: 'manual' }] }],
  resources: [], placements: [
    { id: 'subtitle', kind: 'subtitle', role: 'dialogue', track_id: 'subtitles', turn_id: 'turn', start: { kind: 'turn', ref_id: 'turn', edge: 'start' }, end: { kind: 'turn', ref_id: 'turn', edge: 'end' } },
    { id: 'overlay', kind: 'visual', role: 'overlay', track_id: 'overlay', start: { kind: 'placement', ref_id: 'subtitle', edge: 'start', offset_frames: 5 }, end: { kind: 'beat', ref_id: 'beat', edge: 'end', offset_frames: -10 } },
  ], provenance: {},
};

describe('production composition model', () => {
  it('uses the full speaking take to reflow following beats and keeps word effects on the spoken word', () => {
    const spoken: ProductionPlanV2 = {
      schema_version: 2, goal: '按台词编排', output: { width: 1080, height: 1920, fps_num: 30, fps_den: 1 },
      beats: [
        { id: 'spoken', purpose: '开场', duration_frames: 90, turns: [{ id: 'line', speaker: '嘉宾', text: '黑咖啡', speech_placement_id: 'talk',
          speech_timing: { placement_id: 'talk', media_hash: 'a'.repeat(64), text: '黑咖啡', text_hash: 'c955791e4b09ef1f46bb01f27684db82e7e487c7ede0b0f28776ad931b535fa1', source_in_us: 0, source_out_us: 4_096_000,
            evidence_ref: { artifact_id: 'words', revision: 1 }, words: [{ id: 'coffee', text: '黑咖啡', start_us: 2_900_000, end_us: 3_560_000 }] } }] },
        { id: 'next', purpose: '下一段', duration_frames: 60, turns: [] },
      ],
      resources: [{ id: 'video', node_id: 'talk-node', media_kind: 'video', media_path: 'media/talk.mp4', media_hash: 'a'.repeat(64), duration_us: 4_096_000, has_audio: true }],
      selections: [{ id: 'word-range', label: '关键词', start: { kind: 'word', ref_id: 'coffee', edge: 'start' }, end: { kind: 'word', ref_id: 'coffee', edge: 'end' } }],
      placements: [
        { id: 'talk', kind: 'visual', role: 'primary', track_id: 'primary', resource_id: 'video', start: { kind: 'beat', ref_id: 'spoken', edge: 'start' }, end: { kind: 'beat', ref_id: 'spoken', edge: 'end' } },
        { id: 'later', kind: 'graphic', role: 'effect', track_id: 'graphics', start: { kind: 'beat', ref_id: 'next', edge: 'start' }, end: { kind: 'beat', ref_id: 'next', edge: 'end' } },
        { id: 'word-effect', kind: 'graphic', role: 'effect', track_id: 'graphics', start: { kind: 'selection', ref_id: 'word-range', edge: 'start' }, end: { kind: 'selection', ref_id: 'word-range', edge: 'end' } },
        { id: 'clock-locked', kind: 'graphic', role: 'effect', track_id: 'graphics', start: { kind: 'program_frame', frame: 15 }, end: { kind: 'program_frame', frame: 30 } },
      ], provenance: {},
    };
    const first = projectProductionTimeline(spoken);
    expect(first.durationFrames).toBe(183);
    expect(first.beats[1].startFrame).toBe(123);
    expect(first.placements.find((item) => item.id === 'talk')).toMatchObject({ startFrame: 0, endFrame: 123, issue: null });
    expect(first.selections[0]).toMatchObject({ startFrame: 87, endFrame: 107, issue: null });
    expect(first.placements.find((item) => item.id === 'word-effect')).toMatchObject({ startFrame: 87, endFrame: 107, issue: null });
    expect(sourceRangeForPlacement(spoken, 'talk')).toEqual({ sourceInUs: 0, sourceOutUs: 4_096_000 });
    const oldSaved = structuredClone(spoken);
    oldSaved.provenance.source_plan_ref = { artifact_id: 'old-plan', revision: 1 };
    oldSaved.placements[0].id = 'visual:spoken:talk';
    oldSaved.placements[0].start = { kind: 'program_frame', frame: 0 };
    oldSaved.placements[0].end = { kind: 'program_frame', frame: 90 };
    oldSaved.beats[0].turns[0].speech_placement_id = 'visual:spoken:talk';
    oldSaved.beats[0].turns[0].speech_timing!.placement_id = 'visual:spoken:talk';
    oldSaved.placements[1].id = 'graphic:next:later';
    oldSaved.placements[1].start = { kind: 'program_frame', frame: 90 };
    oldSaved.placements[1].end = { kind: 'program_frame', frame: 150 };
    expect(projectProductionTimeline(oldSaved).placements.find((item) => item.id === 'graphic:next:later')?.startFrame).toBe(123);
    spoken.resources[0].duration_us = 5_000_000;
    spoken.beats[0].turns[0].speech_timing!.source_out_us = 5_000_000;
    const longer = projectProductionTimeline(spoken);
    expect(longer.beats[1].startFrame).toBe(150);
    expect(longer.placements.find((item) => item.id === 'later')?.startFrame).toBe(150);
    expect(longer.placements.find((item) => item.id === 'clock-locked')?.startFrame).toBe(15);
    const delayed = movePlacement(spoken, 'talk', 15);
    expect(delayed.placements[0].start).toMatchObject({ kind: 'beat', ref_id: 'spoken', offset_frames: 15 });
    expect(delayed.placements[0].end).toMatchObject({ kind: 'beat', ref_id: 'spoken' });
    expect(delayed.placements[0].end.offset_frames ?? 0).toBe(0);
    expect(projectProductionTimeline(delayed).beats[1].startFrame).toBe(165);
    expect(projectProductionTimeline(delayed).selections[0].startFrame).toBe(102);
    const trimmed = resizePlacement(delayed, 'talk', 'end', -30);
    expect(trimmed.placements[0].source_out_us).toBe(4_000_000);
    expect(projectProductionTimeline(trimmed).beats[1].startFrame).toBe(135);
  });
  it('lays out two speaking takes in order and leaves fixed beats on their authored clock', () => {
    const spoken: ProductionPlanV2 = {
      schema_version: 2, goal: '双人对话', output: { width: 1280, height: 720, fps_num: 30, fps_den: 1 },
      beats: [
        { id: 'exchange', purpose: '问答', duration_frames: 180, turns: [
          { id: 'ask', speaker: '甲', text: '问题', start_frame: 0, end_frame: 90, speech_placement_id: 'ask-audio',
            speech_timing: { placement_id: 'ask-audio', media_hash: 'a'.repeat(64), text: '问题', text_hash: '33a8618cb6710e43488a4b7ed2406bf0ec1f80d10093863798a3ca5eb8a9d6c6', evidence_ref: { artifact_id: 'ask-words', revision: 1 },
              words: [{ id: 'ask-word', text: '问题', start_us: 500_000, end_us: 1_000_000 }] } },
          { id: 'answer', speaker: '乙', text: '回答', start_frame: 90, end_frame: 180, speech_placement_id: 'answer-audio',
            speech_timing: { placement_id: 'answer-audio', media_hash: 'b'.repeat(64), text: '回答', text_hash: 'f3f512648742020fbd700fa8f0576babd8f7ec8e56d3dcd38e28be5b99287db3', evidence_ref: { artifact_id: 'answer-words', revision: 1 },
              words: [{ id: 'answer-word', text: '回答', start_us: 500_000, end_us: 1_000_000 }] } },
        ] },
        { id: 'outro', purpose: '片尾', duration_frames: 30, turns: [] },
      ],
      resources: [
        { id: 'ask-media', node_id: 'ask-node', media_kind: 'audio', media_path: 'media/ask.wav', media_hash: 'a'.repeat(64), duration_us: 3_000_000, has_audio: true },
        { id: 'answer-media', node_id: 'answer-node', media_kind: 'audio', media_path: 'media/answer.wav', media_hash: 'b'.repeat(64), duration_us: 4_000_000, has_audio: true },
      ],
      placements: [
        { id: 'ask-audio', kind: 'audio', role: 'speech', track_id: 'speech', resource_id: 'ask-media',
          start: { kind: 'turn', ref_id: 'ask', edge: 'start' }, end: { kind: 'turn', ref_id: 'ask', edge: 'end' } },
        { id: 'answer-audio', kind: 'audio', role: 'speech', track_id: 'speech', resource_id: 'answer-media',
          start: { kind: 'turn', ref_id: 'answer', edge: 'start' }, end: { kind: 'turn', ref_id: 'answer', edge: 'end' } },
      ], provenance: {},
    };
    const flowing = projectProductionTimeline(spoken);
    expect(flowing.durationFrames).toBe(240);
    expect(flowing.placements.map((item) => [item.startFrame, item.endFrame])).toEqual([[0, 90], [90, 210]]);
    spoken.placements[1].start = { kind: 'beat', ref_id: 'exchange', edge: 'start', offset_frames: 90 };
    spoken.placements[1].end = { kind: 'beat', ref_id: 'exchange', edge: 'end' };
    const beatRelative = projectProductionTimeline(spoken);
    expect(beatRelative.durationFrames).toBe(240);
    expect(beatRelative.placements.map((item) => [item.startFrame, item.endFrame])).toEqual([[0, 90], [90, 210]]);
    spoken.beats[0].timing_mode = 'fixed';
    const fixed = projectProductionTimeline(spoken);
    expect(fixed.durationFrames).toBe(210);
    expect(fixed.placements.map((item) => [item.startFrame, item.endFrame])).toEqual([[0, 90], [90, 180]]);
  });
  it('projects semantic and placement anchors on a shared frame timeline', () => {
    const view = projectProductionTimeline(plan);
    expect(view.durationFrames).toBe(150);
    expect(view.placements.find((item) => item.id === 'subtitle')).toMatchObject({ startFrame: 20, endFrame: 80 });
    expect(view.placements.find((item) => item.id === 'overlay')).toMatchObject({ startFrame: 25, endFrame: 140 });
  });

  it('shares one named word range across visual and audio placements', () => {
    const ranged: ProductionPlanV2 = { ...plan,
      beats: [{ ...plan.beats[0], turns: [{ ...plan.beats[0].turns[0], words: [
        { id: 'first', text: '你', start_frame: 30, end_frame: 40 },
        { id: 'last', text: '好', start_frame: 40, end_frame: 55 },
      ] }] }],
      selections: [{ id: 'greeting', label: '问候', start: { kind: 'word', ref_id: 'first', edge: 'start' }, end: { kind: 'word', ref_id: 'last', edge: 'end' } }],
      placements: ['visual', 'audio'].map((kind) => ({ id: kind, kind: kind as 'visual' | 'audio', role: kind === 'visual' ? 'overlay' : 'sfx', track_id: kind,
        start: { kind: 'selection', ref_id: 'greeting', edge: 'start' }, end: { kind: 'selection', ref_id: 'greeting', edge: 'end' } })),
    };
    const view = projectProductionTimeline(ranged);
    expect(view.selections).toMatchObject([{ id: 'greeting', startFrame: 30, endFrame: 55, issue: null }]);
    expect(view.placements.map((item) => [item.startFrame, item.endFrame])).toEqual([[30, 55], [30, 55]]);
    expect(view.words).toHaveLength(2);
  });

  it('moves source aligned words with their spoken media and invalidates changed text', () => {
    const spoken: ProductionPlanV2 = { ...plan,
      resources: [{ id: 'voice', node_id: 'voice-node', media_kind: 'audio', media_path: 'media/voice.wav', media_hash: 'a'.repeat(64), duration_us: 6_000_000, has_audio: true }],
      beats: [{ ...plan.beats[0], turns: [{ ...plan.beats[0].turns[0], speech_timing: { placement_id: 'speech', media_hash: 'a'.repeat(64), text: '你好', text_hash: 'b'.repeat(64), source_in_us: 0, source_out_us: 6_000_000, evidence_ref: { artifact_id: 'words', revision: 1 }, words: [{ id: 'hello', text: '你好', start_us: 1_000_000, end_us: 1_400_000 }] } }] }],
      placements: [{ id: 'speech', kind: 'audio', role: 'speech', track_id: 'audio:speech', resource_id: 'voice', start: { kind: 'program_frame', frame: 10 }, end: { kind: 'program_frame', frame: 110 } }],
      selections: [{ id: 'hello-range', label: '你好时', start: { kind: 'word', ref_id: 'hello', edge: 'start' }, end: { kind: 'word', ref_id: 'hello', edge: 'end' } }],
    };
    expect(projectProductionTimeline(spoken).selections[0]).toMatchObject({ startFrame: 35, endFrame: 45, issue: null });
    expect(projectProductionTimeline(spoken).beats[0].turns[0]).toMatchObject({ startFrame: 35, endFrame: 45, estimated: false });
    spoken.placements[0].start.frame = 20;
    spoken.placements[0].end.frame = 120;
    expect(projectProductionTimeline(spoken).selections[0]).toMatchObject({ startFrame: 45, endFrame: 55, issue: null });
    spoken.beats[0].turns[0].text = '改了台词';
    expect(projectProductionTimeline(spoken).selections[0].issue).toMatch(/不存在|语音/);
  });

  it('reports an invalid named range before export', () => {
    const invalid: ProductionPlanV2 = { ...plan, selections: [{ id: 'bad', label: '无效范围', start: { kind: 'program_frame', frame: 90 }, end: { kind: 'program_frame', frame: 50 } }] };
    expect(projectProductionTimeline(invalid).selections[0].issue).toMatch(/结束/);
  });

  it('moves anchored clips by changing offsets and keeps their references', () => {
    const moved = movePlacement(plan, 'overlay', 10);
    const placement = moved.placements.find((item) => item.id === 'overlay')!;
    expect(placement.start).toMatchObject({ kind: 'placement', ref_id: 'subtitle', offset_frames: 15 });
    expect(placement.end).toMatchObject({ kind: 'beat', ref_id: 'beat', offset_frames: 0 });
  });

  it('splits a media clip into stable entries and maps source range', () => {
    const withMedia: ProductionPlanV2 = { ...plan, placements: [{ id: 'clip', kind: 'visual', role: 'primary', track_id: 'primary', start: { kind: 'program_frame', frame: 0 }, end: { kind: 'program_frame', frame: 100 }, source_in_us: 1_000_000, source_out_us: 5_000_000 }] };
    const split = splitPlacement(withMedia, 'clip', 50, 'new-clip');
    expect(split.placements).toHaveLength(2);
    expect(split.placements[0]).toMatchObject({ id: 'clip', source_in_us: 1_000_000, source_out_us: 3_000_000 });
    expect(split.placements[1]).toMatchObject({ id: 'new-clip', source_in_us: 3_000_000, source_out_us: 5_000_000 });
    expect(projectProductionTimeline(split).placements.map((item) => [item.startFrame, item.endFrame])).toEqual([[0, 50], [50, 100]]);
  });

  it('advances the source in point for an implicit range when splitting', () => {
    const withMedia: ProductionPlanV2 = { ...plan, placements: [{ id: 'clip', kind: 'visual', role: 'primary', track_id: 'primary', start: { kind: 'program_frame', frame: 0 }, end: { kind: 'program_frame', frame: 100 }, source_in_us: 1_000_000, speed_num: 2, speed_den: 1 }] };
    const split = splitPlacement(withMedia, 'clip', 50, 'new-clip');
    expect(split.placements[0].source_out_us).toBe(5_000_000);
    expect(split.placements[1].source_in_us).toBe(5_000_000);
  });

  it('reports broken anchor cycles without hanging the editor', () => {
    const cyclic: ProductionPlanV2 = { ...plan, placements: [{ id: 'loop', kind: 'subtitle', role: 'dialogue', track_id: 'subtitles', text: 'x', start: { kind: 'placement', ref_id: 'loop', edge: 'start' }, end: { kind: 'program_frame', frame: 100 } }] };
    expect(projectProductionTimeline(cyclic).placements[0].issue).toMatch(/循环/);
  });

  it('rejects a changed canvas source even while the old media path still exists', () => {
    const bound: ProductionPlanV2 = { ...plan, resources: [{ id: 'resource', node_id: 'video', media_kind: 'video', media_path: 'media/old.mp4', media_hash: 'a'.repeat(64), duration_us: 2_000_000, has_audio: true }], placements: [{ id: 'clip', kind: 'visual', role: 'primary', track_id: 'primary', resource_id: 'resource', start: { kind: 'program_frame', frame: 0 }, end: { kind: 'program_frame', frame: 25 } }] };
    expect(canvasBindingIssues(bound, [{ source: 'video', target: 'plan' }], 'plan', [{ node: { id: 'video' }, media: { media_hash: 'b'.repeat(64), media_path: 'media/new.mp4' } }])).toMatchObject([{ placement_id: 'clip', code: 'stale_canvas_media' }]);
  });
});

import mainlineFixture from '../../../../../tests/fixtures/production-srt-mainline.json';

describe('shared speech mainline', () => {
  it('projects unique complete takes and independent accepted cues', () => {
    const result = projectProductionTimeline(structuredClone(mainlineFixture) as ProductionPlanV2);
    expect(result.beats.map((beat) => [beat.startFrame, beat.endFrame])).toEqual([[0, 192], [192, 282], [282, 402]]);
    expect(result.speechCues.map((cue) => [cue.startFrame, cue.endFrame])).toEqual([[15, 123], [138, 174], [282, 387]]);
    expect(result.placements.find((item) => item.id === 'caption-host')).toMatchObject({ startFrame: 138, endFrame: 174, issue: null });
  });
  it('counts shared legacy placement once', () => {
    const legacy = structuredClone(mainlineFixture) as ProductionPlanV2;
    delete legacy.speech_takes;
    delete legacy.speech_cues;
    legacy.placements.pop();
    expect(projectProductionTimeline(legacy).beats.map((beat) => [beat.startFrame, beat.endFrame])).toEqual([[0, 192], [192, 282], [282, 402]]);
  });
  it('uses rational fps, playback speed, and explicit lead in identically', () => {
    const plan = structuredClone(mainlineFixture) as ProductionPlanV2;
    plan.output.fps_num = 24000; plan.output.fps_den = 1001;
    Object.assign(plan.speech_takes![0], { speed_num: 2, speed_den: 1, lead_in_frames: 7 });
    const result = projectProductionTimeline(plan);
    expect(result.beats[0].endFrame).toBe(84);
    expect(result.speechCues[1].startFrame).toBe(62);
  });
});

it('splits a speaking display into take consumers without restarting its source', () => {
  const plan = structuredClone(mainlineFixture) as ProductionPlanV2;
  const split = splitPlacement(plan, 'talk', 90, 'talk-second');
  expect(split.speech_takes).toEqual(plan.speech_takes);
  expect(split.placements.find((row) => row.id === 'talk-second')?.speech_take_id).toBe('take:first');
  expect(sourceRangeForPlacement(split, 'talk-second')).toEqual({ sourceInUs: 3_000_000, sourceOutUs: 6_400_000 });
  expect(projectProductionTimeline(split).placements.find((row) => row.id === 'talk')).toMatchObject({ startFrame: 0, endFrame: 90 });
});

it('keeps manual cue timing authoritative and hides words that have no accepted association', () => {
  const plan = structuredClone(mainlineFixture) as ProductionPlanV2;
  plan.speech_cues![1].source_start_us = 5_000_000;
  const projected = projectProductionTimeline(plan);
  expect(projected.beats[0].turns[1].startFrame).toBe(150);
  expect(projected.words).toHaveLength(0);
});

it('uses take owned sampling for an origin visual with no consumer field', () => {
  const plan = structuredClone(mainlineFixture) as ProductionPlanV2;
  Object.assign(plan.speech_takes![0], { source_in_us: 100_000, source_out_us: 6_000_000, speed_num: 2 });
  const visual = projectProductionTimeline(plan).placements.find((row) => row.id === 'talk');
  expect(visual).toMatchObject({ source_in_us: 100_000, sourceOutUs: 6_000_000, speed_num: 2 });
});

it('scopes semantic IDs by anchor kind', () => {
  const plan = structuredClone(mainlineFixture) as ProductionPlanV2;
  plan.speech_cues![1].id = 'first';
  plan.placements[plan.placements.length - 1].start.ref_id = 'first';
  plan.placements[plan.placements.length - 1].end.ref_id = 'first';
  expect(projectProductionTimeline(plan).placements[plan.placements.length - 1]).toMatchObject({ startFrame: 138, endFrame: 174 });
});

it('reports an unassigned spoken turn in an automatic take beat', () => {
  const plan = structuredClone(mainlineFixture) as ProductionPlanV2;
  plan.beats[0].turns.push({ id: 'new', speaker: 'guest', text: 'More' });
  expect(projectProductionTimeline(plan).issues.some((issue) => issue.code === 'unresolved_speech_timing')).toBe(true);
  plan.beats[0].timing_mode = 'fixed'; plan.beats[0].duration_frames = 192;
  expect(projectProductionTimeline(plan).issues).toEqual([]);
});

it('refuses to split an explicit take audio origin and preserves ordinary audio splitting', () => {
  const plan = structuredClone(mainlineFixture) as ProductionPlanV2;
  Object.assign(plan.placements[0], { kind: 'audio', role: 'speech', track_id: 'speech' });
  expect(splitPlacement(plan, 'talk', 90, 'split-audio')).toBe(plan);
  delete plan.speech_takes; delete plan.speech_cues; plan.placements.pop();
  expect(splitPlacement(plan, 'talk', 90, 'split-audio').placements.some((row) => row.id === 'split-audio')).toBe(true);
});

it('checks take canvas resources even after their visual origin is deleted', () => {
  const plan = structuredClone(mainlineFixture) as ProductionPlanV2;
  plan.placements = [];
  const issues = canvasBindingIssues(plan, [], 'plan', []);
  expect(issues.some((issue) => issue.resource_id === 'video' && issue.code === 'disconnected_canvas_media')).toBe(true);
  expect(issues.some((issue) => issue.resource_id === 'video' && issue.code === 'stale_canvas_media')).toBe(true);
});

it('projects take owned original audio volume', () => {
  const plan = structuredClone(mainlineFixture) as ProductionPlanV2;
  plan.speech_takes![0].volume = 0.3;
  expect(projectProductionTimeline(plan).placements[0].volume).toBe(0.3);
});
