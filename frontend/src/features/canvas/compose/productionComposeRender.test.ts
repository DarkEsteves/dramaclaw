import { beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ projectProductionPlan: vi.fn(), compileProductionPlan: vi.fn(), createRemixArtifact: vi.fn(), renderRemixManifest: vi.fn() }));
const save = vi.hoisted(() => ({ flushProductionPlanV2AutoSave: vi.fn() }));
const scope = vi.hoisted(() => ({ current: true }));
const sync = vi.hoisted(() => ({ flushFreezoneCanvasRuntime: vi.fn(), captureFreezoneCanvasScope: () => () => scope.current }));
const state = {
  nodes: [] as Array<{ id: string; type: string; data: Record<string, unknown> }>,
  edges: [] as Array<{ source: string; target: string }>,
  updateNodeData: vi.fn((id: string, patch: Record<string, unknown>) => {
    const node = state.nodes.find((item) => item.id === id);
    if (node) node.data = { ...node.data, ...patch };
  }),
};
vi.mock('@/api/remix', () => api);
vi.mock('@/features/canvas/remix/productionPlanV2AutoSave', () => save);
vi.mock('@/features/freezone/canvasSyncRuntime', () => sync);
vi.mock('@/features/canvas/remix/prepareNodeRemix', () => ({ remixGraphFingerprint: () => JSON.stringify(state.nodes.find((node) => node.id === 'media')?.data ?? {}) }));
vi.mock('@/stores/canvasStore', () => ({ useCanvasStore: { getState: () => state } }));

import { submitProductionComposeRender } from './productionComposeRender';

describe('production plan compose render', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    scope.current = true;
    const ref = { artifactId: 'plan-ref', revision: 2 };
    state.nodes = [
      { id: 'plan', type: 'scriptNode', data: { productionPlanV2Ref: ref, productionPlanV2Draft: { schema_version: 2, beats: [], placements: [], resources: [] } } },
      { id: 'compose', type: 'videoComposeNode', data: { productionPlanNodeId: 'plan', productionPlanV2Ref: ref, resultVideoUrl: '/old.mp4' } },
    ];
    state.edges = [{ source: 'plan', target: 'compose' }];
    save.flushProductionPlanV2AutoSave.mockResolvedValue(ref);
    api.projectProductionPlan.mockResolvedValue({ ready: true, issues: [] });
    api.compileProductionPlan.mockResolvedValue({ manifest_version: 2 });
    api.createRemixArtifact.mockResolvedValue({ id: 'manifest', revision: 1 });
    api.renderRemixManifest.mockResolvedValue({ task_key: 'render-job', task_type: 'remix_render', job_id: 'render-job', operation_id: 'operation' });
    sync.flushFreezoneCanvasRuntime.mockResolvedValue(true);
  });

  it('submits the layered saved plan through the protected canvas-bound render endpoint', async () => {
    api.renderRemixManifest.mockImplementation(async () => {
      // The request can succeed while its response is lost. Recovery must
      // still find the exact source revision in the already saved canvas.
      expect(sync.flushFreezoneCanvasRuntime).toHaveBeenCalledTimes(1);
      expect(state.nodes[1].data.productionPlanRenderingRef).toEqual({ artifactId: 'plan-ref', revision: 2 });
      return { task_key: 'render-job', task_type: 'remix_render', job_id: 'render-job', operation_id: 'operation' };
    });
    const result = await submitProductionComposeRender({ project: 'demo', canvasId: 'canvas', planNodeId: 'plan', composeNodeId: 'compose' });
    expect(api.compileProductionPlan).toHaveBeenCalledWith('demo', { artifactId: 'plan-ref', revision: 2 });
    expect(api.createRemixArtifact).toHaveBeenCalledWith('demo', 'composition_manifest.v2', { manifest_version: 2 }, [{ artifactId: 'plan-ref', revision: 2 }]);
    expect(sync.flushFreezoneCanvasRuntime).toHaveBeenCalledBefore(api.renderRemixManifest);
    expect(api.renderRemixManifest).toHaveBeenCalledWith('demo', expect.objectContaining({ manifestRef: { artifactId: 'manifest', revision: 1 }, canvasId: 'canvas', nodeId: 'compose', nodeRevision: 1 }));
    expect(state.nodes[1].data).toMatchObject({ remixPreviousVideoUrl: '/old.mp4', resultVideoUrl: null, remixOperationId: 'operation', productionPlanCompiledRef: { artifactId: 'plan-ref', revision: 2 }, productionPlanRenderingRef: { artifactId: 'plan-ref', revision: 2 } });
    expect(result.task.operation_id).toBe('operation');
  });

  it('refuses to render when the plan changes during compilation', async () => {
    api.compileProductionPlan.mockImplementation(async () => {
      state.updateNodeData('plan', { productionPlanV2Dirty: true });
      return { manifest_version: 2 };
    });
    await expect(submitProductionComposeRender({ project: 'demo', canvasId: 'canvas', planNodeId: 'plan', composeNodeId: 'compose' })).rejects.toThrow(/作品方案.*变化/);
    expect(api.createRemixArtifact).not.toHaveBeenCalled();
    expect(api.renderRemixManifest).not.toHaveBeenCalled();
  });

  it('does not bind a manifest into another canvas after the active session switches', async () => {
    api.compileProductionPlan.mockImplementation(async () => {
      scope.current = false;
      return { manifest_version: 2 };
    });
    await expect(submitProductionComposeRender({ project: 'demo', canvasId: 'canvas', planNodeId: 'plan', composeNodeId: 'compose' })).rejects.toThrow(/画布.*切换/);
    expect(api.createRemixArtifact).not.toHaveBeenCalled();
    expect(api.renderRemixManifest).not.toHaveBeenCalled();
    expect(state.nodes[1].data.resultVideoUrl).toBe('/old.mp4');
  });

  it('keeps the exact rendering ref after a submitted request loses its response', async () => {
    api.renderRemixManifest.mockRejectedValue(new Error('connection lost'));
    await expect(submitProductionComposeRender({ project: 'demo', canvasId: 'canvas', planNodeId: 'plan', composeNodeId: 'compose' })).rejects.toThrow('connection lost');
    expect(sync.flushFreezoneCanvasRuntime).toHaveBeenCalledTimes(1);
    expect(state.nodes[1].data).toMatchObject({
      productionPlanRenderingRef: { artifactId: 'plan-ref', revision: 2 },
      remixManifestRef: { artifactId: 'manifest', revision: 1 },
      remixPreviousVideoUrl: '/old.mp4',
    });
  });

  it('refuses an old manifest when a connected material changes before binding it to the canvas', async () => {
    state.nodes.push({ id: 'media', type: 'videoNode', data: { remixMedia: { media_hash: 'old' } } });
    state.edges.push({ source: 'media', target: 'plan' });
    api.createRemixArtifact.mockImplementation(async () => {
      state.updateNodeData('media', { remixMedia: { media_hash: 'new' } });
      return { id: 'manifest', revision: 1 };
    });
    await expect(submitProductionComposeRender({ project: 'demo', canvasId: 'canvas', planNodeId: 'plan', composeNodeId: 'compose' })).rejects.toThrow(/关联素材.*变化/);
    expect(api.renderRemixManifest).not.toHaveBeenCalled();
    expect(state.nodes[1].data.resultVideoUrl).toBe('/old.mp4');
  });
});
