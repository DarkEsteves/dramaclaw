import { describe, expect, it } from 'vitest';
import type { ProductionPlanV2 } from './productionPlanV2Model';
import { applyProductionPlanHistory, productionPlanHistoryAlreadyApplied, productionPlanHistoryEntry } from './productionPlanEditHistory';

function plan(): ProductionPlanV2 {
  return { schema_version: 2, goal: '原目标', output: { width: 1920, height: 1080, fps_num: 25, fps_den: 1 },
    beats: [{ id: 'beat', purpose: '开场', duration_frames: 100, turns: [{ id: 'turn', speaker: '甲', text: '你好' }] }],
    resources: [], placements: [{ id: 'visual', kind: 'visual', role: 'primary', track_id: 'visual:primary', description: '原画面',
      start: { kind: 'beat', ref_id: 'beat', edge: 'start' }, end: { kind: 'beat', ref_id: 'beat', edge: 'end' } }], provenance: {} };
}

describe('scoped production plan edit history', () => {
  it('undoes only edited fields while preserving another actor’s later field edit', () => {
    const before = plan();
    const after = structuredClone(before);
    after.placements[0].description = '新的画面';
    const entry = productionPlanHistoryEntry(before, after);
    const current = structuredClone(after);
    current.beats[0].turns[0].text = '后来的台词';
    const undone = applyProductionPlanHistory(current, entry, 'undo');
    expect(undone.placements[0].description).toBe('原画面');
    expect(undone.beats[0].turns[0].text).toBe('后来的台词');
    expect(applyProductionPlanHistory(undone, entry, 'redo').placements[0].description).toBe('新的画面');
  });

  it('refuses undo when the same field changed after the recorded edit', () => {
    const before = plan();
    const after = structuredClone(before);
    after.goal = '第一次修改';
    const current = structuredClone(after);
    current.goal = '其他人随后修改';
    expect(() => applyProductionPlanHistory(current, productionPlanHistoryEntry(before, after), 'undo'))
      .toThrow(/已变化/);
  });

  it('removes only its own inserted placement and preserves another insertion', () => {
    const before = plan();
    const after = structuredClone(before);
    after.placements.push({ ...structuredClone(after.placements[0]), id: 'mine', description: '我的画面' });
    const current = structuredClone(after);
    current.placements.push({ ...structuredClone(after.placements[0]), id: 'theirs', description: '后来的画面' });
    const undone = applyProductionPlanHistory(current, productionPlanHistoryEntry(before, after), 'undo');
    expect(undone.placements.map((item) => item.id)).toEqual(['visual', 'theirs']);
  });

  it('does not mistake a deleted parent for a previously saved field deletion', () => {
    const before = plan();
    const after = structuredClone(before);
    delete after.placements[0].description;
    const remote = structuredClone(before);
    remote.placements = [];
    expect(productionPlanHistoryAlreadyApplied(remote, productionPlanHistoryEntry(before, after))).toBe(false);
  });
});
