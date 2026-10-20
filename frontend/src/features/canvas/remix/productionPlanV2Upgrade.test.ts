import { describe, expect, it } from 'vitest';
import type { ProductionPlanV2 } from './productionPlanV2Model';
import { upgradeMigratedPlanAnchors } from './productionPlanV2Upgrade';

describe('migrated production plan anchors', () => {
  it('rebases only original beat-owned cues and leaves deliberate clock positions alone', () => {
    const plan: ProductionPlanV2 = {
      schema_version: 2, goal: '改编', output: { width: 1080, height: 1920, fps_num: 30, fps_den: 1 },
      beats: [{ id: 'first', purpose: '开场', duration_frames: 90, turns: [] }, { id: 'second', purpose: '回答', duration_frames: 60, turns: [] }],
      resources: [], provenance: { source_plan_ref: { artifact_id: 'legacy', revision: 1 } },
      placements: [
        { id: 'visual:first:picture', kind: 'visual', role: 'primary', track_id: 'primary', start: { kind: 'program_frame', frame: 0 }, end: { kind: 'program_frame', frame: 90 } },
        { id: 'graphic:second:badge', kind: 'graphic', role: 'effect', track_id: 'graphics', start: { kind: 'program_frame', frame: 105 }, end: { kind: 'program_frame', frame: 140 } },
        { id: 'hand-placed', kind: 'graphic', role: 'effect', track_id: 'graphics', start: { kind: 'program_frame', frame: 15 }, end: { kind: 'program_frame', frame: 30 } },
      ],
    };
    const upgraded = upgradeMigratedPlanAnchors(plan);
    expect(upgraded).not.toBe(plan);
    expect(upgraded.placements[0]).toMatchObject({ start: { kind: 'beat', ref_id: 'first', edge: 'start', offset_frames: 0 }, end: { kind: 'beat', ref_id: 'first', edge: 'end', offset_frames: 0 } });
    expect(upgraded.placements[1]).toMatchObject({ start: { kind: 'beat', ref_id: 'second', edge: 'start', offset_frames: 15 }, end: { kind: 'beat', ref_id: 'second', edge: 'end', offset_frames: -10 } });
    expect(upgraded.placements[2]).toEqual(plan.placements[2]);
    expect(upgradeMigratedPlanAnchors(upgraded)).toBe(upgraded);
  });
});

import { webcrypto } from 'node:crypto';
import { vi } from 'vitest';
import { upgradePlanSpeech } from './productionPlanV2Upgrade';
function legacySpeech(): ProductionPlanV2 {
 return { schema_version: 2, goal: '', output: { width: 1920, height: 1080, fps_num: 30, fps_den: 1 }, beats: [{ id: 'b', purpose: '', duration_frames: 90, turns: [{ id: 't', speaker: '甲', text: 'hello', speech_placement_id: 'v', speech_timing: { placement_id: 'v', media_hash: 'hash', text: 'hello', source_in_us: 0, source_out_us: 3000000, text_hash: '2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824', evidence_ref: { artifact_id: 'words', revision: 1 }, words: [{ id: 'w', text: 'hello', start_us: 500000, end_us: 1500000 }] } }] }], resources: [{ id: 'r', node_id: 'n', media_kind: 'video', media_path: 'v.mp4', media_hash: 'hash', duration_us: 3000000, has_audio: true }], placements: [{ id: 'v', kind: 'visual', role: 'primary', track_id: 'visual:primary', resource_id: 'r', start: { kind: 'beat', ref_id: 'b', edge: 'start' }, end: { kind: 'beat', ref_id: 'b', edge: 'end' } }], provenance: {} };
}
it('upgrades proven source timing with stable IDs and is idempotent', async () => {
 vi.stubGlobal('crypto', webcrypto);
 const old = legacySpeech(); old.beats[0].turns.push({ ...structuredClone(old.beats[0].turns[0]), id: 't2' });
 const upgraded = await upgradePlanSpeech(old, async () => true);
 expect(upgraded.speech_takes).toHaveLength(1);
 expect(upgraded.speech_takes![0]).toMatchObject({ id: 'take:b:v', turn_ids: ['t', 't2'] });
 expect(upgraded.speech_cues![0]).toMatchObject({ id: 'cue:t', timing_status: 'ready', source_start_us: 500000, source_end_us: 1500000, word_ids: ['w'] });
 expect(upgraded.placements).toHaveLength(1); expect(upgraded.placements[0].speech_take_id).toBe('take:b:v');
 expect(await upgradePlanSpeech(upgraded, async () => true)).toBe(upgraded);
 vi.unstubAllGlobals();
});
it('keeps unverifiable or hash-mismatched timing as a draft cue', async () => {
 vi.stubGlobal('crypto', webcrypto);
 for (const verified of [false, true]) {
  const old = legacySpeech(); if (verified) old.beats[0].turns[0].speech_timing!.text_hash = 'invalid';
  const upgraded = await upgradePlanSpeech(old, async () => verified);
  expect(upgraded.speech_cues![0]).toMatchObject({ timing_status: 'draft', source_start_us: null, word_ids: [], evidence_ref: null });
  expect(upgraded.beats[0].turns[0].speech_timing).toEqual(old.beats[0].turns[0].speech_timing);
 }
 vi.unstubAllGlobals();
});
it('does not guess playback for cross-beat or A/B/A source reuse', async () => {
 const old = legacySpeech(); old.beats.push({ ...structuredClone(old.beats[0]), id: 'other', turns: [{ ...structuredClone(old.beats[0].turns[0]), id: 't2' }] });
 expect(await upgradePlanSpeech(old, async () => true)).toBe(old);
 const interleaved = legacySpeech(); interleaved.beats[0].turns.push({ id: 'other', speaker: '', text: '', speech_placement_id: 'other-source' }, { ...structuredClone(interleaved.beats[0].turns[0]), id: 'last' });
 expect(await upgradePlanSpeech(interleaved, async () => true)).toBe(interleaved);
});

it('keeps truncated legacy recognition draft without losing old word evidence', async () => {
 vi.stubGlobal('crypto', webcrypto); const old = legacySpeech(); old.beats[0].turns[0].speech_timing!.source_out_us = 2900000;
 const upgraded = await upgradePlanSpeech(old, async () => true);
 expect(upgraded.speech_cues![0].timing_status).toBe('draft');
 expect(upgraded.beats[0].turns[0].speech_timing!.words).toHaveLength(1);
 vi.unstubAllGlobals();
});
