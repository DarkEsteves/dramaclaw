import { describe, expect, it } from 'vitest';
import type { ProductionPlanV2 } from './productionPlanV2Model';
import { acceptRemoteDifference, planDifferences } from './productionPlanV2Conflict';

const plan = (): ProductionPlanV2 => ({ schema_version: 2, goal: '本地标题', output: { width: 1920, height: 1080, fps_num: 25, fps_den: 1 },
  beats: [{ id: 'a', purpose: '开头', duration_frames: 50, turns: [] }, { id: 'b', purpose: '结尾', duration_frames: 50, turns: [] }],
  placements: [{ id: 'music', kind: 'audio', role: 'bgm', track_id: 'audio:bgm', description: '本地音乐',
    start: { kind: 'program_frame', frame: 0 }, end: { kind: 'program_frame', frame: 100 } }], resources: [], provenance: {} });

describe('production plan conflict comparison', () => {
  it('uses stable IDs and accepts one remote field without replacing other local edits', () => {
    const local = plan(); const remote = plan();
    remote.goal = '远端标题'; remote.beats.reverse(); remote.placements[0].description = '远端音乐';
    const differences = planDifferences(local, remote);
    expect(differences.map((item) => item.label)).toEqual(['goal', 'placements[music].description']);
    const merged = acceptRemoteDifference(local, remote, differences[1].path);
    expect(merged.goal).toBe('本地标题');
    expect(merged.placements[0].description).toBe('远端音乐');
    expect(local.placements[0].description).toBe('本地音乐');
  });

  it('can remove or add a stable-ID item from the remote version', () => {
    const local = plan(); const remote = plan();
    remote.beats.pop();
    let diff = planDifferences(local, remote).find((item) => item.label === 'beats[b]');
    expect(diff).toBeTruthy();
    expect(acceptRemoteDifference(local, remote, diff!.path).beats.map((item) => item.id)).toEqual(['a']);
    remote.beats.push({ id: 'c', purpose: '补充', duration_frames: 50, turns: [] });
    diff = planDifferences(local, remote).find((item) => item.label === 'beats[c]');
    expect(acceptRemoteDifference(local, remote, diff!.path).beats.map((item) => item.id)).toEqual(['a', 'b', 'c']);
  });
});
