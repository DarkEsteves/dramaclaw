import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fetchRemixArtifact } from '@/api/remix';
import { createFreezoneWorkflowRun, getFreezoneCanvas, getFreezoneWorkflowRun, updateFreezoneWorkflowRun } from '@/api/canvas';
import { useCanvasStore } from '@/stores/canvasStore';
import type { CanvasNode } from '../domain/canvasNodes';
import { emptyProductionPlanV2, type ProductionPlanV2 } from './productionPlanV2Model';
import { buildProductionWorkflowAction, hasCurrentProductionProcessingResult, markProductionWorkflowSubmission, reconcileProductionWorkflowResult, recoverProductionWorkflowAction } from './productionWorkflowRuntime';
import { applyCanvasChatCommandsAsync, hasGeneratedResult } from '@/features/freezone/canvasChatCommands';
import { flushProductionPlanV2AutoSave } from './productionPlanV2AutoSave';
import { remixGraphFingerprint } from './prepareNodeRemix';
import { applyRemoteFreezoneCanvas, flushFreezoneCanvasRuntime, getFreezoneCanvasBaseline } from '@/features/freezone/canvasSyncRuntime';

vi.mock('@/api/remix', () => ({ fetchRemixArtifact: vi.fn() }));
vi.mock('@/api/canvas', () => ({ createFreezoneWorkflowRun: vi.fn(), getFreezoneCanvas: vi.fn(), getFreezoneWorkflowRun: vi.fn(), updateFreezoneWorkflowRun: vi.fn() }));
vi.mock('@/api/tasks', () => ({ getProjectTaskLimits: vi.fn(async () => ({})) }));
vi.mock('@/features/freezone/canvasSyncRuntime', () => ({ flushFreezoneCanvasRuntime: vi.fn(async () => true),
  getFreezoneCanvasBaseline: vi.fn(),
  applyRemoteFreezoneCanvas: vi.fn((_project, _canvasId, remote, merge) => {
    const state = useCanvasStore.getState();
    const merged = merge(remote.nodes, remote.edges, state.nodes, state.edges);
    state.setCanvasData(merged.nodes, merged.edges);
    return true;
  }),
  captureFreezoneCanvasScope: vi.fn(() => () => true),
  captureRegisteredFreezoneCanvasSession: vi.fn(() => ({ id: 'test-canvas', isCurrent: () => true })) }));
vi.mock('./productionPlanV2AutoSave', () => ({ flushProductionPlanV2AutoSave: vi.fn(async () => ({ artifactId: 'plan', revision: 2 })) }));

const ref = { artifactId: 'plan', revision: 1 };
const hash = 'a'.repeat(64);
const plan = (): ProductionPlanV2 => ({ schema_version: 2, goal: '有台词横屏片',
  output: { width: 1920, height: 1080, fps_num: 24, fps_den: 1 }, provenance: {},
  beats: [{ id: 'beat', purpose: '开场', duration_frames: 120, turns: [
    { id: 'first', speaker: '甲', text: '你好' }, { id: 'second', speaker: '乙', text: '再见' },
  ] }], resources: [{ id: 'media', node_id: 'video', media_kind: 'video', media_path: 'media/talk.mp4', media_hash: hash, duration_us: 4_000_000, has_audio: true }],
  placements: [{ id: 'visual', kind: 'visual', role: 'primary', track_id: 'visual', resource_id: 'media', start: { kind: 'beat', ref_id: 'beat', edge: 'start' }, end: { kind: 'beat', ref_id: 'beat', edge: 'end' } }],
  speech_takes: [{ id: 'take', beat_id: 'beat', resource_id: 'media', turn_ids: ['first', 'second'], origin_placement_id: 'visual', source_in_us: 0, source_out_us: 4_000_000, speed_num: 1, speed_den: 1, lead_in_frames: 0 }],
});
const node = (id: string, type: CanvasNode['type'], data: Record<string, unknown>): CanvasNode => ({ id, type, data, position: { x: 0, y: 0 } } as CanvasNode);
const nodes = () => [node('plan-node', 'scriptNode', { remixRole: 'production_plan', productionPlanV2Ref: ref, productionPlanV2Draft: plan(), productionPlanV2Dirty: false,
  processingAction: 'align_production_speech', processingPayload: { media_path: 'media/talk.mp4', turn_id: 'first', take_id: 'take' }, remixNodeRevision: 3 }),
  node('compose', 'videoComposeNode', { processingAction: 'compile_production_plan', productionPlanNodeId: 'plan-node', remixNodeRevision: 2 })];
const edge = { id: 'edge', source: 'plan-node', target: 'compose', type: 'composition_input_for' } as never;
const admittedAction = (nodeId: string, action: string) => ({ node_id: nodeId, action,
  status: 'running' as const, processing_operation_id: `operation-${nodeId}`,
  task_key: `native:${nodeId}`, task_id: `task-${nodeId}` });

