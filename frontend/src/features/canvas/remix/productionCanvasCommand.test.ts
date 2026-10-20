import { beforeEach, describe, expect, it, vi } from 'vitest';
import { applyProductionCommand, attachRemixMedia, fetchRemixArtifact, preflightProductionCommand } from '@/api/remix';
import { preflightCanvasChatCommandEnvelopes } from '@/features/freezone/context/canvasCommandValidator';
import { buildCanvasContextRequestResponses } from '@/features/freezone/chatNodeReferences';
import { useCanvasStore } from '@/stores/canvasStore';
import { flushProductionPlanV2AutoSave } from './productionPlanV2AutoSave';
import type { CanvasNode } from '../domain/canvasNodes';
import { bindProductionCanvasCandidate, executeProductionCanvasCommand, parseProductionCanvasCommand, productionCanvasCommandError, preflightProductionCanvasCommand } from './productionCanvasCommand';
import { applyCanvasChatCommandsAsync, extractCanvasChatCommandEnvelopes } from '@/features/freezone/canvasChatCommands';
const scope = vi.hoisted(() => ({ current: true }));

vi.mock('@/api/remix', () => ({ preflightProductionCommand: vi.fn(), applyProductionCommand: vi.fn(), attachRemixMedia: vi.fn(), fetchRemixArtifact: vi.fn() }));
vi.mock('./productionPlanV2AutoSave', () => ({ flushProductionPlanV2AutoSave: vi.fn(async () => null) }));
vi.mock('@/features/freezone/canvasSyncRuntime', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/features/freezone/canvasSyncRuntime')>(),
  captureFreezoneCanvasScope: () => () => scope.current,
}));

const request = () => ({ type: 'production_plan_command' as const, node_id: 'plan-node', command: {
  command_id: 'edit-1', plan_ref: { artifact_id: 'plan', revision: 1 }, kind: 'edit_beat' as const,
  target_id: 'beat', payload: { purpose: '换个节奏' },
} });
const nodes = (): CanvasNode[] => [
  { id: 'plan-node', type: 'scriptNode', position: { x: 0, y: 0 }, data: {
    remixRole: 'production_plan', productionPlanV2Ref: { artifactId: 'plan', revision: 1 },
    productionPlanV2Draft: { goal: '原方案' }, productionPlanV2Dirty: false,
  } } as unknown as CanvasNode,
  { id: 'compose', type: 'videoComposeNode', position: { x: 500, y: 0 }, data: {
    productionPlanNodeId: 'plan-node', resultVideoUrl: 'old.mp4',
    productionPlanV2Ref: { artifactId: 'plan', revision: 1 },
  } } as unknown as CanvasNode,
];
const media = { media_path: 'media/clip.mp4', media_hash: 'a'.repeat(64), media_kind: 'video' as const,
  duration_us: 4_000_000, has_audio: true, url: '/static/projects/project/media/clip.mp4', size: 100 };
const resource = { id: 'fresh-video', node_id: 'video', media_kind: 'video' as const,
  media_path: media.media_path, media_hash: media.media_hash, duration_us: media.duration_us, has_audio: true };
const bindRequest = () => ({ type: 'production_plan_command' as const, node_id: 'plan-node', command: {
  command_id: 'bind-1', plan_ref: { artifact_id: 'plan', revision: 1 }, kind: 'bind_media' as const,
  target_id: 'visual', payload: { resource_id: resource.id, resource: { ...resource } },
} });
const videoNode = (videoUrl = media.url): CanvasNode => ({ id: 'video', type: 'videoNode', position: { x: 0, y: 0 },
  data: { videoUrl, remixNodeRevision: 2 } } as unknown as CanvasNode);

