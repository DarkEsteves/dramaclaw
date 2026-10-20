import { beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({
  submitRemixProcessAction: vi.fn(), fetchRemixOperation: vi.fn(), fetchRemixArtifact: vi.fn(), fetchRemixTaskStatus: vi.fn(),
}));
const plan = { schema_version: 1, goal: '黑咖啡', output: { width: 1080, height: 1920, fps: 30 }, beats: [] };
const state = {
  nodes: [{ id: 'plan-1', data: { productionPlanDraft: plan } as Record<string, unknown> }],
  updateNodeData: vi.fn((nodeId: string, patch: Record<string, unknown>) => {
    const node = state.nodes.find((item) => item.id === nodeId);
    if (node) node.data = { ...node.data, ...patch };
  }),
};
vi.mock('@/api/remix', () => api);
vi.mock('@/lib/url-params', () => ({ readUrl: () => ({ project: 'demo' }) }));
vi.mock('@/stores/canvasStore', () => ({ useCanvasStore: { getState: () => state } }));

import { resumeProductionPlanDraft, startProductionPlanDraft } from './productionPlanDraftTask';

const task = { task_key: 'task-1', task_type: 'remix_generate_production_plan_draft', task_id: 'task-id', operation_id: 'operation-1' };
const operation = { status: 'needs_confirmation', input_refs: [], output_refs: [{ artifact_id: 'artifact-1', revision: 1 }] };
const artifact = { id: 'artifact-1', revision: 1, schema: 'production_plan.v1', payload: { ...plan, beats: [{ id: 'hook', purpose: '提问', duration_frames: 90, turns: [], visuals: [], audio: [] }] }, upstream_refs: [] };

describe('production plan draft task', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.nodes = [{ id: 'plan-1', data: { productionPlanDraft: plan } }];
    api.submitRemixProcessAction.mockResolvedValue(task);
    api.fetchRemixOperation.mockResolvedValue(operation);
    api.fetchRemixArtifact.mockResolvedValue(artifact);
  });

  it('returns a project task immediately and applies its saved result', async () => {
    const submitted = await startProductionPlanDraft('plan-1', '黑咖啡', null, { summary: '先提问' });
    expect(submitted.task_key).toBe('task-1');
    expect(api.submitRemixProcessAction).toHaveBeenCalledWith('demo', expect.objectContaining({
      action: 'generate_production_plan_draft', topic: '黑咖啡', analysisContext: { summary: '先提问' },
    }));
    await vi.waitFor(() => expect(state.nodes[0].data.productionPlanGenerationStatus).toBe('completed'));
    expect(state.nodes[0].data.productionPlanDraft).toEqual(artifact.payload);
    expect(state.nodes[0].data.productionPlanRef).toEqual({ artifactId: 'artifact-1', revision: 1 });
  });

  it('resumes an accepted task without starting a second one', async () => {
    state.nodes[0].data = { ...state.nodes[0].data,
      productionPlanGenerationOperationId: 'operation-1', productionPlanGenerationTopic: '黑咖啡',
      productionPlanGenerationBlueprintRef: null, productionPlanGenerationSnapshot: JSON.stringify(plan),
    };
    resumeProductionPlanDraft('plan-1');
    await vi.waitFor(() => expect(state.nodes[0].data.productionPlanGenerationStatus).toBe('completed'));
    expect(api.submitRemixProcessAction).not.toHaveBeenCalled();
  });

  it('does not replace edits made while generation runs', async () => {
    let resolveOperation: (value: unknown) => void = () => {};
    api.fetchRemixOperation.mockReturnValue(new Promise((resolve) => { resolveOperation = resolve; }));
    await startProductionPlanDraft('plan-1', '黑咖啡', null, null);
    state.nodes[0].data.productionPlanDraft = { ...plan, goal: '用户后来修改的目标' };
    resolveOperation(operation);
    await vi.waitFor(() => expect(state.nodes[0].data.productionPlanGenerationStatus).toBe('failed'));
    expect(state.nodes[0].data.productionPlanDraft).toEqual({ ...plan, goal: '用户后来修改的目标' });
  });

  it('submits only once for repeated clicks before acceptance', async () => {
    let accept: (value: unknown) => void = () => {};
    api.submitRemixProcessAction.mockReturnValue(new Promise((resolve) => { accept = resolve; }));
    const first = startProductionPlanDraft('plan-1', '黑咖啡', null, null);
    const second = startProductionPlanDraft('plan-1', '黑咖啡', null, null);
    expect(api.submitRemixProcessAction).toHaveBeenCalledTimes(1);
    accept(task);
    expect(await first).toEqual(await second);
  });
});
