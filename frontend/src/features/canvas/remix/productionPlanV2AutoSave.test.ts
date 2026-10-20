import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ProductionPlanV2 } from './productionPlanV2Model';


const api = vi.hoisted(() => ({ createRemixArtifact: vi.fn(), reviseRemixArtifact: vi.fn(),
  fetchRemixArtifact: vi.fn(), fetchLatestRemixArtifact: vi.fn() }));
const canvasSession = vi.hoisted(() => ({ current: 'canvas-a' }));
const node = { id: 'plan-1', data: {} as Record<string, unknown> };
const updateNodeData = vi.fn((id: string, patch: Record<string, unknown>) => {
  if (id === node.id) node.data = { ...node.data, ...patch };
});
vi.mock('@/api/remix', () => api);
vi.mock('@/features/freezone/canvasSyncRuntime', () => ({
  captureRegisteredFreezoneCanvasSession: () => {
    const id = canvasSession.current;
    return { id, isCurrent: () => canvasSession.current === id };
  },
}));
vi.mock('@/stores/canvasStore', () => ({ useCanvasStore: { getState: () => ({ nodes: [node], updateNodeData }) } }));

import { flushProductionPlanV2AutoSave, scheduleProductionPlanV2AutoSave } from './productionPlanV2AutoSave';