describe('approved production canvas command', () => {
  beforeEach(() => { vi.clearAllMocks(); scope.current = true; useCanvasStore.setState({ nodes: nodes(), edges: [] }); });

  it('binds a current canvas output through one revisioned native media command', async () => {
    useCanvasStore.setState({ nodes: [...nodes(), videoNode()], edges: [] });
    useCanvasStore.getState().updateNodeData('plan-node', { productionPlanV2Draft: { goal: '原方案', placements: [{ id: 'visual', kind: 'visual' }], resources: [] } });
    vi.mocked(attachRemixMedia).mockResolvedValue(media);
    vi.mocked(applyProductionCommand).mockResolvedValue({ plan_ref: { artifact_id: 'plan', revision: 2 }, changed_ids: ['visual'], invalidated: [], blockers: [] });
    vi.mocked(fetchRemixArtifact).mockResolvedValue({ id: 'plan', revision: 2, schema: 'production_plan.v2', payload: { goal: 'saved' } } as never);
    await bindProductionCanvasCandidate('project', 'canvas', 'plan-node', 'visual', 'video');
    expect(applyProductionCommand).toHaveBeenCalledWith('project', expect.objectContaining({
      kind: 'bind_media', target_id: 'visual', plan_ref: { artifact_id: 'plan', revision: 1 },
      payload: { resource_id: `resource:video:${'a'.repeat(12)}`, resource: expect.objectContaining({ node_id: 'video', media_hash: 'a'.repeat(64) }) },
    }));
    expect(useCanvasStore.getState().edges).toMatchObject([{ source: 'video', target: 'plan-node' }]);
  });

  it('uses the new revision returned by autosave before binding a candidate', async () => {
    useCanvasStore.setState({ nodes: [...nodes(), videoNode()], edges: [] });
    useCanvasStore.getState().updateNodeData('plan-node', { productionPlanV2Draft: { goal: '原方案', placements: [{ id: 'visual', kind: 'visual' }], resources: [] } });
    vi.mocked(attachRemixMedia).mockResolvedValue(media);
    vi.mocked(flushProductionPlanV2AutoSave).mockImplementationOnce(async () => {
      useCanvasStore.getState().updateNodeData('plan-node', { productionPlanV2Ref: { artifactId: 'plan', revision: 2 } });
      return { artifactId: 'plan', revision: 2 };
    });
    vi.mocked(applyProductionCommand).mockResolvedValue({ plan_ref: { artifact_id: 'plan', revision: 3 }, changed_ids: ['visual'], invalidated: [], blockers: [] });
    vi.mocked(fetchRemixArtifact).mockResolvedValue({ id: 'plan', revision: 3, schema: 'production_plan.v2', payload: { goal: 'saved' } } as never);
    await bindProductionCanvasCandidate('project', 'canvas', 'plan-node', 'visual', 'video');
    expect(applyProductionCommand).toHaveBeenCalledWith('project', expect.objectContaining({ plan_ref: { artifact_id: 'plan', revision: 2 } }));
  });

  it('leaves a changed pending material unbound while attachment is in flight', async () => {
    useCanvasStore.setState({ nodes: [...nodes(), videoNode()], edges: [] });
    useCanvasStore.getState().updateNodeData('plan-node', { productionPlanV2Draft: { goal: '原方案', placements: [{ id: 'visual', kind: 'visual' }], resources: [] },
      productionPlanV2PendingBindings: { visual: 'video' } });
    let release: (value: typeof media) => void = () => undefined;
    vi.mocked(attachRemixMedia).mockReturnValue(new Promise((resolve) => { release = resolve; }));
    const requested = () => (useCanvasStore.getState().nodes[0].data.productionPlanV2PendingBindings as Record<string, string>).visual === 'video';
    const running = bindProductionCanvasCandidate('project', 'canvas', 'plan-node', 'visual', 'video', requested);
    await vi.waitFor(() => expect(attachRemixMedia).toHaveBeenCalled());
    useCanvasStore.getState().updateNodeData('plan-node', { productionPlanV2PendingBindings: { visual: 'another-video' } });
    release(media);
    await expect(running).rejects.toThrow(/绑定目标已变化/);
    expect(applyProductionCommand).not.toHaveBeenCalled();
  });

  it('does not let an older slow bind replace a newer selection for the same placement', async () => {
    const alternate = { ...videoNode('/static/projects/project/media/other.mp4'), id: 'other',
      data: { videoUrl: '/static/projects/project/media/other.mp4', remixNodeRevision: 1,
        remixMedia: { ...media, media_path: 'media/other.mp4', media_hash: 'b'.repeat(64), url: '/static/projects/project/media/other.mp4' } } } as CanvasNode;
    useCanvasStore.setState({ nodes: [...nodes(), videoNode(), alternate], edges: [] });
    useCanvasStore.getState().updateNodeData('plan-node', { productionPlanV2Draft: { goal: '原方案', placements: [{ id: 'visual', kind: 'visual' }], resources: [] } });
    let release: (value: typeof media) => void = () => undefined;
    vi.mocked(attachRemixMedia).mockReturnValue(new Promise((resolve) => { release = resolve; }));
    vi.mocked(applyProductionCommand).mockResolvedValue({ plan_ref: { artifact_id: 'plan', revision: 2 }, changed_ids: ['visual'], invalidated: [], blockers: [] });
    vi.mocked(fetchRemixArtifact).mockResolvedValue({ id: 'plan', revision: 2, schema: 'production_plan.v2', payload: {
      goal: 'saved', placements: [{ id: 'visual', kind: 'visual', resource_id: `resource:other:${'b'.repeat(12)}` }], resources: [],
    } } as never);
    const older = bindProductionCanvasCandidate('project', 'canvas', 'plan-node', 'visual', 'video');
    await vi.waitFor(() => expect(attachRemixMedia).toHaveBeenCalled());
    await bindProductionCanvasCandidate('project', 'canvas', 'plan-node', 'visual', 'other');
    release(media);
    await expect(older).rejects.toThrow(/绑定目标已变化|已有更新的素材绑定/);
    expect(applyProductionCommand).toHaveBeenCalledTimes(1);
    expect(useCanvasStore.getState().nodes[0].data.productionPlanV2Draft).toMatchObject({ placements: [{ resource_id: `resource:other:${'b'.repeat(12)}` }] });
  });

  it('parses only bounded plan commands and rejects identity injection', () => {
    expect(parseProductionCanvasCommand(request())).toEqual(request());
    expect(parseProductionCanvasCommand({ ...request(), command: { ...request().command, tenant_id: 'other' } })).toBeNull();
    expect(parseProductionCanvasCommand({ ...request(), command: { ...request().command, payload: { arbitrary: true } } })).toBeNull();
    expect(parseProductionCanvasCommand({ ...request(), command: { ...request().command, kind: 'bind_speech_source', target_id: 'beat', payload: { placement_id: 'voice', turn_ids: ['host', 'guest'] } } })).not.toBeNull();
    expect(parseProductionCanvasCommand({ ...request(), command: { ...request().command, kind: 'reassign_speech_source', target_id: 'beat', payload: { placement_id: 'picture', turn_ids: ['host', 'guest'] } } })).not.toBeNull();
    expect(parseProductionCanvasCommand({ ...request(), command: { ...request().command, kind: 'reassign_speech_source', target_id: 'beat', payload: { placement_id: null, turn_ids: ['host', 'guest'] } } })).not.toBeNull();
    expect(parseProductionCanvasCommand({ ...request(), command: { ...request().command, kind: 'reassign_speech_source', target_id: 'beat', payload: { placement_id: null, turn_ids: ['host'] } } })).toBeNull();
    expect(parseProductionCanvasCommand({ ...request(), command: { ...request().command, kind: 'edit_turn', payload: { text: '新的台词', invalidate_speech_timing: true } } })).not.toBeNull();
    expect(parseProductionCanvasCommand({ ...request(), command: { ...request().command, kind: 'edit_turn', payload: { speech_placement_id: 'visual' } } })).not.toBeNull();
    expect(parseProductionCanvasCommand({ ...request(), command: { ...request().command, kind: 'edit_turn', payload: { speech_placement_id: null } } })).not.toBeNull();
    expect(parseProductionCanvasCommand({ ...request(), command: { ...request().command, kind: 'edit_turn', payload: { speech_placement_id: 4 } } })).toBeNull();
    expect(parseProductionCanvasCommand({ ...request(), command: { ...request().command, kind: 'edit_turn', payload: { text: '新的台词', invalidate_speech_timing: 'true' } } })).toBeNull();
    expect(parseProductionCanvasCommand({ ...request(), command: { ...request().command, kind: 'edit_speech_take', target_id: 'take', payload: { lead_in_frames: 8 } } })).not.toBeNull();
    expect(parseProductionCanvasCommand({ ...request(), command: { ...request().command, kind: 'edit_speech_take', target_id: 'take', payload: { turn_ids: ['different'] } } })).toBeNull();
    expect(parseProductionCanvasCommand({ ...request(), command: { ...request().command, kind: 'edit_beat', payload: { duration_source_placement_id: 'visual' } } })).not.toBeNull();
    expect(parseProductionCanvasCommand(bindRequest())).toEqual(bindRequest());
    expect(parseProductionCanvasCommand({ ...bindRequest(), command: { ...bindRequest().command,
      payload: { resource_id: resource.id, resource: { ...resource, id: 'different' } } } })).toBeNull();
    expect(parseProductionCanvasCommand({ ...bindRequest(), command: { ...bindRequest().command,
      payload: { resource_id: resource.id, resource: { ...resource, tenant_id: 'other' } } } })).toBeNull();
    expect(parseProductionCanvasCommand({ ...request(), command: { ...request().command,
      payload: { purpose: 'change', resource } } })).toBeNull();
  });

  it('attaches an unregistered current canvas output before binding a new resource', async () => {
    useCanvasStore.setState({ nodes: [...nodes(), videoNode()], edges: [] });
    vi.mocked(attachRemixMedia).mockResolvedValue(media);
    vi.mocked(applyProductionCommand).mockResolvedValue({ plan_ref: { artifact_id: 'plan', revision: 2 }, changed_ids: ['visual'], invalidated: ['render'], blockers: [] });
    vi.mocked(fetchRemixArtifact).mockResolvedValue({ id: 'plan', revision: 2, schema: 'production_plan.v2', payload: { goal: '素材已绑定' } } as never);
    await executeProductionCanvasCommand(bindRequest(), 'project', 'canvas');
    expect(attachRemixMedia).toHaveBeenCalledWith('project', media.media_path);
    expect(applyProductionCommand).toHaveBeenCalledWith('project', bindRequest().command);
    expect(useCanvasStore.getState().nodes.find((node) => node.id === 'video')?.data.remixMedia).toMatchObject(media);
    expect(useCanvasStore.getState().edges).toMatchObject([{ source: 'video', target: 'plan-node' }]);
  });

  it('rejects absent or mismatched current output before applying a bind command', async () => {
    await expect(executeProductionCanvasCommand(bindRequest(), 'project', 'canvas')).rejects.toThrow(/素材/);
    expect(applyProductionCommand).not.toHaveBeenCalled();
    useCanvasStore.setState({ nodes: [...nodes(), videoNode('/static/projects/project/media/other.mp4')], edges: [] });
    await expect(executeProductionCanvasCommand(bindRequest(), 'project', 'canvas')).rejects.toThrow(/素材/);
    expect(attachRemixMedia).not.toHaveBeenCalled();
    expect(applyProductionCommand).not.toHaveBeenCalled();
  });

  it('rejects an output replaced while media registration is in flight', async () => {
    useCanvasStore.setState({ nodes: [...nodes(), videoNode()], edges: [] });
    let release: (value: typeof media) => void = () => undefined;
    vi.mocked(attachRemixMedia).mockReturnValue(new Promise((resolve) => { release = resolve; }));
    const running = executeProductionCanvasCommand(bindRequest(), 'project', 'canvas');
    await vi.waitFor(() => expect(attachRemixMedia).toHaveBeenCalled());
    useCanvasStore.getState().updateNodeData('video', { videoUrl: '/static/projects/project/media/other.mp4' });
    release(media);
    await expect(running).rejects.toThrow(/素材.*变化|素材.*失效/);
    expect(applyProductionCommand).not.toHaveBeenCalled();
  });

  it('rejects regeneration at the same output path while media registration is in flight', async () => {
    useCanvasStore.setState({ nodes: [...nodes(), videoNode()], edges: [] });
    let release: (value: typeof media) => void = () => undefined;
    vi.mocked(attachRemixMedia).mockReturnValue(new Promise((resolve) => { release = resolve; }));
    const running = executeProductionCanvasCommand(bindRequest(), 'project', 'canvas');
    await vi.waitFor(() => expect(attachRemixMedia).toHaveBeenCalled());
    useCanvasStore.getState().updateNodeData('video', { remixNodeRevision: 3 });
    release(media);
    await expect(running).rejects.toThrow(/素材.*变化|素材.*失效/);
    expect(applyProductionCommand).not.toHaveBeenCalled();
  });

  it('retains a conflict instead of overwriting the plan when the source changes during the server command', async () => {
    useCanvasStore.setState({ nodes: [...nodes(), videoNode()], edges: [] });
    vi.mocked(attachRemixMedia).mockResolvedValue(media);
    vi.mocked(applyProductionCommand).mockImplementation(async () => {
      useCanvasStore.getState().updateNodeData('video', { videoUrl: '/static/projects/project/media/new.mp4' });
      return { plan_ref: { artifact_id: 'plan', revision: 2 }, changed_ids: ['visual'], invalidated: ['render'], blockers: [] };
    });
    vi.mocked(fetchRemixArtifact).mockResolvedValue({ id: 'plan', revision: 2, schema: 'production_plan.v2', payload: { goal: '服务器已改' } } as never);
    await expect(executeProductionCanvasCommand(bindRequest(), 'project', 'canvas')).rejects.toThrow(/版本冲突/);
    expect(useCanvasStore.getState().nodes[0].data).toMatchObject({ productionPlanV2Ref: { artifactId: 'plan', revision: 1 }, productionPlanV2Conflict: true });
    expect(useCanvasStore.getState().edges).toHaveLength(0);
  });

  it('rejects a stale expected revision before any backend mutation', async () => {
    const command = request(); command.command.plan_ref.revision = 2;
    expect(productionCanvasCommandError(command, useCanvasStore.getState().nodes)).toMatch(/版本/);
    await expect(executeProductionCanvasCommand(command, 'project', 'canvas')).rejects.toThrow(/版本/);
    expect(applyProductionCommand).not.toHaveBeenCalled();
  });

  it('loads the saved revision and marks linked composition stale while retaining its preview', async () => {
    vi.mocked(applyProductionCommand).mockResolvedValue({ plan_ref: { artifact_id: 'plan', revision: 2 }, changed_ids: ['beat'], invalidated: ['projection', 'render'], blockers: [] });
    vi.mocked(fetchRemixArtifact).mockResolvedValue({ id: 'plan', revision: 2, schema: 'production_plan.v2', payload: { goal: '新方案' } } as never);
    await executeProductionCanvasCommand(request(), 'project', 'canvas');
    expect(useCanvasStore.getState().nodes[0].data).toMatchObject({ productionPlanV2Ref: { artifactId: 'plan', revision: 2 }, productionPlanV2Draft: { goal: '新方案' }, productionPlanV2Dirty: false });
    expect(useCanvasStore.getState().nodes[1].data).toMatchObject({ productionPlanV2Ref: { artifactId: 'plan', revision: 2 }, resultVideoUrl: 'old.mp4', remixInputsCurrent: false });
    await executeProductionCanvasCommand(request(), 'project', 'canvas');
    expect(applyProductionCommand).toHaveBeenCalledTimes(1);
  });

  it('retains local edits made while the saved command is in flight', async () => {
    vi.mocked(applyProductionCommand).mockImplementation(async () => {
      useCanvasStore.getState().updateNodeData('plan-node', { productionPlanV2Draft: { goal: '人工修改' }, productionPlanV2Dirty: true });
      return { plan_ref: { artifact_id: 'plan', revision: 2 }, changed_ids: ['beat'], invalidated: ['render'], blockers: [] };
    });
    vi.mocked(fetchRemixArtifact).mockResolvedValue({ id: 'plan', revision: 2, schema: 'production_plan.v2', payload: { goal: '虾导修改' } } as never);
    await expect(executeProductionCanvasCommand(request(), 'project', 'canvas')).rejects.toThrow(/本地修改/);
    expect(useCanvasStore.getState().nodes[0].data).toMatchObject({ productionPlanV2Draft: { goal: '人工修改' }, productionPlanV2Conflict: true });
  });

  it('does not backfill an old-scope response into a newly selected canvas with the same node IDs', async () => {
    vi.mocked(applyProductionCommand).mockImplementation(async () => {
      scope.current = false;
      useCanvasStore.setState({ nodes: nodes(), edges: [] });
      return { plan_ref: { artifact_id: 'plan', revision: 2 }, changed_ids: ['beat'], invalidated: ['render'], blockers: [] };
    });
    vi.mocked(fetchRemixArtifact).mockResolvedValue({ id: 'plan', revision: 2, schema: 'production_plan.v2', payload: { goal: '旧画布结果' } } as never);
    await expect(executeProductionCanvasCommand(request(), 'project', 'canvas')).rejects.toThrow(/画布.*切换/);
    expect(useCanvasStore.getState().nodes[0].data).toMatchObject({ productionPlanV2Ref: { artifactId: 'plan', revision: 1 }, productionPlanV2Draft: { goal: '原方案' } });
    expect(useCanvasStore.getState().nodes[0].data.productionPlanV2Conflict).not.toBe(true);
  });

  it('executes a scoped production edit through the existing approved command batch', async () => {
    const envelopes = extractCanvasChatCommandEnvelopes([{ schema_version: 'canvas_chat_commands.v1', project_id: 'project', canvas_id: 'canvas', commands: [request()] }]);
    expect(envelopes).toHaveLength(1);
    vi.mocked(applyProductionCommand).mockResolvedValue({ plan_ref: { artifact_id: 'plan', revision: 2 }, changed_ids: ['beat'], invalidated: ['render'], blockers: [] });
    vi.mocked(fetchRemixArtifact).mockResolvedValue({ id: 'plan', revision: 2, schema: 'production_plan.v2', payload: { goal: '新方案' } } as never);
    const result = await applyCanvasChatCommandsAsync(envelopes, { projectId: 'project', canvasId: 'canvas' });
    expect(result.errors).toEqual([]);
    expect(result.applied).toBe(1);
    expect(applyProductionCommand).toHaveBeenCalledTimes(1);
  });
});