describe('native production processing workflow', () => {
  beforeEach(() => { vi.clearAllMocks(); useCanvasStore.setState({ nodes: nodes(), edges: [edge] }); });

  it('builds arrangement on the same node with only its exact saved ref', () => {
    const current = useCanvasStore.getState();
    const descriptor = buildProductionWorkflowAction(current.nodes[0], current.nodes, [], {
      action: 'generate_production_plan_arrangement', parameters: { plan_ref: { artifact_id: 'plan', revision: 1 } },
    });
    expect(descriptor).toMatchObject({ action: 'generate_production_plan_arrangement', planNodeId: 'plan-node',
      payload: { plan_ref: { artifact_id: 'plan', revision: 1 } } });
    expect(descriptor?.blocked).toBeUndefined();
    expect(Object.keys(descriptor!.payload)).toEqual(['plan_ref']);
  });

  it('strips command routing keys from override parameters before building the payload', () => {
    useCanvasStore.setState({ nodes: [node('plan-node', 'scriptNode', {
      remixRole: 'production_plan', processingAction: 'generate_production_plan_draft',
      processingPayload: { topic: '旧目标' },
      productionPlanV2Draft: emptyProductionPlanV2('旧目标'), productionPlanV2Dirty: false,
      productionPlanV2Conflict: false, remixNodeRevision: 1 })], edges: [] });
    const current = useCanvasStore.getState();
    const descriptor = buildProductionWorkflowAction(current.nodes[0], current.nodes, current.edges, {
      action: 'generate_production_plan_draft',
      parameters: { node_id: 'plan-node', action: 'generate_production_plan_draft', topic: '黑咖啡' },
    });
    expect(descriptor?.payload).toEqual({ topic: '黑咖啡' });
    expect(descriptor?.payload).not.toHaveProperty('node_id');
    expect(descriptor?.payload).not.toHaveProperty('action');
  });

  it('keeps an empty plan node goal and action version aligned for canvas and Agent edits', () => {
    useCanvasStore.setState({ nodes: [node('plan-node', 'scriptNode', {
      remixRole: 'production_plan', processingAction: 'generate_production_plan_draft',
      processingPayload: { topic: '旧目标', analysis_context: { summary: '原片' },
        requirements: { dialogue: { mode: 'preserve_source' } },
        requirements_fingerprint: 'old-direction' },
      productionPlanV2Draft: emptyProductionPlanV2('旧目标'), productionPlanV2Dirty: false,
      remixNodeRevision: 1,
    })], edges: [] });
    const store = useCanvasStore.getState();
    const before = buildProductionWorkflowAction(store.nodes[0], store.nodes, store.edges)!;
    store.updateNodeData('plan-node', { processingPayload: { topic: '新目标', analysis_context: { summary: '原片' } } });
    const after = useCanvasStore.getState().nodes[0];
    expect(after.data.remixNodeRevision).toBe(2);
    expect((after.data.productionPlanV2Draft as ProductionPlanV2).goal).toBe('新目标');
    expect(after.data.processingPayload).not.toHaveProperty('requirements');
    expect(after.data.processingPayload).not.toHaveProperty('requirements_fingerprint');
    expect(buildProductionWorkflowAction(after, [after], [])).toMatchObject({ nodeRevision: 2, payload: { topic: '新目标' } });
    expect(before.snapshot.nodeData).not.toBe(buildProductionWorkflowAction(after, [after], [])?.snapshot.nodeData);

    const edited = structuredClone(after.data.productionPlanV2Draft as ProductionPlanV2);
    edited.goal = '用户在画布修改的目标';
    useCanvasStore.getState().updateNodeData('plan-node', { productionPlanV2Draft: edited, productionPlanV2Dirty: true });
    const manual = useCanvasStore.getState().nodes[0].data;
    expect(manual.processingPayload).toMatchObject({ topic: '用户在画布修改的目标' });
    expect(manual.remixNodeRevision).toBe(3);
  });

  it('uses the exact current plan revision and defers compile when there is no saved plan', () => {
    const current = useCanvasStore.getState();
    expect(buildProductionWorkflowAction(current.nodes[1], current.nodes, current.edges)).toMatchObject({ action: 'compile_production_plan', nodeRevision: 2, payload: { plan_ref: { artifact_id: 'plan', revision: 1 } } });
    current.updateNodeData('plan-node', { productionPlanV2Ref: null });
    const missing = buildProductionWorkflowAction(useCanvasStore.getState().nodes[1], useCanvasStore.getState().nodes, useCanvasStore.getState().edges);
    expect(missing?.blocked).toMatch(/方案|版本/);
  });

  it('attaches a V2 draft to an editable plan and its own compose node', async () => {
    useCanvasStore.setState({ nodes: [node('plan-node', 'scriptNode', { remixRole: 'production_plan', processingAction: 'generate_production_plan_draft', processingPayload: { topic: '新片', analysis_context: { summary: '原片叙事' } }, remixNodeRevision: 1 }), nodes()[1]], edges: [edge] });
    const current = useCanvasStore.getState();
    const descriptor = buildProductionWorkflowAction(current.nodes[0], current.nodes, current.edges)!;
    vi.mocked(fetchRemixArtifact).mockResolvedValue({ id: 'new-plan', revision: 1, schema: 'production_plan.v2', payload: plan() as never });
    const outcome = await reconcileProductionWorkflowResult('project', descriptor, { artifact_id: 'new-plan', revision: 1 }, () => true);
    expect(outcome.status).toBe('ready');
    expect(useCanvasStore.getState().nodes[0].data).toMatchObject({ productionPlanV2Ref: { artifactId: 'new-plan', revision: 1 }, productionPlanV2Draft: { goal: '有台词横屏片' }, productionPlanV2Dirty: false });
    expect(useCanvasStore.getState().nodes[1].data).toMatchObject({ productionPlanNodeId: 'plan-node', productionPlanV2Ref: { artifactId: 'new-plan', revision: 1 } });
    expect(hasCurrentProductionProcessingResult(useCanvasStore.getState().nodes[0], 'generate_production_plan_draft', useCanvasStore.getState().nodes, useCanvasStore.getState().edges)).toBe(true);
    useCanvasStore.getState().updateNodeData('plan-node', { processingPayload: { topic: '改过的目标' } });
    expect(hasCurrentProductionProcessingResult(useCanvasStore.getState().nodes[0], 'generate_production_plan_draft', useCanvasStore.getState().nodes, useCanvasStore.getState().edges)).toBe(false);
    expect(buildProductionWorkflowAction(useCanvasStore.getState().nodes[0], useCanvasStore.getState().nodes, useCanvasStore.getState().edges)?.blocked).toMatch(/已存在/);
  });

  it('accepts the plan node mounting its untouched empty editor while generation runs', async () => {
    useCanvasStore.setState({ nodes: [node('plan-node', 'scriptNode', { remixRole: 'production_plan',
      processingAction: 'generate_production_plan_draft', processingPayload: { topic: '黑咖啡', analysis_context: { summary: '访谈' } },
      remixNodeRevision: 1 })], edges: [] });
    const state = useCanvasStore.getState();
    const descriptor = buildProductionWorkflowAction(state.nodes[0], state.nodes, state.edges)!;
    state.updateNodeData('plan-node', { productionPlanV2Draft: emptyProductionPlanV2('黑咖啡'), productionPlanV2Dirty: false });
    vi.mocked(fetchRemixArtifact).mockResolvedValue({ id: 'generated', revision: 1, schema: 'production_plan.v2', payload: plan() as never });
    await expect(reconcileProductionWorkflowResult('project', descriptor, { artifact_id: 'generated', revision: 1 }, () => true)).resolves.toMatchObject({ status: 'ready' });
    expect(useCanvasStore.getState().nodes[0].data.productionPlanV2Ref).toEqual({ artifactId: 'generated', revision: 1 });
  });

  it('does not publish a completed plan from the browser without a canvas identity', async () => {
    useCanvasStore.setState({ nodes: [node('plan-node', 'scriptNode', { remixRole: 'production_plan',
      processingAction: 'generate_production_plan_draft', processingPayload: { topic: '黑咖啡', analysis_context: { summary: '访谈' } },
      remixNodeRevision: 1 })], edges: [] });
    const state = useCanvasStore.getState();
    const descriptor = buildProductionWorkflowAction(state.nodes[0], state.nodes, state.edges)!;
    markProductionWorkflowSubmission(descriptor);
    state.updateNodeData('plan-node', { productionPlanV2Draft: emptyProductionPlanV2('黑咖啡'), productionPlanV2Dirty: false,
      productionPlanV2Conflict: true, productionPlanV2SaveError: '处理期间作品方案或素材已变化；结果已保留，请重新核对版本' });
    vi.mocked(fetchRemixArtifact).mockResolvedValue({ id: 'generated', revision: 1, schema: 'production_plan.v2', payload: plan() as never });
    const action = { node_id: 'plan-node', action: 'generate_production_plan_draft', status: 'completed',
      artifact_status: 'valid', node_revision: 1, payload: descriptor.payload,
      result_ref: { artifact_id: 'generated', revision: 1 } } as never;
    expect(await recoverProductionWorkflowAction('project', action, () => true)).toBe('retry');
    expect(useCanvasStore.getState().nodes[0].data.productionPlanV2Ref).toBeUndefined();
  });

  it('recovery refreshes the host published plan and keeps unrelated local edits', async () => {
    useCanvasStore.setState({ nodes: [node('plan-node', 'scriptNode', { remixRole: 'production_plan',
      processingAction: 'generate_production_plan_draft', processingPayload: { topic: '黑咖啡' }, remixNodeRevision: 1 }),
      node('note', 'scriptNode', { text: '原文' })], edges: [] });
    const baseline = structuredClone(useCanvasStore.getState().nodes);
    vi.mocked(getFreezoneCanvasBaseline).mockReturnValue({ nodes: baseline, edges: [], revision: 1 });
    useCanvasStore.getState().updateNodeData('note', { text: '本地备注' });
    vi.mocked(getFreezoneCanvas).mockResolvedValue({ nodes: [
      { ...baseline[0], data: { ...baseline[0].data, productionPlanV2Ref: { artifactId: 'generated', revision: 1 },
        productionPlanV2Draft: plan(), productionPlanV2Dirty: false } }, baseline[1],
    ], edges: [], revision: 2 } as never);
    const action = { node_id: 'plan-node', action: 'generate_production_plan_draft', status: 'completed',
      artifact_status: 'valid', result_ref: { artifact_id: 'generated', revision: 1 } } as never;
    expect(await recoverProductionWorkflowAction('project', action, () => true, 'canvas')).toBe('ready');
    expect(getFreezoneCanvas).toHaveBeenCalledWith('project', 'canvas');
    expect(applyRemoteFreezoneCanvas).toHaveBeenCalled();
    expect(fetchRemixArtifact).not.toHaveBeenCalled();
    expect(useCanvasStore.getState().nodes.find((item) => item.id === 'plan-node')?.data.productionPlanV2Ref)
      .toEqual({ artifactId: 'generated', revision: 1 });
    expect(useCanvasStore.getState().nodes.find((item) => item.id === 'note')?.data).toMatchObject({ text: '本地备注' });
  });

  it('recovery does not overwrite a locally edited plan with a host result', async () => {
    useCanvasStore.setState({ nodes: [node('plan-node', 'scriptNode', { remixRole: 'production_plan',
      processingAction: 'generate_production_plan_draft', processingPayload: { topic: '黑咖啡' }, remixNodeRevision: 1 })], edges: [] });
    const baseline = structuredClone(useCanvasStore.getState().nodes);
    vi.mocked(getFreezoneCanvasBaseline).mockReturnValue({ nodes: baseline, edges: [], revision: 1 });
    useCanvasStore.getState().updateNodeData('plan-node', { productionPlanV2Draft: { ...plan(), goal: '用户新目标' }, productionPlanV2Dirty: true });
    vi.mocked(getFreezoneCanvas).mockResolvedValue({ nodes: [{ ...baseline[0], data: { ...baseline[0].data,
      productionPlanV2Ref: { artifactId: 'generated', revision: 1 }, productionPlanV2Draft: plan() } }], edges: [], revision: 2 } as never);
    const action = { node_id: 'plan-node', action: 'generate_production_plan_draft', status: 'completed',
      artifact_status: 'valid', result_ref: { artifact_id: 'generated', revision: 1 } } as never;
    await expect(recoverProductionWorkflowAction('project', action, () => true, 'canvas')).resolves.toBe('stale');
    expect(useCanvasStore.getState().nodes[0].data.productionPlanV2Ref).toBeUndefined();
    expect(useCanvasStore.getState().nodes[0].data.productionPlanV2Draft).toMatchObject({ goal: '用户新目标' });
  });

  it('replaces only an unchanged empty editable plan after goal based drafting', async () => {
    const empty = { schema_version: 2, goal: '黑咖啡广告', output: { width: 1080, height: 1920, fps_num: 30, fps_den: 1 }, beats: [], resources: [], placements: [], provenance: {} };
    const story = node('story', 'videoStoryNode', { rawResult: { video_story: { summary: '访谈反转' } } });
    const planNode = node('plan-node', 'scriptNode', { remixRole: 'production_plan', productionPlanV2Ref: ref,
      productionPlanV2Draft: empty, productionPlanV2Dirty: false });
    const storyEdge = { id: 'story-edge', source: 'story', target: 'plan-node', type: 'context_for' } as never;
    useCanvasStore.setState({ nodes: [story, planNode], edges: [storyEdge] });
    const state = useCanvasStore.getState();
    const descriptor = buildProductionWorkflowAction(state.nodes[1], state.nodes, state.edges,
      { action: 'generate_production_plan_draft', parameters: { topic: '黑咖啡广告' } })!;
    expect(descriptor.blocked).toBeUndefined();
    vi.mocked(fetchRemixArtifact).mockResolvedValue({ id: 'drafted', revision: 1, schema: 'production_plan.v2', payload: plan() as never });
    await reconcileProductionWorkflowResult('project', descriptor, { artifact_id: 'drafted', revision: 1 }, () => true);
    expect(useCanvasStore.getState().nodes[1].data.productionPlanV2Ref).toEqual({ artifactId: 'drafted', revision: 1 });
    const current = useCanvasStore.getState();
    expect(buildProductionWorkflowAction(current.nodes[1], current.nodes, current.edges,
      { action: 'generate_production_plan_draft', parameters: { topic: '再改一次' } })?.blocked).toMatch(/已有|新建/);
  });

  it('preserves edits made while the draft artifact is loading', async () => {
    useCanvasStore.setState({ nodes: [node('plan-node', 'scriptNode', { remixRole: 'production_plan', processingAction: 'generate_production_plan_draft', processingPayload: { topic: '新片', analysis_context: { summary: '原片叙事' } }, remixNodeRevision: 1 }), nodes()[1]], edges: [edge] });
    const current = useCanvasStore.getState();
    const descriptor = buildProductionWorkflowAction(current.nodes[0], current.nodes, current.edges)!;
    vi.mocked(fetchRemixArtifact).mockImplementation(async () => {
      useCanvasStore.getState().updateNodeData('plan-node', { productionPlanV2Draft: plan(), productionPlanV2Dirty: true });
      return { id: 'new-plan', revision: 1, schema: 'production_plan.v2', payload: plan() as never };
    });
    await expect(reconcileProductionWorkflowResult('project', descriptor, { artifact_id: 'new-plan', revision: 1 }, () => true)).rejects.toThrow(/本地修改|版本/);
    expect(useCanvasStore.getState().nodes[0].data).toMatchObject({ productionPlanV2Dirty: true, productionPlanV2Conflict: true });
  });

  it('uses completed VideoStory analysis and rejects a changed reference while the result is loading', async () => {
    const story = node('story', 'videoStoryNode', { sourceVideoUrl: 'media/source.mp4', rawResult: {
      video_story: { summary: '先提问再反转', adaptation_structure: { narrative_role: '悬念', reusable_beats: ['提问', '反转'] } },
    } });
    const planNode = node('plan-node', 'scriptNode', { remixRole: 'production_plan', processingAction: 'generate_production_plan_draft',
      processingPayload: { topic: '新片' }, remixNodeRevision: 1 });
    const storyEdge = { id: 'story-edge', source: 'story', target: 'plan-node', type: 'context_for' } as never;
    useCanvasStore.setState({ nodes: [story, planNode], edges: [storyEdge] });
    const state = useCanvasStore.getState();
    const descriptor = buildProductionWorkflowAction(state.nodes[1], state.nodes, state.edges)!;
    expect(descriptor.payload.analysis_context).toMatchObject({ summary: '先提问再反转' });
    expect(descriptor.blocked).toBeUndefined();
    vi.mocked(fetchRemixArtifact).mockImplementation(async () => {
      useCanvasStore.getState().updateNodeData('story', { sourceVideoUrl: 'media/new-source.mp4' });
      return { id: 'new-plan', revision: 1, schema: 'production_plan.v2', payload: plan() as never };
    });
    await expect(reconcileProductionWorkflowResult('project', descriptor, { artifact_id: 'new-plan', revision: 1 }, () => true)).rejects.toThrow(/本地修改|版本/);
    expect(useCanvasStore.getState().nodes[1].data.productionPlanV2Ref).toBeUndefined();
  });

  it('keeps a mismatch as review evidence and blocks automatic compile', async () => {
    const current = useCanvasStore.getState();
    const descriptor = buildProductionWorkflowAction(current.nodes[0], current.nodes, current.edges)!;
    vi.mocked(fetchRemixArtifact).mockResolvedValue({ id: 'asr', revision: 1, schema: 'word_timeline.v1', upstream_refs: [{ artifact_id: 'plan', revision: 1 }], payload: {
      status: 'script_mismatch', turn_id: 'first', placement_id: 'visual', media_hash: `sha256:${hash}`, source_in_us: 0, source_out_us: 4_000_000, group_turn_ids: ['first', 'second'],
      mismatch: { report: { normalized_asr: '别的台词' } },
    } as never });
    const outcome = await reconcileProductionWorkflowResult('project', descriptor, { artifact_id: 'asr', revision: 1 }, () => true);
    expect(outcome.status).toBe('review');
    expect(useCanvasStore.getState().nodes[0].data.productionSpeechReviews).toMatchObject({ first: { status: 'script_mismatch', artifactRef: { artifactId: 'asr', revision: 1 } } });
    const compose = useCanvasStore.getState().nodes[1];
    expect(buildProductionWorkflowAction(compose, useCanvasStore.getState().nodes, useCanvasStore.getState().edges)?.blocked).toMatch(/台词/);
  });

  it('ignores old speech reviews after the turn is removed or its source is rebound', () => {
    const firstReview = { status: 'script_mismatch', placementId: 'visual', mediaHash: hash,
      text: '你好', sourceInUs: 0, sourceOutUs: 4_000_000 };
    const state = useCanvasStore.getState();
    state.updateNodeData('plan-node', { productionSpeechReviews: { first: firstReview } });
    expect(buildProductionWorkflowAction(state.nodes[1], useCanvasStore.getState().nodes, state.edges)?.blocked).toMatch(/台词/);

    const rebound = plan();
    rebound.resources.push({ id: 'replacement', node_id: 'other-video', media_kind: 'video', media_path: 'media/new.mp4',
      media_hash: 'b'.repeat(64), duration_us: 5_000_000, has_audio: true });
    rebound.speech_takes![0].resource_id = 'replacement';
    state.updateNodeData('plan-node', { productionPlanV2Draft: rebound });
    expect(buildProductionWorkflowAction(useCanvasStore.getState().nodes[1], useCanvasStore.getState().nodes, state.edges)?.blocked).toBeUndefined();

    const removed = plan();
    removed.beats[0].turns = [removed.beats[0].turns[1]];
    removed.speech_takes![0].turn_ids = ['second'];
    state.updateNodeData('plan-node', { productionPlanV2Draft: removed });
    expect(buildProductionWorkflowAction(useCanvasStore.getState().nodes[1], useCanvasStore.getState().nodes, state.edges)?.blocked).toBeUndefined();
  });

  it('lets the Agent compile manually corrected ready cues despite an old ASR mismatch', async () => {
    const corrected = plan();
    corrected.beats[0].turns = [corrected.beats[0].turns[0]];
    corrected.speech_takes![0].turn_ids = ['first'];
    corrected.beats[0].turns[0].speech_timing = { placement_id: 'visual', media_hash: hash,
      text: '你好', text_hash: 'c'.repeat(64), source_in_us: 0, source_out_us: 4_000_000,
      evidence_ref: { artifact_id: 'corrected-srt', revision: 1 },
      words: [{ id: 'first:spoken:corrected:0', text: '你好', start_us: 100_000, end_us: 300_000 }] };
    corrected.speech_cues = [{ id: 'cue-first', turn_id: 'first', take_id: 'take', text: '你好',
      source_start_us: 100_000, source_end_us: 300_000, timing_source: 'manual', timing_status: 'ready',
      word_ids: ['first:spoken:corrected:0'], evidence_ref: { artifact_id: 'corrected-srt', revision: 1 } }];
    const state = useCanvasStore.getState();
    state.updateNodeData('plan-node', { productionPlanV2Draft: corrected,
      productionSpeechReviews: { first: { status: 'script_mismatch', placementId: 'visual', mediaHash: hash,
        text: '你好', sourceInUs: 0, sourceOutUs: 4_000_000 } } });
    expect(buildProductionWorkflowAction(useCanvasStore.getState().nodes[1], useCanvasStore.getState().nodes, state.edges)?.blocked).toBeUndefined();
    const base = { schema_version: 'freezone_workflow_run.v1' as const, run_id: 'run-corrected', project_id: 'project', canvas_id: 'canvas',
      resumable: false, created_at: '', started_at: '', updated_at: '', actions: [] };
    const completed = { ...base, status: 'completed' as const, actions: [{ node_id: 'compose', action: 'compile_production_plan', status: 'completed' as const,
      artifact_status: 'valid' as const, result_ref: { artifact_id: 'manifest', revision: 1 } }] };
    vi.mocked(createFreezoneWorkflowRun).mockResolvedValue({ ...base, status: 'running', actions: [admittedAction('compose', 'compile_production_plan')] });
    vi.mocked(getFreezoneWorkflowRun).mockResolvedValue(completed);
    vi.mocked(updateFreezoneWorkflowRun).mockResolvedValue(completed);
    vi.mocked(fetchRemixArtifact).mockResolvedValue({ id: 'manifest', revision: 1, schema: 'composition_manifest.v2',
      upstream_refs: [{ artifact_id: 'plan', revision: 1 }], payload: {} as never });
    const result = await applyCanvasChatCommandsAsync([{ schema_version: 'canvas_chat_commands.v1', project_id: 'project', canvas_id: 'canvas',
      commands: [{ type: 'run_node_action', node_id: 'compose', action: 'compile_production_plan' }] }],
    { projectId: 'project', canvasId: 'canvas', actionTimeoutMs: 2000 });
    expect(result.errors).toEqual([]);
    expect(createFreezoneWorkflowRun).toHaveBeenCalledWith('project', 'canvas',
      [expect.objectContaining({ action: 'compile_production_plan' })], expect.any(String), expect.any(String));
  });

  it('rejects a changed speech source before submitting the action', () => {
    const current = useCanvasStore.getState();
    current.updateNodeData('plan-node', { processingPayload: { media_path: 'media/other.mp4', turn_id: 'first', take_id: 'take' } });
    const latest = useCanvasStore.getState();
    expect(buildProductionWorkflowAction(latest.nodes[0], latest.nodes, latest.edges)?.blocked).toMatch(/素材已变化/);
  });

  it('accepts all turns from one shared source and preserves word identity', async () => {
    const current = useCanvasStore.getState();
    const descriptor = buildProductionWorkflowAction(current.nodes[0], current.nodes, current.edges)!;
    const makeArtifact = (id: string, turnId: string, text: string, start: number) => ({ id, revision: 1, schema: 'word_timeline.v1', upstream_refs: [{ artifact_id: 'plan', revision: 1 }], payload: {
      status: 'completed', turn_id: turnId, placement_id: 'visual', media_hash: `sha256:${hash}`, source_in_us: 0, source_out_us: 4_000_000,
      transcript: text, text_hash: 'b'.repeat(64), words: [{ text, start_us: start, end_us: start + 200_000 }],
      ...(turnId === 'first' ? { group_turn_ids: ['first', 'second'], group_refs: [{ turn_id: 'second', artifact_id: 'asr-second', revision: 1 }] } : {}),
    } as never });
    vi.mocked(fetchRemixArtifact).mockImplementation(async (_project, resultRef) => resultRef.artifactId === 'asr-first' ? makeArtifact('asr-first', 'first', '你好', 100_000) : makeArtifact('asr-second', 'second', '再见', 600_000));
    const outcome = await reconcileProductionWorkflowResult('project', descriptor, { artifact_id: 'asr-first', revision: 1 }, () => true);
    expect(outcome.status).toBe('ready');
    const draft = useCanvasStore.getState().nodes[0].data.productionPlanV2Draft as ProductionPlanV2;
    expect(draft.beats[0].turns.map((turn) => turn.speech_timing?.words[0].id)).toEqual(['first:spoken:asr-first:1:0', 'second:spoken:asr-second:1:0']);
  });

  it('accepts both speakers when the shared source is selected by placement only', async () => {
    const draft = plan();
    draft.speech_takes = [];
    draft.beats[0].turns[0].speech_placement_id = 'visual';
    draft.beats[0].turns[1].speech_placement_id = 'visual';
    useCanvasStore.getState().updateNodeData('plan-node', { productionPlanV2Draft: draft,
      processingPayload: { turn_id: 'first', placement_id: 'visual', media_path: 'media/talk.mp4' } });
    const state = useCanvasStore.getState();
    const descriptor = buildProductionWorkflowAction(state.nodes[0], state.nodes, state.edges)!;
    expect(descriptor.blocked).toBeUndefined();
    const artifact = (id: string, turnId: string, text: string) => ({ id, revision: 1, schema: 'word_timeline.v1',
      upstream_refs: [{ artifact_id: 'plan', revision: 1 }], payload: {
        status: 'completed', turn_id: turnId, placement_id: 'visual', media_hash: `sha256:${hash}`,
        source_in_us: 0, source_out_us: 4_000_000, transcript: text, text_hash: 'b'.repeat(64),
        words: [{ text, start_us: 100_000, end_us: 200_000 }],
        ...(turnId === 'first' ? { group_turn_ids: ['first', 'second'], group_refs: [{ turn_id: 'second', artifact_id: 'asr-second', revision: 1 }] } : {}),
      } as never });
    vi.mocked(fetchRemixArtifact).mockImplementation(async (_project, resultRef) => resultRef.artifactId === 'asr-first'
      ? artifact('asr-first', 'first', '你好') : artifact('asr-second', 'second', '再见'));
    await expect(reconcileProductionWorkflowResult('project', descriptor, { artifact_id: 'asr-first', revision: 1 }, () => true)).resolves.toMatchObject({ status: 'ready' });
    expect((useCanvasStore.getState().nodes[0].data.productionPlanV2Draft as ProductionPlanV2).beats[0].turns.every((turn) => turn.speech_timing?.words.length)).toBe(true);
  });

  it('attaches a compiled manifest only to its owning plan and compose node', async () => {
    const current = useCanvasStore.getState();
    const descriptor = buildProductionWorkflowAction(current.nodes[1], current.nodes, current.edges)!;
    vi.mocked(fetchRemixArtifact).mockResolvedValue({ id: 'manifest', revision: 1, schema: 'composition_manifest.v2', upstream_refs: [{ artifact_id: 'plan', revision: 1 }], payload: { source_versions: [{ artifact_id: 'plan', revision: 1 }] } as never });
    const outcome = await reconcileProductionWorkflowResult('project', descriptor, { artifact_id: 'manifest', revision: 1 }, () => true);
    expect(outcome.status).toBe('ready');
    expect(useCanvasStore.getState().nodes[1].data).toMatchObject({ remixManifestRef: { artifactId: 'manifest', revision: 1 }, productionPlanNodeId: 'plan-node', productionPlanV2Ref: ref });
  });

  it('submits arrangement through the existing explicit node action flow without dependencies', async () => {
    const current = nodes()[0];
    current.data.processingAction = 'generate_production_plan_draft';
    useCanvasStore.setState({ nodes: [current], edges: [] });
    vi.mocked(createFreezoneWorkflowRun).mockResolvedValue({ run_id: 'arrange-run', status: 'running',
      actions: [admittedAction('plan-node', 'generate_production_plan_arrangement')] } as never);
    const result = await applyCanvasChatCommandsAsync([{ schema_version: 'canvas_chat_commands.v1',
      project_id: 'project', canvas_id: 'canvas', commands: [{ type: 'run_node_action', node_id: 'plan-node',
        action: 'generate_production_plan_arrangement', parameters: { plan_ref: { artifact_id: 'plan', revision: 1 } } }] }],
      { projectId: 'project', canvasId: 'canvas', actionTimeoutMs: 2000 });
    expect(result.errors).toEqual([]);
    expect(createFreezoneWorkflowRun).toHaveBeenCalledWith('project', 'canvas', [expect.objectContaining({
      action: 'generate_production_plan_arrangement', node_id: 'plan-node', depends_on: [],
      payload: { plan_ref: { artifact_id: 'plan', revision: 1 } },
    })], expect.any(String), expect.any(String));
    expect(useCanvasStore.getState().nodes[0].data.productionWorkflowSubmission).toMatchObject({
      action: 'generate_production_plan_arrangement', planRef: ref, payload: { plan_ref: { artifact_id: 'plan', revision: 1 } },
    });
  });

  it('reconciles arrangement as a new revision of the same editable plan', async () => {
    const state = useCanvasStore.getState();
    const descriptor = buildProductionWorkflowAction(state.nodes[0], state.nodes, [], { action: 'generate_production_plan_arrangement' })!;
    vi.mocked(fetchRemixArtifact).mockResolvedValue({ id: 'plan', revision: 2, schema: 'production_plan.v2', payload: { ...plan(), authoring_stage: 'arranged' } as never });
    await reconcileProductionWorkflowResult('project', descriptor, { artifact_id: 'plan', revision: 2 }, () => true);
    expect(useCanvasStore.getState().nodes[0].data.productionPlanV2Ref).toEqual({ artifactId: 'plan', revision: 2 });
    expect(useCanvasStore.getState().nodes[0].data.productionPlanV2Draft).toMatchObject({ authoring_stage: 'arranged' });
  });

  it('submits the registered draft without browser artifact synchronization', async () => {
    useCanvasStore.setState({ nodes: [node('plan-node', 'scriptNode', { remixRole: 'production_plan', processingAction: 'generate_production_plan_draft', processingPayload: { topic: '新片', analysis_context: { summary: '原片叙事' } }, remixNodeRevision: 1 })], edges: [] });
    const base = { schema_version: 'freezone_workflow_run.v1' as const, run_id: 'run-1', project_id: 'project', canvas_id: 'canvas',
      resumable: true, created_at: '', started_at: '', updated_at: '', actions: [] };
    vi.mocked(createFreezoneWorkflowRun).mockResolvedValue({ ...base, status: 'running', actions: [admittedAction('plan-node', 'generate_production_plan_draft')] });
    vi.mocked(getFreezoneWorkflowRun).mockResolvedValue({ ...base, status: 'completed', actions: [{ node_id: 'plan-node', action: 'generate_production_plan_draft', status: 'completed', artifact_status: 'valid', result_ref: { artifact_id: 'new-plan', revision: 1 } }] });
    vi.mocked(updateFreezoneWorkflowRun).mockResolvedValue({ ...base, status: 'completed', actions: [{ node_id: 'plan-node', action: 'generate_production_plan_draft', status: 'completed', artifact_status: 'valid', result_ref: { artifact_id: 'new-plan', revision: 1 } }] });
    vi.mocked(fetchRemixArtifact).mockResolvedValue({ id: 'new-plan', revision: 1, schema: 'production_plan.v2', payload: plan() as never });
    const result = await applyCanvasChatCommandsAsync([{ schema_version: 'canvas_chat_commands.v1', project_id: 'project', canvas_id: 'canvas',
      commands: [{ type: 'run_workflow', node_ids: ['plan-node'], scope: 'selection', direction: 'node' }] }], { projectId: 'project', canvasId: 'canvas', actionTimeoutMs: 2000 });
    expect(result.errors).toEqual([]);
    expect(createFreezoneWorkflowRun).toHaveBeenCalledWith('project', 'canvas', [expect.objectContaining({
      node_id: 'plan-node', action: 'generate_production_plan_draft', node_revision: 1, payload: { topic: '新片', analysis_context: { summary: '原片叙事' } }, depends_on: [],
    })], expect.any(String), expect.any(String));
    expect(fetchRemixArtifact).not.toHaveBeenCalled();
    expect(useCanvasStore.getState().nodes[0].data.productionPlanV2Ref).toBeUndefined();
    expect(result.commandResults).toEqual(expect.arrayContaining([expect.objectContaining({
      status: 'pending', output: expect.objectContaining({ reason: 'native_action_accepted', task_id: 'task-plan-node' }),
    })]));
  });

  it('lets the agent calibrate an existing plan through an explicit action without rewriting node processing data', async () => {
    const base = { schema_version: 'freezone_workflow_run.v1' as const, run_id: 'run-1', project_id: 'project', canvas_id: 'canvas',
      resumable: true, created_at: '', started_at: '', updated_at: '', actions: [] };
    vi.mocked(createFreezoneWorkflowRun).mockResolvedValue({ ...base, status: 'running', actions: [admittedAction('plan-node', 'align_production_speech')] });
    vi.mocked(getFreezoneWorkflowRun).mockResolvedValue({ ...base, status: 'failed', actions: [{ node_id: 'plan-node', action: 'align_production_speech', status: 'blocked', artifact_status: 'needs_review', result_ref: { artifact_id: 'asr', revision: 1 } }] });
    vi.mocked(updateFreezoneWorkflowRun).mockResolvedValue({ ...base, status: 'failed', actions: [{ node_id: 'plan-node', action: 'align_production_speech', status: 'blocked', artifact_status: 'needs_review', result_ref: { artifact_id: 'asr', revision: 1 } }] });
    vi.mocked(fetchRemixArtifact).mockResolvedValue({ id: 'asr', revision: 1, schema: 'word_timeline.v1', upstream_refs: [{ artifact_id: 'plan', revision: 1 }], payload: {
      status: 'script_mismatch', turn_id: 'first', placement_id: 'visual', media_hash: `sha256:${hash}`,
      source_in_us: 0, source_out_us: 4_000_000, group_turn_ids: ['first', 'second'],
    } as never });
    const result = await applyCanvasChatCommandsAsync([{ schema_version: 'canvas_chat_commands.v1', project_id: 'project', canvas_id: 'canvas',
      commands: [{ type: 'run_node_action', node_id: 'plan-node', action: 'align_production_speech',
        parameters: { turn_id: 'first', take_id: 'take' } }] }], { projectId: 'project', canvasId: 'canvas', actionTimeoutMs: 2000 });
    expect(result.errors).toEqual([]);
    expect(createFreezoneWorkflowRun).toHaveBeenCalledWith('project', 'canvas', [expect.objectContaining({
      node_id: 'plan-node', action: 'align_production_speech', node_revision: 3,
      payload: { turn_id: 'first', take_id: 'take', media_path: 'media/talk.mp4', plan_ref: { artifact_id: 'plan', revision: 1 } },
    })], expect.any(String), expect.any(String));
    expect(useCanvasStore.getState().nodes[0].data.productionSpeechReviews).toBeUndefined();
    expect(fetchRemixArtifact).not.toHaveBeenCalled();
  });

  it('does not submit the same explicit compile twice', async () => {
    const base = { schema_version: 'freezone_workflow_run.v1' as const, run_id: 'run-compile', project_id: 'project', canvas_id: 'canvas',
      resumable: false, created_at: '', started_at: '', updated_at: '', actions: [] };
    const completed = { ...base, status: 'completed' as const, actions: [{ node_id: 'compose', action: 'compile_production_plan', status: 'completed' as const,
      artifact_status: 'valid' as const, result_ref: { artifact_id: 'manifest', revision: 1 } }] };
    vi.mocked(createFreezoneWorkflowRun).mockResolvedValue({ ...base, status: 'running', actions: [admittedAction('compose', 'compile_production_plan')] });
    vi.mocked(getFreezoneWorkflowRun).mockResolvedValue(completed);
    vi.mocked(updateFreezoneWorkflowRun).mockResolvedValue(completed);
    vi.mocked(fetchRemixArtifact).mockResolvedValue({ id: 'manifest', revision: 1, schema: 'composition_manifest.v2',
      upstream_refs: [{ artifact_id: 'plan', revision: 1 }], payload: {} as never });
    const envelope = [{ schema_version: 'canvas_chat_commands.v1' as const, project_id: 'project', canvas_id: 'canvas',
      commands: [{ type: 'run_node_action' as const, node_id: 'compose', action: 'compile_production_plan' }] }];
    const first = await applyCanvasChatCommandsAsync(envelope, { projectId: 'project', canvasId: 'canvas', actionTimeoutMs: 2000 });
    expect(first.errors).toEqual([]);
    expect(createFreezoneWorkflowRun).toHaveBeenCalledTimes(1);
    const current = useCanvasStore.getState();
    const descriptor = buildProductionWorkflowAction(current.nodes[1], current.nodes, current.edges,
      { action: 'compile_production_plan' })!;
    current.updateNodeData('compose', { productionPlanNodeId: 'plan-node', productionPlanV2Ref: ref,
      productionWorkflowAction: 'compile_production_plan', productionWorkflowInputs: descriptor.snapshot.nodeData,
      productionWorkflowRequestPayload: descriptor.requestPayload,
      remixManifestRef: { artifactId: 'manifest', revision: 1 }, remixInputsCurrent: true });
    const second = await applyCanvasChatCommandsAsync(envelope, { projectId: 'project', canvasId: 'canvas', actionTimeoutMs: 2000 });
    expect(second.errors).toEqual([]);
    expect(createFreezoneWorkflowRun).toHaveBeenCalledTimes(1);
    expect(second.commandResults).toEqual(expect.arrayContaining([expect.objectContaining({ output: expect.objectContaining({ reason: 'workflow_action_already_completed' }) })]));
  });

  it('does not re-pay for the same direct speech alignment after the plan save advances its revision', async () => {
    const draft = plan();
    draft.beats[0].turns = [draft.beats[0].turns[0]];
    draft.speech_takes![0].turn_ids = ['first'];
    useCanvasStore.getState().updateNodeData('plan-node', { productionPlanV2Draft: draft, processingAction: 'generate_production_plan_draft',
      processingPayload: { topic: '旧起草目标' } });
    vi.mocked(flushProductionPlanV2AutoSave).mockImplementation(async () => {
      useCanvasStore.getState().updateNodeData('plan-node', { productionPlanV2Ref: { artifactId: 'plan', revision: 2 }, productionPlanV2Dirty: false });
      return { artifactId: 'plan', revision: 2 };
    });
    const base = { schema_version: 'freezone_workflow_run.v1' as const, run_id: 'run-align', project_id: 'project', canvas_id: 'canvas',
      resumable: false, created_at: '', started_at: '', updated_at: '', actions: [] };
    const completed = { ...base, status: 'completed' as const, actions: [{ node_id: 'plan-node', action: 'align_production_speech', status: 'completed' as const,
      artifact_status: 'valid' as const, result_ref: { artifact_id: 'asr', revision: 1 } }] };
    vi.mocked(createFreezoneWorkflowRun).mockResolvedValue({ ...base, status: 'running', actions: [admittedAction('plan-node', 'align_production_speech')] });
    vi.mocked(getFreezoneWorkflowRun).mockResolvedValue(completed);
    vi.mocked(updateFreezoneWorkflowRun).mockResolvedValue(completed);
    vi.mocked(fetchRemixArtifact).mockResolvedValue({ id: 'asr', revision: 1, schema: 'word_timeline.v1', upstream_refs: [{ artifact_id: 'plan', revision: 1 }], payload: {
      status: 'completed', turn_id: 'first', placement_id: 'visual', media_hash: `sha256:${hash}`,
      source_in_us: 0, source_out_us: 4_000_000, transcript: '你好', text_hash: 'b'.repeat(64),
      words: [{ text: '你好', start_us: 100_000, end_us: 200_000 }],
    } as never });
    const envelope = [{ schema_version: 'canvas_chat_commands.v1' as const, project_id: 'project', canvas_id: 'canvas',
      commands: [{ type: 'run_node_action' as const, node_id: 'plan-node', action: 'align_production_speech',
        parameters: { turn_id: 'first', take_id: 'take' } }] }];
    const first = await applyCanvasChatCommandsAsync(envelope, { projectId: 'project', canvasId: 'canvas', actionTimeoutMs: 2000 });
    expect(first.errors).toEqual([]);
    expect(createFreezoneWorkflowRun).toHaveBeenCalledTimes(1);
    const current = useCanvasStore.getState();
    const descriptor = buildProductionWorkflowAction(current.nodes[0], current.nodes, current.edges,
      { action: 'align_production_speech', parameters: { turn_id: 'first', take_id: 'take' } })!;
    await reconcileProductionWorkflowResult('project', descriptor, { artifact_id: 'asr', revision: 1 }, () => true);
    const second = await applyCanvasChatCommandsAsync(envelope, { projectId: 'project', canvasId: 'canvas', actionTimeoutMs: 2000 });
    expect(second.errors).toEqual([]);
    expect(createFreezoneWorkflowRun).toHaveBeenCalledTimes(1);
  });

  it('treats a prior export as stale when the linked production plan revision changes', () => {
    const state = useCanvasStore.getState();
    const fingerprint = remixGraphFingerprint(state.nodes, state.edges, 'compose');
    state.updateNodeData('compose', { productionPlanRenderedRef: ref, productionPlanV2Ref: ref,
      remixInputFingerprint: fingerprint, remixInputsCurrent: true, resultVideoUrl: '/final.mp4' });
    expect(hasGeneratedResult('compose', 'auto_compose_video')).toBe(true);
    const nextRef = { artifactId: 'plan', revision: 2 };
    state.updateNodeData('plan-node', { productionPlanV2Ref: nextRef });
    state.updateNodeData('compose', { productionPlanV2Ref: nextRef });
    expect(hasGeneratedResult('compose', 'auto_compose_video')).toBe(false);
  });

  it('keeps a newer manually saved plan instead of replaying an old completed draft', async () => {
    useCanvasStore.setState({ nodes: [node('plan-node', 'scriptNode', { remixRole: 'production_plan',
      processingAction: 'generate_production_plan_draft', processingPayload: { topic: '旧片', analysis_context: { summary: '原片' } },
      productionPlanV2Ref: { artifactId: 'manual', revision: 2 }, productionPlanV2Draft: plan(), remixNodeRevision: 1 })], edges: [] });
    const status = await recoverProductionWorkflowAction('project', { node_id: 'plan-node', action: 'generate_production_plan_draft',
      status: 'completed', artifact_status: 'valid', node_revision: 1,
      payload: { topic: '旧片', analysis_context: { summary: '原片' } }, result_ref: { artifact_id: 'old', revision: 1 } }, () => true);
    expect(status).toBe('retry');
    expect(fetchRemixArtifact).not.toHaveBeenCalled();
    expect(useCanvasStore.getState().nodes[0].data.productionPlanV2Ref).toEqual({ artifactId: 'manual', revision: 2 });
  });

  it('recovers a host published compile after canvas reload without another task', async () => {
    const state = useCanvasStore.getState();
    const descriptor = buildProductionWorkflowAction(state.nodes[1], state.nodes, state.edges, { action: 'compile_production_plan' })!;
    markProductionWorkflowSubmission(descriptor);
    const saved = useCanvasStore.getState();
    saved.setCanvasData(structuredClone(saved.nodes), structuredClone(saved.edges));
    const baseline = structuredClone(useCanvasStore.getState().nodes);
    vi.mocked(getFreezoneCanvasBaseline).mockReturnValue({ nodes: baseline, edges: [edge], revision: 1 });
    vi.mocked(getFreezoneCanvas).mockResolvedValue({ nodes: [baseline[0], { ...baseline[1], data: { ...baseline[1].data,
      productionWorkflowAction: 'compile_production_plan', productionWorkflowResultRef: { artifactId: 'manifest', revision: 1 },
      remixManifestRef: { artifactId: 'manifest', revision: 1 }, productionPlanV2Ref: ref,
      productionWorkflowInputs: descriptor.snapshot.nodeData, productionWorkflowRequestPayload: descriptor.requestPayload,
      remixInputsCurrent: true } }], edges: [edge], revision: 2 } as never);
    const status = await recoverProductionWorkflowAction('project', { node_id: 'compose', action: 'compile_production_plan',
      status: 'completed', artifact_status: 'valid', node_revision: 2,
      payload: { plan_ref: { artifact_id: 'plan', revision: 1 } }, result_ref: { artifact_id: 'manifest', revision: 1 } }, () => true, 'canvas');
    expect(status).toBe('ready');
    expect(useCanvasStore.getState().nodes[1].data.remixManifestRef).toEqual({ artifactId: 'manifest', revision: 1 });
    const repeated = await applyCanvasChatCommandsAsync([{ schema_version: 'canvas_chat_commands.v1', project_id: 'project', canvas_id: 'canvas',
      commands: [{ type: 'run_node_action', node_id: 'compose', action: 'compile_production_plan' }] }],
    { projectId: 'project', canvasId: 'canvas', actionTimeoutMs: 2000 });
    expect(repeated.errors).toEqual([]);
    expect(createFreezoneWorkflowRun).not.toHaveBeenCalled();
  });

  it('does not create a paid processing run until its input snapshot is saved', async () => {
    useCanvasStore.setState({ nodes: [node('plan-node', 'scriptNode', { remixRole: 'production_plan',
      processingAction: 'generate_production_plan_draft', processingPayload: { topic: '新片', analysis_context: { summary: '原片' } },
      remixNodeRevision: 1 })], edges: [] });
    vi.mocked(flushFreezoneCanvasRuntime).mockResolvedValueOnce(false);
    const result = await applyCanvasChatCommandsAsync([{ schema_version: 'canvas_chat_commands.v1', project_id: 'project', canvas_id: 'canvas',
      commands: [{ type: 'run_workflow', node_ids: ['plan-node'], scope: 'selection', direction: 'node' }] }],
    { projectId: 'project', canvasId: 'canvas', actionTimeoutMs: 2000 });
    expect(result.errors).toEqual(expect.arrayContaining([expect.stringMatching(/未保存|保存画布/)]));
    expect(createFreezoneWorkflowRun).not.toHaveBeenCalled();
  });

  it('returns after a native task is durably admitted without polling for its final artifact', async () => {
    useCanvasStore.setState({ nodes: [node('analysis', 'videoStoryNode', { rawResult: { video_story: { summary: '原片叙事' } } }),
      node('plan-node', 'scriptNode', { remixRole: 'production_plan',
        processingAction: 'generate_production_plan_draft', processingPayload: { topic: '新片', analysis_context: { summary: '原片叙事' } }, remixNodeRevision: 1 })],
    edges: [{ id: 'analysis-to-plan', source: 'analysis', target: 'plan-node', type: 'context_for' } as never] });
    vi.mocked(createFreezoneWorkflowRun).mockResolvedValue({ run_id: 'run-plan', status: 'running',
      actions: [{ node_id: 'plan-node', action: 'generate_production_plan_draft', status: 'running',
        processing_operation_id: 'operation-plan', task_key: 'native:plan', task_id: 'task-plan' }] } as never);
    vi.mocked(getFreezoneWorkflowRun).mockImplementation(() => new Promise(() => undefined));
    vi.mocked(updateFreezoneWorkflowRun).mockResolvedValue({ run_id: 'run-plan', status: 'running', actions: [] } as never);
    const persisted = vi.fn();
    const execution = applyCanvasChatCommandsAsync([{ schema_version: 'canvas_chat_commands.v1', project_id: 'project', canvas_id: 'canvas',
      commands: [{ type: 'run_node_action', node_id: 'plan-node', action: 'generate_production_plan_draft',
        parameters: { topic: '新片', analysis_context: { summary: '原片叙事' } } }] }],
    { projectId: 'project', canvasId: 'canvas', actionTimeoutMs: 60_000, onWorkflowRunPersisted: persisted });
    const result = await Promise.race([execution, new Promise<'timed-out'>((resolve) => setTimeout(() => resolve('timed-out'), 100))]);
    expect(result).not.toBe('timed-out');
    expect(getFreezoneWorkflowRun).not.toHaveBeenCalled();
    expect(result).toMatchObject({ errors: [], commandResults: [expect.objectContaining({ type: 'run_node_action', status: 'pending' })] });
    expect(createFreezoneWorkflowRun).toHaveBeenCalledOnce();
    expect(persisted).toHaveBeenCalledWith('run-plan', { workflow_run_id: 'run-plan', operation_id: 'operation-plan',
      task_id: 'task-plan', node_id: 'plan-node', action: 'generate_production_plan_draft', completion_owner: 'host' });
  });

  it('accepts a host-owned run without a task id in the initial response', async () => {
    useCanvasStore.setState({ nodes: [node('analysis', 'videoStoryNode', { rawResult: { video_story: { summary: '原片' } } }),
      node('plan-node', 'scriptNode', { remixRole: 'production_plan',
      processingAction: 'generate_production_plan_draft', processingPayload: { topic: '新片', analysis_context: { summary: '原片' } },
      remixNodeRevision: 1 })], edges: [{ id: 'analysis-to-plan', source: 'analysis', target: 'plan-node', type: 'context_for' } as never] });
    vi.mocked(createFreezoneWorkflowRun).mockResolvedValue({ run_id: 'run-host', status: 'running',
      acceptance: { workflow_run_id: 'run-host', completion_owner: 'host' },
      actions: [{ node_id: 'plan-node', action: 'generate_production_plan_draft', status: 'running',
        processing_operation_id: 'operation-plan', task_key: 'native:plan' }] } as never);
    const persisted = vi.fn();
    const result = await applyCanvasChatCommandsAsync([{ schema_version: 'canvas_chat_commands.v1', project_id: 'project', canvas_id: 'canvas',
      commands: [{ type: 'run_node_action', node_id: 'plan-node', action: 'generate_production_plan_draft',
        parameters: { topic: '新片', analysis_context: { summary: '原片' } } }] }],
    { projectId: 'project', canvasId: 'canvas', bridgeKey: 'bridge.executor.owner', onWorkflowRunPersisted: persisted });
    expect(result.errors).toEqual([]);
    expect(result.commandResults).toEqual(expect.arrayContaining([expect.objectContaining({
      type: 'run_node_action', status: 'pending', output: expect.objectContaining({ workflow_run_id: 'run-host' }),
    })]));
    expect(persisted).toHaveBeenCalledWith('run-host', { workflow_run_id: 'run-host', completion_owner: 'host' });
    expect(getFreezoneWorkflowRun).not.toHaveBeenCalled();
    expect(createFreezoneWorkflowRun).toHaveBeenCalledOnce();
    expect(vi.mocked(updateFreezoneWorkflowRun).mock.calls.some((call) => call[3]?.status === 'failed')).toBe(false);
  });

  it('keeps an older run pending when its task receipt is temporarily incomplete', async () => {
    useCanvasStore.setState({ nodes: [node('analysis', 'videoStoryNode', { rawResult: { video_story: { summary: '原片' } } }),
      node('plan-node', 'scriptNode', { remixRole: 'production_plan',
      processingAction: 'generate_production_plan_draft', processingPayload: { topic: '新片', analysis_context: { summary: '原片' } },
      remixNodeRevision: 1 })], edges: [{ id: 'analysis-to-plan', source: 'analysis', target: 'plan-node', type: 'context_for' } as never] });
    const oldRun = { run_id: 'run-old', status: 'running', actions: [{ node_id: 'plan-node',
      action: 'generate_production_plan_draft', status: 'running', processing_operation_id: 'operation-plan',
      task_key: 'native:plan' }] } as never;
    vi.mocked(createFreezoneWorkflowRun).mockResolvedValue(oldRun);
    vi.mocked(getFreezoneWorkflowRun).mockResolvedValue(oldRun);
    const result = await applyCanvasChatCommandsAsync([{ schema_version: 'canvas_chat_commands.v1', project_id: 'project', canvas_id: 'canvas',
      commands: [{ type: 'run_node_action', node_id: 'plan-node', action: 'generate_production_plan_draft',
        parameters: { topic: '新片', analysis_context: { summary: '原片' } } }] }],
    { projectId: 'project', canvasId: 'canvas', bridgeKey: 'bridge.executor.owner' });
    expect(result.errors).toEqual([]);
    expect(result.commandResults).toEqual(expect.arrayContaining([expect.objectContaining({
      type: 'run_node_action', status: 'pending', output: expect.objectContaining({
        reason: 'workflow_server_reconciliation_pending', workflow_run_id: 'run-old',
      }),
    })]));
    expect(createFreezoneWorkflowRun).toHaveBeenCalledOnce();
    expect(vi.mocked(updateFreezoneWorkflowRun).mock.calls.some((call) => call[3]?.status === 'failed')).toBe(false);
  });

  it('does not attach an old project artifact to a newly imported canvas with the same node ID', async () => {
    const old = node('plan-node', 'scriptNode', { remixRole: 'production_plan',
      processingAction: 'generate_production_plan_draft', processingPayload: { topic: '新片', analysis_context: { summary: '原片' } },
      remixNodeRevision: 1 });
    useCanvasStore.setState({ nodes: [old], edges: [] });
    const state = useCanvasStore.getState();
    const descriptor = buildProductionWorkflowAction(state.nodes[0], state.nodes, state.edges)!;
    let releaseFetch!: (artifact: unknown) => void;
    vi.mocked(fetchRemixArtifact).mockImplementation(() => new Promise((resolve) => { releaseFetch = resolve as typeof releaseFetch; }));
    let oldCanvasIsCurrent = true;
    const pending = reconcileProductionWorkflowResult('project-a', descriptor, { artifact_id: 'old-result', revision: 1 }, () => oldCanvasIsCurrent);
    useCanvasStore.getState().setCanvasData([node('plan-node', 'scriptNode', { remixRole: 'production_plan',
      processingAction: 'generate_production_plan_draft', processingPayload: { topic: '新片', analysis_context: { summary: '原片' } },
      remixNodeRevision: 1, displayName: '新项目中的同名节点' })], []);
    oldCanvasIsCurrent = false;
    releaseFetch({ id: 'old-result', revision: 1, schema: 'production_plan.v2', payload: plan() });
    await expect(pending).rejects.toThrow(/画布|项目/);
    expect(useCanvasStore.getState().nodes[0].data.productionPlanV2Ref).toBeUndefined();
  });
});