function savablePlan(goal: string): ProductionPlanV2 { return { schema_version: 2, goal, output: { width: 1920, height: 1080, fps_num: 25, fps_den: 1 }, beats: [{ id: 'b', purpose: '开场', duration_frames: 75, turns: [] }], resources: [], placements: [], provenance: {} }; }
describe('production plan autosave', () => {
  beforeEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
    canvasSession.current = 'canvas-a';
    node.data = {};
    api.createRemixArtifact.mockResolvedValue({ id: 'plan-artifact', revision: 1 });
    api.reviseRemixArtifact.mockResolvedValue({ id: 'plan-artifact', revision: 2 });
  });

  it('saves the latest edit after a short pause without a button click', async () => {
    vi.useFakeTimers();
    const first = savablePlan('黑咖啡');
    const latest = { ...first, goal: '黑咖啡短片 ' };
    updateNodeData(node.id, { productionPlanV2Draft: first, productionPlanV2Dirty: true });
    scheduleProductionPlanV2AutoSave('demo', node.id);
    updateNodeData(node.id, { productionPlanV2Draft: latest, productionPlanV2Dirty: true });
    scheduleProductionPlanV2AutoSave('demo', node.id);
    await vi.advanceTimersByTimeAsync(500);
    expect(api.createRemixArtifact).toHaveBeenCalledTimes(1);
    expect(api.createRemixArtifact).toHaveBeenCalledWith('demo', 'production_plan.v2', latest, []);
    expect(node.data.productionPlanV2Ref).toEqual({ artifactId: 'plan-artifact', revision: 1 });
    expect(node.data.productionPlanV2Dirty).toBe(false);
    vi.useRealTimers();
  });

  it('keeps authored pace and extra pause in the saved v2 beat', async () => {
    const plan = savablePlan('保留节奏');
    plan.beats[0].speech_pace = 'slow';
    plan.beats[0].extra_pause_seconds = 0.8;
    updateNodeData(node.id, { productionPlanV2Draft: plan, productionPlanV2Dirty: true });
    await flushProductionPlanV2AutoSave('demo', node.id);
    expect(api.createRemixArtifact).toHaveBeenCalledWith('demo', 'production_plan.v2', plan, []);
    expect((node.data.productionPlanV2Draft as ProductionPlanV2).beats[0]).toMatchObject({
      speech_pace: 'slow', extra_pause_seconds: 0.8,
    });
  });

  it('serializes saves and keeps a newer edit when an earlier request completes', async () => {
    let finishFirst!: (value: { id: string; revision: number }) => void;
    api.createRemixArtifact.mockImplementation(() => new Promise((resolve) => { finishFirst = resolve; }));
    const first = savablePlan('第一版');
    const latest = { ...first, goal: '第二版' };
    updateNodeData(node.id, { productionPlanV2Draft: first, productionPlanV2Dirty: true });
    scheduleProductionPlanV2AutoSave('demo', node.id);
    const firstFlush = flushProductionPlanV2AutoSave('demo', node.id);
    await vi.waitFor(() => expect(api.createRemixArtifact).toHaveBeenCalledTimes(1));
    updateNodeData(node.id, { productionPlanV2Draft: latest, productionPlanV2Dirty: true });
    scheduleProductionPlanV2AutoSave('demo', node.id);
    finishFirst({ id: 'plan-artifact', revision: 1 });
    await firstFlush;
    expect(api.reviseRemixArtifact).toHaveBeenCalledWith('demo', { artifactId: 'plan-artifact', revision: 1 }, latest, []);
    expect(node.data.productionPlanV2Ref).toEqual({ artifactId: 'plan-artifact', revision: 2 });
    expect(node.data.productionPlanV2Dirty).toBe(false);
  });

  it('flushes immediately and reports a failed save without claiming success', async () => {
    api.createRemixArtifact.mockRejectedValueOnce(new Error('网络断开'));
    updateNodeData(node.id, { productionPlanV2Draft: savablePlan('黑咖啡'), productionPlanV2Dirty: true });
    scheduleProductionPlanV2AutoSave('demo', node.id);
    await expect(flushProductionPlanV2AutoSave('demo', node.id)).rejects.toThrow('网络断开');
    expect(node.data.productionPlanV2Dirty).toBe(true);
    expect(node.data.productionPlanV2SaveError).toBe('网络断开');
  });

  it('carries migrated source and blueprint lineage on every v2 revision', async () => {
    const migrated = savablePlan('迁移后编辑');
    migrated.provenance = { source_plan_ref: { artifact_id: 'old-plan', revision: 5 }, blueprint_ref: { artifact_id: 'blueprint', revision: 2 }, branch_source_ref: { artifact_id: 'branch-parent', revision: 3 } };
    updateNodeData(node.id, { productionPlanV2Draft: migrated, productionPlanV2Ref: { artifactId: 'v2', revision: 1 }, productionPlanV2Dirty: true });
    await flushProductionPlanV2AutoSave('demo', node.id);
    expect(api.reviseRemixArtifact).toHaveBeenCalledWith('demo', { artifactId: 'v2', revision: 1 }, migrated,
      [{ artifactId: 'blueprint', revision: 2 }, { artifactId: 'old-plan', revision: 5 }, { artifactId: 'branch-parent', revision: 3 }]);
  });

  it('includes accepted speech evidence in the saved plan lineage', async () => {
    const spoken = savablePlan('有台词');
    spoken.beats[0].turns = [{ id: 'turn', speaker: '甲', text: '你好', speech_timing: {
      placement_id: 'speech', media_hash: 'a'.repeat(64), text: '你好', text_hash: 'b'.repeat(64),
      source_in_us: 0, source_out_us: 1_000_000,
      evidence_ref: { artifact_id: 'timed-words', revision: 3 }, words: [{ id: 'w', text: '你好', start_us: 10_000, end_us: 200_000 }],
    } }];
    updateNodeData(node.id, { productionPlanV2Draft: spoken, productionPlanV2Ref: { artifactId: 'v2', revision: 1 }, productionPlanV2Dirty: true });
    await flushProductionPlanV2AutoSave('demo', node.id);
    expect(api.reviseRemixArtifact).toHaveBeenCalledWith('demo', { artifactId: 'v2', revision: 1 }, spoken,
      [{ artifactId: 'timed-words', revision: 3 }]);
  });

  it('includes cue evidence exactly once even when a turn shares the same artifact', async () => {
    const spoken = savablePlan('两人同一段视频');
    spoken.speech_cues = [
      { id: 'cue-1', turn_id: 'turn-1', take_id: 'take-1', text: '你好', source_start_us: 100_000, source_end_us: 300_000,
        timing_source: 'srt_import', timing_status: 'ready', word_ids: [], evidence_ref: { artifact_id: 'srt-evidence', revision: 2 } },
      { id: 'cue-2', turn_id: 'turn-2', take_id: 'take-1', text: '再见', source_start_us: 400_000, source_end_us: 700_000,
        timing_source: 'srt_import', timing_status: 'ready', word_ids: [], evidence_ref: { artifact_id: 'srt-evidence', revision: 2 } },
    ];
    updateNodeData(node.id, { productionPlanV2Draft: spoken, productionPlanV2Dirty: true });
    await flushProductionPlanV2AutoSave('demo', node.id);
    expect(api.createRemixArtifact).toHaveBeenCalledWith('demo', 'production_plan.v2', spoken,
      [{ artifactId: 'srt-evidence', revision: 2 }]);
  });

  it('retains local changes and stops retrying a conflicting revision', async () => {
    const conflict = Object.assign(new Error('修订冲突'), { response: { status: 409 } });
    api.reviseRemixArtifact.mockRejectedValueOnce(conflict);
    updateNodeData(node.id, { productionPlanV2Draft: savablePlan('当前修改'), productionPlanV2Dirty: true, productionPlanV2Ref: { artifactId: 'saved', revision: 4 } });
    await expect(flushProductionPlanV2AutoSave('demo', node.id)).rejects.toThrow('修订冲突');
    expect(node.data.productionPlanV2Conflict).toBe(true);
    expect(node.data.productionPlanV2Dirty).toBe(true);
    await expect(flushProductionPlanV2AutoSave('demo', node.id)).rejects.toThrow('修订冲突');
    expect(api.reviseRemixArtifact).toHaveBeenCalledTimes(1);
  });

  it('rebases disjoint local edits once and preserves the newer remote field', async () => {
    const base = savablePlan('原目标');
    const local = { ...base, goal: '本地目标' };
    const remote = structuredClone(base);
    remote.beats[0].purpose = 'Agent 更新的段落';
    const conflict = Object.assign(new Error('修订冲突'), { response: { status: 409 } });
    api.reviseRemixArtifact.mockRejectedValueOnce(conflict)
      .mockResolvedValueOnce({ id: 'v2', revision: 3 });
    api.fetchRemixArtifact.mockResolvedValue({ schema: 'production_plan.v2', id: 'v2', revision: 1, payload: base });
    api.fetchLatestRemixArtifact.mockResolvedValue({ schema: 'production_plan.v2', id: 'v2', revision: 2, payload: remote });
    updateNodeData(node.id, { productionPlanV2Draft: local, productionPlanV2Ref: { artifactId: 'v2', revision: 1 }, productionPlanV2Dirty: true });
    await flushProductionPlanV2AutoSave('demo', node.id);
    expect(api.reviseRemixArtifact).toHaveBeenCalledTimes(2);
    expect(api.reviseRemixArtifact).toHaveBeenLastCalledWith('demo', { artifactId: 'v2', revision: 2 },
      expect.objectContaining({ goal: '本地目标', beats: [expect.objectContaining({ purpose: 'Agent 更新的段落' })] }), []);
    expect(node.data.productionPlanV2Ref).toEqual({ artifactId: 'v2', revision: 3 });
    expect((node.data.productionPlanV2Draft as ProductionPlanV2).beats[0].purpose).toBe('Agent 更新的段落');
    expect(node.data.productionPlanV2Conflict).toBe(false);
  });

  it('keeps both drafts when the remote changed the same field', async () => {
    const base = savablePlan('原目标');
    const local = { ...base, goal: '用户目标' };
    const remote = { ...base, goal: 'Agent 目标' };
    const conflict = Object.assign(new Error('修订冲突'), { response: { status: 409 } });
    api.reviseRemixArtifact.mockRejectedValueOnce(conflict);
    api.fetchRemixArtifact.mockResolvedValue({ schema: 'production_plan.v2', id: 'v2', revision: 1, payload: base });
    api.fetchLatestRemixArtifact.mockResolvedValue({ schema: 'production_plan.v2', id: 'v2', revision: 2, payload: remote });
    updateNodeData(node.id, { productionPlanV2Draft: local, productionPlanV2Ref: { artifactId: 'v2', revision: 1 }, productionPlanV2Dirty: true });
    await expect(flushProductionPlanV2AutoSave('demo', node.id)).rejects.toThrow('修订冲突');
    expect(api.reviseRemixArtifact).toHaveBeenCalledTimes(1);
    expect(node.data.productionPlanV2Draft).toEqual(local);
    expect(node.data.productionPlanV2Conflict).toBe(true);
  });

  it('recognizes a saved revision whose response was lost without writing it again', async () => {
    const base = savablePlan('原目标');
    const local = { ...base, goal: '已保存目标' };
    const latest = structuredClone(local);
    latest.beats[0].purpose = '后续其他改动';
    const conflict = Object.assign(new Error('修订冲突'), { response: { status: 409 } });
    api.reviseRemixArtifact.mockRejectedValueOnce(conflict);
    api.fetchRemixArtifact.mockResolvedValue({ schema: 'production_plan.v2', id: 'v2', revision: 1, payload: base });
    api.fetchLatestRemixArtifact.mockResolvedValue({ schema: 'production_plan.v2', id: 'v2', revision: 3, payload: latest });
    updateNodeData(node.id, { productionPlanV2Draft: local, productionPlanV2Ref: { artifactId: 'v2', revision: 1 }, productionPlanV2Dirty: true });
    await flushProductionPlanV2AutoSave('demo', node.id);
    expect(api.reviseRemixArtifact).toHaveBeenCalledTimes(1);
    expect(node.data.productionPlanV2Ref).toEqual({ artifactId: 'v2', revision: 3 });
    expect(node.data.productionPlanV2Draft).toEqual(latest);
  });

  it('does not overwrite input typed while a rebased save is pending', async () => {
    const base = savablePlan('原目标');
    const local = { ...base, goal: '用户目标' };
    const remote = structuredClone(base);
    remote.beats[0].purpose = 'Agent 段落';
    const conflict = Object.assign(new Error('修订冲突'), { response: { status: 409 } });
    let finishRetry!: (value: { id: string; revision: number }) => void;
    api.reviseRemixArtifact.mockRejectedValueOnce(conflict)
      .mockImplementationOnce(() => new Promise((resolve) => { finishRetry = resolve; }));
    api.fetchRemixArtifact.mockResolvedValue({ schema: 'production_plan.v2', id: 'v2', revision: 1, payload: base });
    api.fetchLatestRemixArtifact.mockResolvedValue({ schema: 'production_plan.v2', id: 'v2', revision: 2, payload: remote });
    updateNodeData(node.id, { productionPlanV2Draft: local, productionPlanV2Ref: { artifactId: 'v2', revision: 1 }, productionPlanV2Dirty: true });
    const pending = flushProductionPlanV2AutoSave('demo', node.id);
    await vi.waitFor(() => expect(api.reviseRemixArtifact).toHaveBeenCalledTimes(2));
    const newer = { ...local, goal: '继续编辑' };
    updateNodeData(node.id, { productionPlanV2Draft: newer, productionPlanV2Dirty: true });
    finishRetry({ id: 'v2', revision: 3 });
    await pending;
    expect(node.data.productionPlanV2Draft).toBe(newer);
    expect(node.data.productionPlanV2Ref).toEqual({ artifactId: 'v2', revision: 3 });
    expect(node.data.productionPlanV2Dirty).toBe(true);
    expect(node.data.productionPlanV2Conflict).toBe(true);
  });

  it('does not mark a newer AI result dirty when an older save fails', async () => {
    let rejectOld!: (reason: Error) => void;
    api.createRemixArtifact.mockImplementation(() => new Promise((_resolve, reject) => { rejectOld = reject; }));
    updateNodeData(node.id, { productionPlanV2Draft: savablePlan('旧方案'), productionPlanV2Dirty: true });
    scheduleProductionPlanV2AutoSave('demo', node.id);
    const pending = flushProductionPlanV2AutoSave('demo', node.id);
    await vi.waitFor(() => expect(api.createRemixArtifact).toHaveBeenCalledTimes(1));
    updateNodeData(node.id, {
      productionPlanV2Draft: savablePlan('AI 新方案'),
      productionPlanV2Ref: { artifactId: 'ai-plan', revision: 1 }, productionPlanV2Dirty: false,
    });
    rejectOld(new Error('旧请求失败'));
    await expect(pending).resolves.toEqual({ artifactId: 'ai-plan', revision: 1 });
    expect(node.data.productionPlanV2Ref).toEqual({ artifactId: 'ai-plan', revision: 1 });
    expect(node.data.productionPlanV2Dirty).toBe(false);
    expect(node.data.productionPlanV2SaveError).toBeUndefined();
  });

  it('ignores an old timer after a canvas switch with the same node ID', async () => {
    vi.useFakeTimers();
    updateNodeData(node.id, { productionPlanV2Draft: savablePlan('旧画布'), productionPlanV2Dirty: true });
    scheduleProductionPlanV2AutoSave('demo', node.id);
    canvasSession.current = 'canvas-b';
    node.data = { productionPlanV2Draft: savablePlan('新画布'), productionPlanV2Dirty: true };
    await vi.advanceTimersByTimeAsync(500);
    expect(api.createRemixArtifact).not.toHaveBeenCalled();
    expect(node.data.productionPlanV2SaveError).toBeUndefined();
    scheduleProductionPlanV2AutoSave('demo', node.id);
    await vi.advanceTimersByTimeAsync(500);
    expect(api.createRemixArtifact).toHaveBeenCalledTimes(1);
    expect(api.createRemixArtifact).toHaveBeenCalledWith('demo', 'production_plan.v2', expect.objectContaining({ goal: '新画布' }), []);
    expect(node.data.productionPlanV2Ref).toEqual({ artifactId: 'plan-artifact', revision: 1 });
    vi.useRealTimers();
  });

  it('does not let an old in-flight response block or mutate a new canvas with the same node ID', async () => {
    let finishOld!: (value: { id: string; revision: number }) => void;
    api.createRemixArtifact.mockImplementationOnce(() => new Promise((resolve) => { finishOld = resolve; }))
      .mockResolvedValueOnce({ id: 'new-plan', revision: 1 });
    node.data = { productionPlanV2Draft: savablePlan('旧画布'), productionPlanV2Dirty: true };
    const oldFlush = flushProductionPlanV2AutoSave('demo', node.id);
    await vi.waitFor(() => expect(api.createRemixArtifact).toHaveBeenCalledTimes(1));
    canvasSession.current = 'canvas-b';
    node.data = { productionPlanV2Draft: savablePlan('新画布'), productionPlanV2Dirty: true };
    await expect(flushProductionPlanV2AutoSave('demo', node.id)).resolves.toEqual({ artifactId: 'new-plan', revision: 1 });
    expect(node.data.productionPlanV2SaveError).toBeNull();
    finishOld({ id: 'old-plan', revision: 1 });
    await expect(oldFlush).rejects.toThrow(/所属画布已切换/);
    expect(node.data.productionPlanV2Ref).toEqual({ artifactId: 'new-plan', revision: 1 });
    expect(node.data.productionPlanV2Dirty).toBe(false);
    expect(node.data.productionPlanV2SaveError).toBeNull();
  });

  it('does not show an old save failure on the new canvas', async () => {
    let rejectOld!: (reason: Error) => void;
    api.createRemixArtifact.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectOld = reject; }));
    node.data = { productionPlanV2Draft: savablePlan('旧画布'), productionPlanV2Dirty: true };
    const oldFlush = flushProductionPlanV2AutoSave('demo', node.id);
    await vi.waitFor(() => expect(api.createRemixArtifact).toHaveBeenCalledTimes(1));
    canvasSession.current = 'canvas-b';
    node.data = { productionPlanV2Draft: savablePlan('新画布'), productionPlanV2Dirty: true };
    rejectOld(new Error('旧请求失败'));
    await expect(oldFlush).rejects.toThrow(/所属画布已切换/);
    expect(node.data.productionPlanV2SaveError).toBeUndefined();
    expect(node.data.productionPlanV2Ref).toBeUndefined();
    expect(node.data.productionPlanV2Dirty).toBe(true);
  });
});