describe('direct production authoring', () => {
  beforeEach(() => { vi.clearAllMocks(); scope.current = true; useCanvasStore.setState({ nodes: nodes(), edges: [] }); });
  const batch = (commands: unknown[]) => ({ ...request(), command: { ...request().command, kind: 'batch' as const, target_id: 'plan', payload: { commands } } });
  it('accepts inserts and bounded atomic reordering with strictly validated child commands', () => {
    const insert = { kind: 'insert_turn', target_id: 'turn-new', payload: { beat_id: 'beat', index: 0, turn: { id: 'turn-new', speaker: '主持人', text: '你好' } } };
    const reorder = { kind: 'reorder_turns', target_id: 'beat', payload: { ids: ['turn-new', 'turn-old'] } };
    expect(parseProductionCanvasCommand(batch([insert, reorder]))).not.toBeNull();
    expect(parseProductionCanvasCommand(batch([{ ...insert, payload: { ...insert.payload, index: -1 } }]))).toBeNull();
    expect(parseProductionCanvasCommand(batch([{ ...insert, payload: { ...insert.payload, turn: { id: 'other' } } }]))).toBeNull();
    expect(parseProductionCanvasCommand(batch([{ ...reorder, payload: { ids: ['same', 'same'] } }]))).toBeNull();
    expect(parseProductionCanvasCommand(batch([{ ...insert, tenant_id: 'other' }]))).toBeNull();
    expect(parseProductionCanvasCommand(batch([batch([]).command]))).toBeNull();
    expect(parseProductionCanvasCommand(batch([]))).toBeNull();
    expect(parseProductionCanvasCommand(batch(Array(101).fill(reorder)))).toBeNull();
    expect(parseProductionCanvasCommand(batch([{ kind: 'bind_media', target_id: 'visual', payload: { resource_id: resource.id, resource: { ...resource, media_path: '../bad' } } }]))).toBeNull();
  });
  it('validates every new resource in a batch against the current canvas before mutation', async () => {
    const command = batch([{ kind: 'bind_media', target_id: 'visual', payload: { resource_id: resource.id, resource } }]);
    await expect(executeProductionCanvasCommand(command, 'project', 'canvas')).rejects.toThrow(/素材/);
    expect(applyProductionCommand).not.toHaveBeenCalled();
  });
  it('registers batch bindings, applies one command and connects the current source', async () => {
    useCanvasStore.setState({ nodes: [...nodes(), videoNode()], edges: [] });
    const command = batch([{ kind: 'edit_beat', target_id: 'beat', payload: { purpose: '新节奏' } }, { kind: 'bind_media', target_id: 'visual', payload: { resource_id: resource.id, resource } }]);
    vi.mocked(attachRemixMedia).mockResolvedValue(media);
    vi.mocked(applyProductionCommand).mockResolvedValue({ plan_ref: { artifact_id: 'plan', revision: 2 }, changed_ids: ['visual'], invalidated: ['render'], blockers: [] });
    vi.mocked(fetchRemixArtifact).mockResolvedValue({ id: 'plan', revision: 2, schema: 'production_plan.v2', payload: { goal: 'batch' } } as never);
    await executeProductionCanvasCommand(command, 'project', 'canvas');
    expect(applyProductionCommand).toHaveBeenCalledTimes(1);
    expect(attachRemixMedia).toHaveBeenCalledWith('project', media.media_path);
    expect(useCanvasStore.getState().edges).toMatchObject([{ source: 'video', target: 'plan-node' }]);
  });
});


