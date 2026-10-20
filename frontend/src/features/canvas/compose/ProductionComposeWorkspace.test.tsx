import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ProductionPlanV2 } from '@/features/canvas/remix/productionPlanV2Model';

const api = vi.hoisted(() => ({ fetchRemixArtifact: vi.fn(), projectProductionPlan: vi.fn(), compileProductionPlan: vi.fn(), createRemixArtifact: vi.fn(), renderRemixManifest: vi.fn(), attachRemixMedia: vi.fn(), fetchLatestRemixArtifact: vi.fn(), listRemixOperations: vi.fn(), previewProductionFrame: vi.fn() }));
const save = vi.hoisted(() => ({ flushProductionPlanV2AutoSave: vi.fn(), scheduleProductionPlanV2AutoSave: vi.fn(), productionPlanUpstreamRefs: vi.fn((_draft: ProductionPlanV2): Array<{ artifactId: string; revision: number }> => []) }));
const canvas = vi.hoisted(() => ({ flushFreezoneCanvasRuntime: vi.fn(), captureFreezoneCanvasScope: () => () => true }));
const native = vi.hoisted(() => ({ bindProductionCanvasCandidate: vi.fn() }));
const state = {
  nodes: [] as Array<{ id: string; type: string; data: Record<string, unknown> }>, edges: [] as Array<{ source: string; target: string }>,
  updateNodeData: vi.fn((id: string, patch: Record<string, unknown>) => { const node = state.nodes.find((item) => item.id === id); if (node) node.data = { ...node.data, ...patch }; }),
  addEdge: vi.fn(() => 'edge'), addNode: vi.fn(() => 'new'), findNodePosition: vi.fn(() => ({ x: 0, y: 0 })),
  setSelectedNode: vi.fn(), requestFocusNode: vi.fn(),
};
vi.mock('@/api/remix', () => api);
vi.mock('@/features/canvas/remix/productionPlanV2AutoSave', () => save);
vi.mock('@/features/canvas/remix/productionCanvasCommand', () => native);
vi.mock('@/features/freezone/canvasSyncRuntime', () => canvas);
vi.mock('@/features/viewer-kit/useViewerImmersiveBody', () => ({ useViewerImmersiveBody: vi.fn() }));
vi.mock('@/features/canvas/remix/prepareNodeRemix', () => ({ remixGraphFingerprint: () => 'fingerprint' }));
vi.mock('@/stores/canvasStore', () => ({ useCanvasStore: Object.assign((selector: (value: typeof state) => unknown) => selector(state), { getState: () => state }) }));

import { ProductionComposeWorkspace } from './ProductionComposeWorkspace';

function plan(visual = true): ProductionPlanV2 {
  return { schema_version: 2, goal: '横屏作品', output: { width: 1920, height: 1080, fps_num: 25, fps_den: 1 },
    beats: [{ id: 'beat', purpose: '开场', duration_frames: 100, turns: [] }], resources: [],
    placements: visual ? [{ id: 'visual', kind: 'visual', role: 'primary', track_id: 'visual:primary', description: '开场画面', start: { kind: 'program_frame', frame: 0 }, end: { kind: 'program_frame', frame: 100 } }] : [], provenance: {} };
}