it('carries bound requirements into the same manual action and freezes nested inputs', () => {
  const requirements = { primary_shots: { min: 4, max: 7 }, semantic: [{ id: 'scene', text: 'Keep the interview scene' }] };
  const locked_turns = [{ speaker: 'speaker-a', text: 'Actual spoken words' }];
  const planNode = node('plan-node', 'scriptNode', { remixRole: 'production_plan',
    processingAction: 'generate_production_plan_draft', processingPayload: { topic: 'goal', analysis_context: { summary: 'actual' },
      requirements, locked_turns, requirements_fingerprint: 'host-fingerprint', requirements_provenance: { direction_revision: 2 } } });
  const descriptor = buildProductionWorkflowAction(planNode, [planNode], [],
    { action: 'generate_production_plan_draft', parameters: { topic: 'goal', analysis_context: { summary: 'actual' } } })!;
  expect(descriptor.payload.requirements).toEqual(requirements);
  expect(descriptor.payload.locked_turns).toEqual(locked_turns);
  expect(descriptor.payload.requirements_fingerprint).toBe('host-fingerprint');
  requirements.primary_shots.min = 20;
  locked_turns[0].text = 'changed locally';
  expect(descriptor.payload.requirements).toEqual({ primary_shots: { min: 4, max: 7 }, semantic: [{ id: 'scene', text: 'Keep the interview scene' }] });
  expect(descriptor.payload.locked_turns).toEqual([{ speaker: 'speaker-a', text: 'Actual spoken words' }]);
});