describe('production rhythm and read-only preflight', () => {
  beforeEach(() => { vi.clearAllMocks(); scope.current = true; useCanvasStore.setState({ nodes: nodes(), edges: [] }); });
  it('accepts explicit pace and zero pause, rejecting invalid rhythm', () => {
    const edit = (payload: Record<string, unknown>) => ({ ...request(), command: { ...request().command, payload } });
    expect(parseProductionCanvasCommand(edit({ speech_pace: 'slow', extra_pause_seconds: 0 }))).not.toBeNull();
    expect(parseProductionCanvasCommand(edit({ speech_pace: null, extra_pause_seconds: null }))).not.toBeNull();
    for (const payload of [{ speech_pace: 'rapid' }, { speech_pace: ['normal'] }, { extra_pause_seconds: -1 }, { extra_pause_seconds: Infinity }, { extra_pause_seconds: '1' }]) expect(parseProductionCanvasCommand(edit(payload))).toBeNull();
  });
  it('returns business issues without saving or executing', async () => {
    const result = { ok: false, plan_ref: request().command.plan_ref, issues: [{ code: 'speech_source_media_required', path: '/payload/placement_id', entity_id: 'visual', message: '画面尚未绑定实际视频' }], speech_estimates: [], plan_diagnostics: [] };
    vi.mocked(preflightProductionCommand).mockResolvedValue(result);
    expect(await preflightProductionCanvasCommand(request(), 'project', 'canvas')).toEqual(result);
    expect(flushProductionPlanV2AutoSave).not.toHaveBeenCalled();
    expect(applyProductionCommand).not.toHaveBeenCalled();
  });
  it('refuses unsaved changes before requesting preflight', async () => {
    useCanvasStore.getState().updateNodeData('plan-node', { productionPlanV2Dirty: true });
    await expect(preflightProductionCanvasCommand(request(), 'project', 'canvas')).rejects.toThrow(/保存/);
    expect(preflightProductionCommand).not.toHaveBeenCalled();
  });
  it.each(['scope', 'revision', 'dirty', 'draft', 'response'] as const)('rejects obsolete preflight after %s changes', async (change) => {
    vi.mocked(preflightProductionCommand).mockImplementation(async () => {
      if (change === 'scope') scope.current = false;
      if (change === 'revision') useCanvasStore.getState().updateNodeData('plan-node', { productionPlanV2Ref: { artifactId: 'plan', revision: 2 } });
      if (change === 'dirty') useCanvasStore.getState().updateNodeData('plan-node', { productionPlanV2Dirty: true });
      if (change === 'draft') useCanvasStore.getState().updateNodeData('plan-node', { productionPlanV2Draft: { goal: 'changed' } });
      return { ok: true, plan_ref: { artifact_id: 'plan', revision: change === 'response' ? 2 : 1 }, issues: [], speech_estimates: [], plan_diagnostics: [] };
    });
    await expect(preflightProductionCanvasCommand(request(), 'project', 'canvas')).rejects.toThrow(/变化|切换|保存/);
  });
  it('does not turn network failure into successful validation', async () => {
    vi.mocked(preflightProductionCommand).mockRejectedValue(new Error('network unavailable'));
    await expect(preflightProductionCanvasCommand(request(), 'project', 'canvas')).rejects.toThrow('network unavailable');
  });
});


