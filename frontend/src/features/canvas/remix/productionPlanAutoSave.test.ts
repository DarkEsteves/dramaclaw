import { beforeEach, describe, expect, it, vi } from 'vitest';
import { emptyProductionPlan } from './productionPlanModel';

const api = vi.hoisted(() => ({ createRemixArtifact: vi.fn(), reviseRemixArtifact: vi.fn() }));
const node = { id: 'plan-1', data: {} as Record<string, unknown> };
const updateNodeData = vi.fn((id: string, patch: Record<string, unknown>) => {
  if (id === node.id) node.data = { ...node.data, ...patch };
});
vi.mock('@/api/remix', () => api);
vi.mock('@/stores/canvasStore', () => ({ useCanvasStore: { getState: () => ({ nodes: [node], updateNodeData }) } }));

import { flushProductionPlanAutoSave, scheduleProductionPlanAutoSave } from './productionPlanAutoSave';

function savablePlan(goal: string) {
  const draft = emptyProductionPlan();
  draft.goal = goal;
  draft.beats[0].purpose = '提出问题';
  return draft;
}

describe('production plan autosave', () => {
  beforeEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
    node.data = {};
    api.createRemixArtifact.mockResolvedValue({ id: 'plan-artifact', revision: 1 });
    api.reviseRemixArtifact.mockResolvedValue({ id: 'plan-artifact', revision: 2 });
  });

  it('saves the latest edit after a short pause without a button click', async () => {
    vi.useFakeTimers();
    const first = savablePlan('黑咖啡');
    const latest = { ...first, goal: '黑咖啡短片 ' };
    updateNodeData(node.id, { productionPlanDraft: first, productionPlanDirty: true });
    scheduleProductionPlanAutoSave('demo', node.id);
    updateNodeData(node.id, { productionPlanDraft: latest, productionPlanDirty: true });
    scheduleProductionPlanAutoSave('demo', node.id);
    await vi.advanceTimersByTimeAsync(500);
    expect(api.createRemixArtifact).toHaveBeenCalledTimes(1);
    expect(api.createRemixArtifact).toHaveBeenCalledWith('demo', 'production_plan.v1', latest, []);
    expect(node.data.productionPlanRef).toEqual({ artifactId: 'plan-artifact', revision: 1 });
    expect(node.data.productionPlanDirty).toBe(false);
    vi.useRealTimers();
  });

  it('waits for an unfinished beat or dialogue before posting the draft', async () => {
    const draft = { ...emptyProductionPlan(), goal: '黑咖啡' };
    updateNodeData(node.id, { productionPlanDraft: draft, productionPlanDirty: true });
    await flushProductionPlanAutoSave('demo', node.id);
    expect(api.createRemixArtifact).not.toHaveBeenCalled();
    expect(node.data.productionPlanSaveError).toBeUndefined();
    draft.beats[0].purpose = '提问开场';
    draft.beats[0].turns.push({ speaker: '女', text: '' });
    updateNodeData(node.id, { productionPlanDraft: draft, productionPlanDirty: true });
    await flushProductionPlanAutoSave('demo', node.id);
    expect(api.createRemixArtifact).not.toHaveBeenCalled();
    draft.beats[0].turns[0].text = '黑咖啡';
    updateNodeData(node.id, { productionPlanDraft: draft, productionPlanDirty: true });
    await flushProductionPlanAutoSave('demo', node.id);
    expect(api.createRemixArtifact).toHaveBeenCalledTimes(1);
  });

  it('serializes saves and keeps a newer edit when an earlier request completes', async () => {
    let finishFirst!: (value: { id: string; revision: number }) => void;
    api.createRemixArtifact.mockImplementation(() => new Promise((resolve) => { finishFirst = resolve; }));
    const first = savablePlan('第一版');
    const latest = { ...first, goal: '第二版' };
    updateNodeData(node.id, { productionPlanDraft: first, productionPlanDirty: true });
    scheduleProductionPlanAutoSave('demo', node.id);
    const firstFlush = flushProductionPlanAutoSave('demo', node.id);
    await vi.waitFor(() => expect(api.createRemixArtifact).toHaveBeenCalledTimes(1));
    updateNodeData(node.id, { productionPlanDraft: latest, productionPlanDirty: true });
    scheduleProductionPlanAutoSave('demo', node.id);
    finishFirst({ id: 'plan-artifact', revision: 1 });
    await firstFlush;
    expect(api.reviseRemixArtifact).toHaveBeenCalledWith('demo', { artifactId: 'plan-artifact', revision: 1 }, latest, []);
    expect(node.data.productionPlanRef).toEqual({ artifactId: 'plan-artifact', revision: 2 });
    expect(node.data.productionPlanDirty).toBe(false);
  });

  it('flushes immediately and reports a failed save without claiming success', async () => {
    api.createRemixArtifact.mockRejectedValueOnce(new Error('网络断开'));
    updateNodeData(node.id, { productionPlanDraft: savablePlan('黑咖啡'), productionPlanDirty: true });
    scheduleProductionPlanAutoSave('demo', node.id);
    await expect(flushProductionPlanAutoSave('demo', node.id)).rejects.toThrow('网络断开');
    expect(node.data.productionPlanDirty).toBe(true);
    expect(node.data.productionPlanSaveError).toBe('网络断开');
  });

  it('does not mark a newer AI result dirty when an older save fails', async () => {
    let rejectOld!: (reason: Error) => void;
    api.createRemixArtifact.mockImplementation(() => new Promise((_resolve, reject) => { rejectOld = reject; }));
    updateNodeData(node.id, { productionPlanDraft: savablePlan('旧方案'), productionPlanDirty: true });
    scheduleProductionPlanAutoSave('demo', node.id);
    const pending = flushProductionPlanAutoSave('demo', node.id);
    await vi.waitFor(() => expect(api.createRemixArtifact).toHaveBeenCalledTimes(1));
    updateNodeData(node.id, {
      productionPlanDraft: { ...emptyProductionPlan(), goal: 'AI 新方案' },
      productionPlanRef: { artifactId: 'ai-plan', revision: 1 }, productionPlanDirty: false,
    });
    rejectOld(new Error('旧请求失败'));
    await expect(pending).resolves.toEqual({ artifactId: 'ai-plan', revision: 1 });
    expect(node.data.productionPlanRef).toEqual({ artifactId: 'ai-plan', revision: 1 });
    expect(node.data.productionPlanDirty).toBe(false);
    expect(node.data.productionPlanSaveError).toBeUndefined();
  });
});