describe('production composition workspace', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.edges = [{ source: 'plan', target: 'compose' }];
    state.nodes = [{ id: 'plan', type: 'scriptNode', data: { productionPlanV2Draft: plan(), productionPlanV2Ref: { artifactId: 'plan-artifact', revision: 1 }, productionPlanV2Dirty: false } },
      { id: 'compose', type: 'videoComposeNode', data: { productionPlanNodeId: 'plan', productionPlanV2Ref: { artifactId: 'plan-artifact', revision: 1 } } }];
    api.projectProductionPlan.mockResolvedValue({ plan_ref: { artifact_id: 'plan-artifact', revision: 1 }, output_spec: {}, beats: [], resources: [], placements: [], issues: [{ placement_id: 'visual', code: 'unbound_media', message: 'Bind media' }], ready: false });
    api.compileProductionPlan.mockResolvedValue({ manifest_version: 4 });
    api.createRemixArtifact.mockResolvedValue({ id: 'manifest', revision: 1 });
    api.renderRemixManifest.mockResolvedValue({ operation_id: 'operation' });
    api.listRemixOperations.mockResolvedValue({ items: [] });
    save.flushProductionPlanV2AutoSave.mockResolvedValue({ artifactId: 'plan-artifact', revision: 1 });
    canvas.flushFreezoneCanvasRuntime.mockResolvedValue(true);
  });

  it('opens an incomplete plan with semantic and placeholder tracks, and blocks export', async () => {
    render(<ProductionComposeWorkspace project="demo" canvasId="canvas" planNodeId="plan" composeNodeId="compose" onClose={() => undefined} />);
    expect(screen.getByRole('dialog', { name: '视频合成' })).toBeVisible();
    expect(screen.getByText(/1\. 开场/)).toBeVisible();
    expect(screen.getAllByText('开场画面').length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole('button', { name: '导出 MP4' }));
    await waitFor(() => expect(screen.getByText(/有 1 项需要处理/)).toBeVisible());
    expect(api.compileProductionPlan).not.toHaveBeenCalled();
  });

  it('writes new track entries into the same plan draft', () => {
    render(<ProductionComposeWorkspace project="demo" canvasId="canvas" planNodeId="plan" composeNodeId="compose" onClose={() => undefined} />);
    fireEvent.click(screen.getAllByRole('button', { name: '字幕' })[0]);
    const next = state.nodes[0].data.productionPlanV2Draft as ProductionPlanV2;
    expect(next.placements).toHaveLength(2);
    expect(next.placements[1]).toMatchObject({ kind: 'subtitle', role: 'dialogue', track_id: 'subtitles' });
    expect(save.scheduleProductionPlanV2AutoSave).toHaveBeenCalledWith('demo', 'plan');
  });

  it('undoes its own timeline edit without restoring an older whole-plan snapshot', () => {
    render(<ProductionComposeWorkspace project="demo" canvasId="canvas" planNodeId="plan" composeNodeId="compose" onClose={() => undefined} />);
    fireEvent.click(screen.getAllByRole('button', { name: '字幕' })[0]);
    const later = structuredClone(state.nodes[0].data.productionPlanV2Draft as ProductionPlanV2);
    later.goal = '其他人后改的目标';
    state.updateNodeData('plan', { productionPlanV2Draft: later });
    fireEvent.click(screen.getByTitle('撤销'));
    const undone = state.nodes[0].data.productionPlanV2Draft as ProductionPlanV2;
    expect(undone.placements.map((item) => item.id)).toEqual(['visual']);
    expect(undone.goal).toBe('其他人后改的目标');
  });

  it('adds and edits a graphic effect without requesting media', () => {
    state.nodes[0].data.productionPlanV2Draft = plan(false);
    render(<ProductionComposeWorkspace project="demo" canvasId="canvas" planNodeId="plan" composeNodeId="compose" onClose={() => undefined} />);
    fireEvent.click(screen.getAllByRole('button', { name: '图形效果' })[0]);
    let draft = state.nodes[0].data.productionPlanV2Draft as ProductionPlanV2;
    expect(draft.placements[0]).toMatchObject({ kind: 'graphic', role: 'effect', graphic: { template: 'question_card' } });
    fireEvent.click(screen.getByTitle(/问题卡片/));
    expect(screen.queryByText('待绑定素材')).not.toBeInTheDocument();
    expect(screen.queryByText(/此处没有画面/)).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('图形文字'), { target: { value: '最近离不开什么？' } });
    draft = state.nodes[0].data.productionPlanV2Draft as ProductionPlanV2;
    expect(draft.placements[0].graphic?.text).toBe('最近离不开什么？');
    expect(save.scheduleProductionPlanV2AutoSave).toHaveBeenCalledWith('demo', 'plan');
  });

  it('shows word and named-range lanes and attaches a placement to the shared range', () => {
    const draft = plan();
    draft.beats[0].turns = [{ id: 'turn', speaker: 'A', text: '你好', start_frame: 10, end_frame: 40, timing_basis: 'aligned',
      words: [{ id: 'hello', text: '你好', start_frame: 12, end_frame: 25 }] }];
    draft.selections = [{ id: 'cue', label: '问候', start: { kind: 'word', ref_id: 'hello', edge: 'start' }, end: { kind: 'word', ref_id: 'hello', edge: 'end' } }];
    state.nodes[0].data.productionPlanV2Draft = draft;
    render(<ProductionComposeWorkspace project="demo" canvasId="canvas" planNodeId="plan" composeNodeId="compose" onClose={() => undefined} />);
    expect(screen.getByText('词级定位')).toBeVisible();
    expect(screen.getAllByText('问候').length).toBeGreaterThan(0);
    fireEvent.click(screen.getByTitle(/开场画面.*待绑定素材/));
    fireEvent.change(screen.getByLabelText('绑定命名范围'), { target: { value: 'cue' } });
    expect((state.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).placements[0]).toMatchObject({
      start: { kind: 'selection', ref_id: 'cue', edge: 'start' }, end: { kind: 'selection', ref_id: 'cue', edge: 'end' },
    });
    expect(save.scheduleProductionPlanV2AutoSave).toHaveBeenCalledWith('demo', 'plan');
  });

  it('shows accepted SRT cues and binds an existing placement to the selected cue', () => {
    const draft = plan();
    draft.beats[0].turns = [{ id: 'turn', speaker: '嘉宾', text: '黑咖啡，没有之一。' }];
    draft.speech_takes = [{ id: 'take', beat_id: 'beat', resource_id: 'voice', turn_ids: ['turn'], origin_placement_id: 'voice-placement',
      source_in_us: 0, source_out_us: 4_000_000, speed_num: 1, speed_den: 1, lead_in_frames: 0 }];
    draft.speech_cues = [{ id: 'cue-1', turn_id: 'turn', take_id: 'take', text: '黑咖啡，没有之一。',
      source_start_us: 500_000, source_end_us: 2_000_000, timing_source: 'srt_import', timing_status: 'ready', word_ids: [],
      evidence_ref: { artifact_id: 'evidence', revision: 1 } }];
    draft.resources = [{ id: 'voice', node_id: 'voice-node', media_kind: 'audio', media_path: 'media/voice.wav', media_hash: 'a'.repeat(64),
      duration_us: 4_000_000, has_audio: true }];
    state.nodes[0].data.productionPlanV2Draft = draft;
    render(<ProductionComposeWorkspace project="demo" canvasId="canvas" planNodeId="plan" composeNodeId="compose" onClose={() => undefined} />);
    expect(screen.getByText('SRT 台词')).toBeVisible();
    expect(screen.getByRole('button', { name: /黑咖啡，没有之一。/ })).toBeVisible();
    expect(document.querySelector('[data-speech-take-id="take"]')).toHaveAttribute('src', '/api/v1/projects/demo/media/media/voice.wav');
    fireEvent.click(screen.getByTitle(/开场画面.*待绑定素材/));
    fireEvent.change(screen.getByLabelText('绑定台词'), { target: { value: 'cue-1' } });
    expect((state.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).placements[0]).toMatchObject({
      start: { kind: 'cue', ref_id: 'cue-1', edge: 'start' }, end: { kind: 'cue', ref_id: 'cue-1', edge: 'end' },
    });
  });

  it('edits one take-owned original sound without splitting it or leaving old cue evidence ready', () => {
    const draft = plan(false);
    draft.beats[0].turns = [{ id: 'turn', speaker: '嘉宾', text: '黑咖啡。', timing_basis: 'aligned',
      words: [{ id: 'word', text: '黑咖啡', start_frame: 0, end_frame: 20 }] }];
    draft.resources = [{ id: 'voice', node_id: 'voice-node', media_kind: 'audio', media_path: 'media/voice.wav',
      media_hash: 'a'.repeat(64), duration_us: 4_000_000, has_audio: true }];
    draft.speech_takes = [{ id: 'take', beat_id: 'beat', resource_id: 'voice', turn_ids: ['turn'], origin_placement_id: 'voice-placement',
      source_in_us: 0, source_out_us: null, speed_num: 1, speed_den: 1, lead_in_frames: 0 }];
    draft.speech_cues = [{ id: 'cue', turn_id: 'turn', take_id: 'take', text: '黑咖啡。', source_start_us: 0, source_end_us: 2_000_000,
      timing_source: 'asr', timing_status: 'ready', word_ids: ['word'], evidence_ref: { artifact_id: 'evidence', revision: 1 } }];
    draft.placements = [{ id: 'voice-placement', kind: 'audio', role: 'speech', track_id: 'audio:speech', description: '原声', resource_id: 'voice',
      start: { kind: 'beat', ref_id: 'beat', edge: 'start' }, end: { kind: 'beat', ref_id: 'beat', edge: 'end' } }];
    state.nodes[0].data.productionPlanV2Draft = draft;
    render(<ProductionComposeWorkspace project="demo" canvasId="canvas" planNodeId="plan" composeNodeId="compose" onClose={() => undefined} />);
    fireEvent.click(screen.getByTitle(/原声/));
    expect(screen.getByRole('button', { name: '在播放头拆分' })).toBeDisabled();
    fireEvent.change(screen.getByLabelText('音量'), { target: { value: '0.4' } });
    expect((state.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).speech_takes?.[0].volume).toBe(0.4);
    fireEvent.change(screen.getByLabelText('源出点（秒；0 为完整素材）'), { target: { value: '3' } });
    const next = state.nodes[0].data.productionPlanV2Draft as ProductionPlanV2;
    expect(next.speech_takes?.[0].source_out_us).toBe(3_000_000);
    expect(next.speech_cues?.[0]).toMatchObject({ timing_status: 'stale', word_ids: [], evidence_ref: null });
  });

  it('returns a selected placement to the original plan editor', () => {
    const onClose = vi.fn();
    render(<ProductionComposeWorkspace project="demo" canvasId="canvas" planNodeId="plan" composeNodeId="compose" onClose={onClose} />);
    fireEvent.click(screen.getByTitle(/开场画面.*待绑定素材/));
    fireEvent.click(screen.getByRole('button', { name: '在作品方案中查看' }));
    expect(state.nodes[0].data).toMatchObject({ productionPlanOpenOnCreate: true, productionPlanV2FocusPlacementId: 'visual' });
    expect(onClose).toHaveBeenCalled();
  });

  it('shows bound resource provenance and only related operations', async () => {
    const draft = plan();
    draft.resources = [{ id: 'resource', node_id: 'source-video', node_revision: 3, media_kind: 'video', media_path: 'media/take.mp4',
      media_hash: 'a'.repeat(64), duration_us: 4_000_000, has_audio: true }];
    draft.placements[0].resource_id = 'resource';
    state.nodes[0].data.productionPlanV2Draft = draft;
    state.nodes.push({ id: 'source-video', type: 'videoNode', data: { displayName: '访谈镜头', videoUrl: '/take.mp4',
      remixMedia: { media_path: 'media/take.mp4', media_hash: 'a'.repeat(64), media_kind: 'video', duration_us: 4_000_000, has_audio: true } } });
    state.edges.push({ source: 'source-video', target: 'plan' });
    api.listRemixOperations.mockResolvedValue({ items: [
      { operation_id: 'related', status: 'succeeded', node_id: 'compose', output_refs: [] },
      { operation_id: 'unrelated', status: 'succeeded', node_id: 'other', output_refs: [] },
    ] });
    render(<ProductionComposeWorkspace project="demo" canvasId="canvas" planNodeId="plan" composeNodeId="compose" onClose={() => undefined} />);
    fireEvent.click(screen.getByRole('button', { name: '素材版本' }));
    expect(screen.getByText('访谈镜头')).toBeVisible();
    expect(screen.getByText(/节点版本 3/)).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '任务记录' }));
    await waitFor(() => expect(screen.getByText(/related/)).toBeVisible());
    expect(screen.queryByText(/unrelated/)).not.toBeInTheDocument();
  });

  it('remembers the selected placement when creating media on the canvas', () => {
    render(<ProductionComposeWorkspace project="demo" canvasId="canvas" planNodeId="plan" composeNodeId="compose" onClose={() => undefined} />);
    fireEvent.click(screen.getByTitle(/开场画面.*待绑定素材/));
    fireEvent.click(screen.getByRole('button', { name: '生成图片' }));
    expect(state.nodes[0].data.productionPlanV2PendingBindings).toEqual({ visual: 'new' });
  });

  it('clears a pending generated material when the user binds another completed material', async () => {
    state.nodes[0].data.productionPlanV2PendingBindings = { visual: 'pending-material' };
    state.nodes.push({ id: 'ready-image', type: 'imageGenNode', data: { displayName: '现有画面', imageUrl: '/ready.png', remixNodeRevision: 7,
      remixMedia: { media_path: 'images/ready.png', media_hash: 'a'.repeat(64), media_kind: 'image', duration_us: 0,
        has_audio: false, url: '/ready.png' } } });
    native.bindProductionCanvasCandidate.mockImplementation(async () => {
      const next = structuredClone(state.nodes[0].data.productionPlanV2Draft as ProductionPlanV2);
      next.resources.push({ id: `resource:ready-image:${'a'.repeat(12)}`, node_id: 'ready-image', node_revision: 7,
        media_kind: 'image', media_path: 'images/ready.png', media_hash: 'a'.repeat(64), duration_us: 0, has_audio: false });
      next.placements[0].resource_id = next.resources[0].id;
      state.updateNodeData('plan', { productionPlanV2Draft: next, productionPlanV2Ref: { artifactId: 'plan-artifact', revision: 2 } });
    });
    render(<ProductionComposeWorkspace project="demo" canvasId="canvas" planNodeId="plan" composeNodeId="compose" onClose={() => undefined} />);
    fireEvent.click(screen.getByTitle(/开场画面.*待绑定素材/));
    fireEvent.click(screen.getByRole('button', { name: '绑定所选' }));
    await waitFor(() => expect(state.nodes[0].data.productionPlanV2PendingBindings).toEqual({}));
    expect(native.bindProductionCanvasCandidate).toHaveBeenCalledWith('demo', 'canvas', 'plan', 'visual', 'ready-image');
    expect((state.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).placements[0].resource_id).toContain('ready-image');
    expect((state.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).resources[0].node_revision).toBe(7);
  });

  it('compiles and renders only a saved, ready revision', async () => {
    state.nodes[0].data.productionPlanV2Draft = plan(false);
    api.projectProductionPlan.mockResolvedValue({ plan_ref: { artifact_id: 'plan-artifact', revision: 1 }, output_spec: {}, beats: [], resources: [], placements: [], issues: [], ready: true });
    render(<ProductionComposeWorkspace project="demo" canvasId="canvas" planNodeId="plan" composeNodeId="compose" onClose={() => undefined} />);
    fireEvent.click(screen.getByRole('button', { name: '导出 MP4' }));
    await waitFor(() => expect(api.renderRemixManifest).toHaveBeenCalled());
    expect(api.compileProductionPlan).toHaveBeenCalledWith('demo', { artifactId: 'plan-artifact', revision: 1 });
    expect(api.createRemixArtifact).toHaveBeenCalledWith('demo', 'composition_manifest.v2', { manifest_version: 4 }, [{ artifactId: 'plan-artifact', revision: 1 }]);
    expect(state.nodes[1].data.remixOperationId).toBe('operation');
  });

  it('reviews one exact frame only from a ready saved revision', async () => {
    state.nodes[0].data.productionPlanV2Draft = plan(false);
    api.projectProductionPlan.mockResolvedValue({ plan_ref: { artifact_id: 'plan-artifact', revision: 1 }, output_spec: {}, beats: [], resources: [], selections: [], placements: [], issues: [], ready: true });
    api.previewProductionFrame.mockResolvedValue(new Blob(['image'], { type: 'image/png' }));
    const createUrl = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:review-frame');
    const revokeUrl = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    render(<ProductionComposeWorkspace project="demo" canvasId="canvas" planNodeId="plan" composeNodeId="compose" onClose={() => undefined} />);
    const review = screen.getByRole('button', { name: '核对导出画面' });
    expect(review).toBeDisabled();
    await waitFor(() => expect(review).toBeEnabled());
    fireEvent.click(review);
    await waitFor(() => expect(api.previewProductionFrame).toHaveBeenCalledWith('demo', { artifactId: 'plan-artifact', revision: 1 }, 0));
    expect(screen.getByAltText('导出渲染帧')).toHaveAttribute('src', 'blob:review-frame');
    expect(screen.getByText(/方案版本 1 · 第 0 帧/)).toBeVisible();
    createUrl.mockRestore(); revokeUrl.mockRestore();
  });

  it('does not review a dirty plan or missing material', async () => {
    state.nodes[0].data.productionPlanV2Dirty = true;
    render(<ProductionComposeWorkspace project="demo" canvasId="canvas" planNodeId="plan" composeNodeId="compose" onClose={() => undefined} />);
    expect(screen.getByRole('button', { name: '核对导出画面' })).toBeDisabled();
    expect(api.previewProductionFrame).not.toHaveBeenCalled();
  });

  it('preserves edits made while branching a conflicted local plan', async () => {
    state.nodes[0].data.productionPlanV2Conflict = true;
    api.fetchLatestRemixArtifact.mockResolvedValue({ id: 'plan-artifact', revision: 2,
      schema: 'production_plan.v2', payload: plan(false) });
    let finish!: (artifact: { id: string; revision: number }) => void;
    api.createRemixArtifact.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    render(<ProductionComposeWorkspace project="demo" canvasId="canvas" planNodeId="plan" composeNodeId="compose" onClose={() => undefined} />);
    await waitFor(() => expect(screen.getByRole('button', { name: '保留本地为新方案' })).toBeVisible());
    fireEvent.click(screen.getByRole('button', { name: '保留本地为新方案' }));
    fireEvent.change(screen.getByDisplayValue('横屏作品'), { target: { value: '刚刚改的标题' } });
    await act(async () => { finish({ id: 'branch', revision: 1 }); });
    expect((state.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).goal).toBe('刚刚改的标题');
    expect(state.nodes[0].data.productionPlanV2Dirty).toBe(true);
    expect(save.scheduleProductionPlanV2AutoSave).toHaveBeenCalledWith('demo', 'plan');
  });

  it('declares a branch source when preserving timed cues in a conflict branch', async () => {
    state.nodes[0].data.productionPlanV2Conflict = true;
    api.fetchLatestRemixArtifact.mockResolvedValue({ id: 'plan-artifact', revision: 2,
      schema: 'production_plan.v2', payload: plan(false) });
    save.productionPlanUpstreamRefs.mockImplementationOnce((draft: ProductionPlanV2) => [{
      artifactId: draft.provenance.branch_source_ref!.artifact_id,
      revision: draft.provenance.branch_source_ref!.revision,
    }]);
    render(<ProductionComposeWorkspace project="demo" canvasId="canvas" planNodeId="plan" composeNodeId="compose" onClose={() => undefined} />);
    await waitFor(() => expect(screen.getByRole('button', { name: '保留本地为新方案' })).toBeVisible());
    fireEvent.click(screen.getByRole('button', { name: '保留本地为新方案' }));
    await waitFor(() => expect(api.createRemixArtifact).toHaveBeenCalledWith('demo', 'production_plan.v2',
      expect.objectContaining({ provenance: expect.objectContaining({ branch_source_ref: { artifact_id: 'plan-artifact', revision: 1 } }) }),
      [{ artifactId: 'plan-artifact', revision: 2 }, { artifactId: 'plan-artifact', revision: 1 }]));
  });
});