describe('shared production business validation', () => {
  beforeEach(() => { vi.clearAllMocks(); scope.current = true; useCanvasStore.setState({ nodes: nodes(), edges: [] }); });
  const envelopes = () => extractCanvasChatCommandEnvelopes([{ schema_version: 'canvas_chat_commands.v1', project_id: 'project', canvas_id: 'canvas', commands: [request()] }]);
  it('blocks confirmation preparation with one object-specific business issue', async () => {
    vi.mocked(preflightProductionCommand).mockResolvedValue({ ok: false, plan_ref: request().command.plan_ref,
      issues: [{ code: 'speech_source_media_required', path: '/payload/placement_id', entity_id: 'visual', message: '画面 visual 尚未绑定实际视频' }], speech_estimates: [], plan_diagnostics: [] });
    const result = await preflightCanvasChatCommandEnvelopes(envelopes(), useCanvasStore.getState().nodes, [], 'project', 'canvas');
    expect(result.ok).toBe(false);
    expect(result.issues).toHaveLength(1);
    expect(result.issues[0]).toMatchObject({ path: 'envelopes[0].commands[0]/payload/placement_id', message: '画面 visual 尚未绑定实际视频' });
    expect(applyProductionCommand).not.toHaveBeenCalled();
  });
  it('calls business preflight from validate_canvas_commands context requests', async () => {
    vi.mocked(preflightProductionCommand).mockResolvedValue({ ok: true, plan_ref: request().command.plan_ref, issues: [], speech_estimates: [], plan_diagnostics: [] });
    const responses = await buildCanvasContextRequestResponses({ project: 'project', canvasId: 'canvas', nodes: useCanvasStore.getState().nodes, edges: [], ontologyContext: null,
      envelopes: [{ schema_version: 'canvas_context_request.v1', requests: [{ type: 'validate_canvas_commands', payload: envelopes()[0] }] }] as never });
    expect(preflightProductionCommand).toHaveBeenCalledWith('project', request().command);
    expect(responses?.[0]).toMatchObject({ data: { ok: true } });
    expect(applyProductionCommand).not.toHaveBeenCalled();
  });
  it('returns failed validation on network errors and foreign project envelopes', async () => {
    vi.mocked(preflightProductionCommand).mockRejectedValue(new Error('offline'));
    const result = await preflightCanvasChatCommandEnvelopes(envelopes(), useCanvasStore.getState().nodes, [], 'project', 'canvas');
    expect(result).toMatchObject({ ok: false, issues: [{ message: 'offline' }] });
    vi.clearAllMocks();
    const foreign = envelopes(); foreign[0].project_id = 'other';
    expect((await preflightCanvasChatCommandEnvelopes(foreign, useCanvasStore.getState().nodes, [], 'project', 'canvas')).ok).toBe(false);
    expect(preflightProductionCommand).not.toHaveBeenCalled();
  });
});
