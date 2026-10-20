import { StrictMode, useSyncExternalStore } from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const canvasActions = vi.hoisted(() => ({ updateNodeData: vi.fn(), setSelectedNode: vi.fn(), requestFocusNode: vi.fn() }));
const prepareNodeRemix = vi.hoisted(() => vi.fn());
const remixGraphFingerprint = vi.hoisted(() => vi.fn(() => 'current-fingerprint'));
const renderRemixManifest = vi.hoisted(() => vi.fn());
const fetchRemixArtifact = vi.hoisted(() => vi.fn());
const reviseRemixArtifact = vi.hoisted(() => vi.fn());
const fetchRemixOperation = vi.hoisted(() => vi.fn());
const reconcileRemixOperation = vi.hoisted(() => vi.fn());
const composeModal = vi.hoisted(() => vi.fn((_props: unknown) => null));
const submitProductionComposeRender = vi.hoisted(() => vi.fn());
const submitFreezoneVideoCompose = vi.hoisted(() => vi.fn());
const graphState = vi.hoisted(() => ({ nodes: [] as unknown[], edges: [] as unknown[], upstreamNodes: [] as unknown[], listeners: new Set<() => void>() }));
vi.mock('@xyflow/react', () => ({ Handle: ({ id }: { id: string }) => <div data-handle={id} />, Position: { Left: 'left', Right: 'right' }, useUpdateNodeInternals: () => () => {} }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('@/stores/canvasStore', () => {
  const state = { get nodes() { return graphState.nodes; }, get edges() { return graphState.edges; }, updateNodeData: canvasActions.updateNodeData, setSelectedNode: canvasActions.setSelectedNode, requestFocusNode: canvasActions.requestFocusNode };
  return { useCanvasStore: Object.assign((selector: (value: typeof state) => unknown) => useSyncExternalStore(
    (listener) => { graphState.listeners.add(listener); return () => graphState.listeners.delete(listener); },
    () => selector(state),
  ), { getState: () => state }) };
});
vi.mock('@/features/canvas/application/useUpstreamGraph', () => ({ useUpstreamNodes: () => graphState.upstreamNodes }));
vi.mock('@/features/canvas/domain/nodeDisplay', () => ({ localizeNodeDisplayName: () => 'Compose' }));
vi.mock('@/features/canvas/ui/NodeHeader', () => ({ NodeHeader: () => null, NODE_HEADER_FLOATING_POSITION_CLASS: '' }));
vi.mock('@/features/canvas/ui/AddNodeToChatButton', () => ({ AddNodeToChatButton: () => null }));
vi.mock('@/features/canvas/compose/VideoComposeModal', () => ({ VideoComposeModal: composeModal, buildInitialTimeline: vi.fn(), reconcileDraftWithUpstream: vi.fn() }));
vi.mock('@/features/canvas/compose/productionComposeRender', () => ({ submitProductionComposeRender }));
vi.mock('@/api/ops', async (importOriginal) => ({ ...await importOriginal<object>(), submitFreezoneVideoCompose }));
vi.mock('@/api/tasks', async (importOriginal) => ({ ...await importOriginal<object>(), awaitTaskCompletion: vi.fn(async () => ({ status: 'succeeded' })) }));
vi.mock('@/features/canvas/remix/prepareNodeRemix', () => ({ prepareNodeRemix, remixGraphFingerprint }));
vi.mock('@/lib/url-params', () => ({ readUrl: () => ({ project: 'demo', canvas: 'default' }) }));
vi.mock('@/api/remix', () => ({
  listRemixOperations: vi.fn(async () => ({ items: [] })),
  fetchRemixOperation, reconcileRemixOperation, transitionRemixOperation: vi.fn(), fetchRemixArtifact, reviseRemixArtifact, confirmRemixOperation: vi.fn(), renderRemixManifest,
}));
vi.mock('@/features/freezone/canvasSyncRuntime', () => ({ flushFreezoneCanvasRuntime: vi.fn(async () => true), applyRemoteFreezoneCanvas: vi.fn(), captureFreezoneCanvasScope: () => () => true }));
vi.mock('@/api/canvas', () => ({ getFreezoneCanvas: vi.fn() }));

import { VideoComposeNode } from '../nodes/VideoComposeNode';
import { dispatchNodeAction } from '@/features/canvas/application/nodeActionResult';
import { canvasEventBus } from '@/features/canvas/application/canvasServices';

describe('VideoComposeNode automatic preparation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    graphState.nodes = [];
    graphState.edges = [];
    graphState.upstreamNodes = [{ id: 'timing', type: 'semanticTimelineNode', data: {} }];
    graphState.listeners.clear();
    remixGraphFingerprint.mockReturnValue('current-fingerprint');
    prepareNodeRemix.mockReset();
    fetchRemixArtifact.mockReset();
    reviseRemixArtifact.mockReset();
    fetchRemixOperation.mockReset();
    reconcileRemixOperation.mockReset();
    submitProductionComposeRender.mockReset();
    submitFreezoneVideoCompose.mockReset();
  });

  it('runs the saved layered production plan rather than the standalone clip composer', async () => {
    const ref = { artifactId: 'plan-v2', revision: 2 };
    const data = { displayName: '视频合成', productionPlanNodeId: 'plan', productionPlanV2Ref: ref };
    graphState.nodes = [{ id: 'compose', type: 'videoComposeNode', data }];
    graphState.upstreamNodes = [{ id: 'plan', type: 'scriptNode', data: { productionPlanV2Ref: ref } }];
    canvasActions.updateNodeData.mockImplementation((nodeId, patch) => {
      const node = graphState.nodes.find((item) => (item as { id: string }).id === nodeId) as { data: Record<string, unknown> } | undefined;
      if (node) Object.assign(node.data, patch);
    });
    submitProductionComposeRender.mockImplementation(async () => {
      Object.assign(data, { productionPlanRenderedRef: ref, resultVideoUrl: '/layered.mp4' });
      return { saved: ref, task: { task_key: 'render-task', task_type: 'remix_render', job_id: 'render-job', operation_id: 'op' } };
    });
    render(<VideoComposeNode id="compose" type="videoComposeNode" data={data} selected={false} draggable={true} dragging={false} selectable={true} deletable={true} zIndex={0} isConnectable={true} positionAbsoluteX={0} positionAbsoluteY={0} />);
    const received: Array<Record<string, unknown>> = [];
    const unsubscribe = canvasEventBus.subscribe('freezone/node-action-result', (result) => received.push(result as unknown as Record<string, unknown>));
    act(() => { dispatchNodeAction({ nodeId: 'compose', action: 'auto_compose_video', requestId: 'production-action' }); });
    await waitFor(() => expect(received.some((result) => result.requestId === 'production-action' && result.status === 'success')).toBe(true));
    expect(submitProductionComposeRender).toHaveBeenCalledWith({ project: 'demo', canvasId: 'default', planNodeId: 'plan', composeNodeId: 'compose' });
    expect(submitFreezoneVideoCompose).not.toHaveBeenCalled();
    expect(received.find((result) => result.requestId === 'production-action')?.output).toMatchObject({ videoUrl: '/layered.mp4', task_key: 'render-task' });
    unsubscribe();
  });

  it('directs a remix user to the script when a legacy empty background node remains', async () => {
    graphState.upstreamNodes = [
      { id: 'timing', type: 'semanticTimelineNode', data: {} },
      { id: 'script', type: 'scriptNode', data: { remixRole: 'adaptation_script' } },
      { id: 'background', type: 'uploadNode', data: { displayName: '成片背景 / A-roll', remixRole: 'background' } },
    ];
    prepareNodeRemix.mockRejectedValueOnce(new Error('画面素材 background 已变化，请重新选择'));
    const props = { id: 'compose', type: 'videoComposeNode', selected: false, draggable: true, dragging: false, selectable: true, deletable: true, zIndex: 0, isConnectable: true, positionAbsoluteX: 0, positionAbsoluteY: 0 } as const;
    const view = render(<VideoComposeNode {...props} data={{ displayName: 'Compose', autoPrepareRequested: true }} />);

    await waitFor(() => expect(prepareNodeRemix).toHaveBeenCalled());
    expect(screen.getByText(/从改编脚本继续创建主画面/)).toBeTruthy();
    expect(screen.queryByText('videoCompose.node.hint')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '前往脚本节点' }));
    expect(canvasActions.setSelectedNode).toHaveBeenCalledWith('script');
    expect(canvasActions.requestFocusNode).toHaveBeenCalledWith('script');

    graphState.upstreamNodes = [
      { id: 'timing', type: 'semanticTimelineNode', data: {} },
      { id: 'background', type: 'uploadNode', data: {
        remixRole: 'background', imageUrl: '/static/projects/demo/freezone/image/background.png',
        remixMedia: { url: '/static/projects/demo/freezone/image/background.png', media_path: 'freezone/image/background.png', media_hash: 'a'.repeat(64), media_kind: 'image' },
      } },
    ];
    view.rerender(<VideoComposeNode {...props} data={{ displayName: 'Compose' }} />);
    await waitFor(() => expect(screen.queryByText('画面素材 background 已变化，请重新选择')).toBeNull());
    expect(screen.getByRole('button', { name: '生成合成清单' })).toBeEnabled();
  });

  it('offers manifest generation once the background is uploaded and bound', async () => {
    graphState.upstreamNodes = [
      { id: 'timing', type: 'semanticTimelineNode', data: {} },
      { id: 'background', type: 'uploadNode', data: {
        displayName: '成片背景 / A-roll', remixRole: 'background', imageUrl: '/static/projects/demo/freezone/image/background.png',
        remixMedia: { url: '/static/projects/demo/freezone/image/background.png', media_path: 'freezone/image/background.png', media_hash: 'a'.repeat(64), media_kind: 'image' },
      } },
    ];
    prepareNodeRemix.mockResolvedValueOnce({ manifestRef: { artifactId: 'manifest', revision: 1 }, events: [], selections: [], unresolvedRequiredEvents: [] });
    render(<VideoComposeNode id="compose" type="videoComposeNode" data={{ displayName: 'Compose' }} selected={false} draggable={true} dragging={false} selectable={true} deletable={true} zIndex={0} isConnectable={true} positionAbsoluteX={0} positionAbsoluteY={0} />);

    fireEvent.click(screen.getByRole('button', { name: '生成合成清单' }));
    await waitFor(() => expect(prepareNodeRemix).toHaveBeenCalled());
  });

  it('opens production composition from the normal canvas node without exposing raw media ids', async () => {
    graphState.upstreamNodes = [{ id: 'timeline', type: 'semanticTimelineNode', data: { productionPlanNodeId: 'plan' } }];
    fetchRemixArtifact.mockResolvedValue({ id: 'manifest', schema: 'composition_manifest.v1', revision: 1,
      upstream_refs: [], payload: { output_spec: { width: 1080, height: 1920, fps: 30, duration_frames: 90 },
        media_bindings: [], layers: [{ media_id: 'node:internal-secret', start_frame: 0, end_frame: 90 }],
        audio_events: [], subtitle_events: [], manual_overrides: [], source_versions: [] } });
    render(<VideoComposeNode id="compose" type="videoComposeNode" data={{ displayName: '视频合成', remixRole: 'production_compose',
      remixManifestRef: { artifactId: 'manifest', revision: 1 }, remixInputFingerprint: 'current-fingerprint',
      remixNodeRevision: 1, unresolvedRequiredEvents: [], remixEvents: [], remixSelections: [] }}
      selected={false} draggable={true} dragging={false} selectable={true} deletable={true} zIndex={0} isConnectable={true} positionAbsoluteX={0} positionAbsoluteY={0} />);
    expect(screen.getByRole('button', { name: '打开视频合成' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '渲染导出' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '打开视频合成' }));
    expect(screen.getByRole('dialog', { name: '视频合成' })).toBeInTheDocument();
    expect(await screen.findByText(/画面 1/)).toBeInTheDocument();
    expect(screen.queryByText('node:internal-secret')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '渲染导出' })).toBeInTheDocument();
  });

  it('routes a v2 plan straight into the shared full-screen editor with no upstream video minimum', () => {
    graphState.upstreamNodes = [{ id: 'plan', type: 'scriptNode', data: { remixRole: 'production_plan' } }];
    graphState.nodes = [{ id: 'plan', type: 'scriptNode', data: { productionPlanV2Ref: { artifactId: 'plan-v2', revision: 2 }, productionPlanV2Draft: { goal: '横屏作品', beats: [] } } }];
    render(<VideoComposeNode id="compose" type="videoComposeNode" data={{ displayName: '视频合成', productionPlanNodeId: 'plan', productionPlanV2Ref: { artifactId: 'plan-v2', revision: 2 } }} selected={false} draggable={true} dragging={false} selectable={true} deletable={true} zIndex={0} isConnectable={true} positionAbsoluteX={0} positionAbsoluteY={0} />);
    fireEvent.click(screen.getByRole('button', { name: '打开视频合成' }));
    expect(composeModal.mock.calls[0][0]).toMatchObject({ productionPlanNodeId: 'plan', composeNodeId: 'compose', seedNodeIds: [] });
    expect(prepareNodeRemix).not.toHaveBeenCalled();
  });

  it('labels a retained preview as outdated when the arrangement manifest changes', async () => {
    graphState.upstreamNodes = [{ id: 'timeline', type: 'semanticTimelineNode', data: { productionPlanNodeId: 'plan' } }];
    fetchRemixArtifact.mockResolvedValue({ id: 'manifest', schema: 'composition_manifest.v1', revision: 2,
      upstream_refs: [], payload: { output_spec: { width: 1080, height: 1920, fps: 30, duration_frames: 90 },
        media_bindings: [], layers: [], audio_events: [], subtitle_events: [], manual_overrides: [], source_versions: [] } });
    const props = { id: 'compose', type: 'videoComposeNode', selected: false, draggable: true, dragging: false,
      selectable: true, deletable: true, zIndex: 0, isConnectable: true, positionAbsoluteX: 0, positionAbsoluteY: 0 } as const;
    const view = render(<VideoComposeNode {...props} data={{ displayName: '视频合成', remixRole: 'production_compose',
      remixManifestRef: { artifactId: 'manifest', revision: 2 }, remixRenderManifestRef: { artifactId: 'manifest', revision: 1 },
      remixInputFingerprint: 'current-fingerprint', resultVideoUrl: '/old.mp4', remixNodeRevision: 2,
      unresolvedRequiredEvents: [], remixEvents: [], remixSelections: [] }} />);
    fireEvent.click(screen.getByRole('button', { name: '打开视频合成' }));
    expect(await screen.findByText('旧版预览')).toBeInTheDocument();
    expect(screen.getByText('预览已过期，重新渲染后更新')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '渲染导出' })).toBeEnabled();
    renderRemixManifest.mockResolvedValue({ operation_id: 'new-render' });
    fireEvent.click(screen.getByRole('button', { name: '渲染导出' }));
    await waitFor(() => expect(canvasActions.updateNodeData).toHaveBeenCalledWith('compose', expect.objectContaining({
      remixOperationId: 'new-render', resultVideoUrl: null, remixPreviousVideoUrl: '/old.mp4',
    })));
    view.rerender(<VideoComposeNode {...props} data={{ displayName: '视频合成', remixRole: 'production_compose',
      remixManifestRef: { artifactId: 'manifest', revision: 2 }, remixRenderManifestRef: { artifactId: 'manifest', revision: 1 },
      remixInputFingerprint: 'current-fingerprint', resultVideoUrl: null, remixPreviousVideoUrl: '/old.mp4',
      remixOperationId: 'new-render', remixNodeRevision: 2, unresolvedRequiredEvents: [], remixEvents: [], remixSelections: [] }} />);
    expect(screen.getByText('正在合成视频')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '渲染导出' })).toBeDisabled();
    expect(screen.getByRole('link', { name: '查看旧版预览' })).toHaveAttribute('href', '/old.mp4');
  });

  it('offers manifest generation when every script event already has bound B-roll', async () => {
    graphState.upstreamNodes = [
      { id: 'timing', type: 'semanticTimelineNode', data: {} },
      { id: 'script', type: 'scriptNode', data: { remixRole: 'adaptation_script', remixScriptRef: { artifactId: 'script', revision: 1 }, remixScriptDraft: { selections: [{ id: 'event-1', label: '产品镜头', visualNodeId: 'visual', visualMediaRevision: 'a'.repeat(64) }] } } },
      { id: 'visual', type: 'uploadNode', data: { remixRole: 'visual', imageUrl: '/static/projects/demo/freezone/image/visual.png', remixMedia: { url: '/static/projects/demo/freezone/image/visual.png', media_path: 'freezone/image/visual.png', media_hash: 'a'.repeat(64), media_kind: 'image' } } },
    ];
    graphState.edges = [{ id: 'visual-script', source: 'visual', target: 'script' }];
    render(<VideoComposeNode id="compose" type="videoComposeNode" data={{ displayName: 'Compose' }} selected={false} draggable={true} dragging={false} selectable={true} deletable={true} zIndex={0} isConnectable={true} positionAbsoluteX={0} positionAbsoluteY={0} />);
    expect(screen.getByRole('button', { name: '生成合成清单' })).toBeEnabled();
  });

  it('offers reconciliation and force regeneration while provider outcome is unknown', async () => {
    fetchRemixArtifact.mockResolvedValue({ id: 'manifest', schema: 'composition_manifest.v1', revision: 2, upstream_refs: [], payload: {
      output_spec: { width: 1080, height: 1920, fps: 30, duration_frames: 60 }, media_bindings: [], layers: [], subtitle_events: [], audio_events: [], manual_overrides: [], source_versions: [],
    } });
    fetchRemixOperation.mockResolvedValue({ operation_id: 'op-1', status: 'pending_reconciliation', task_id: 'task-1', output_refs: [] });
    reconcileRemixOperation.mockResolvedValue({ operation_id: 'op-1', status: 'pending_reconciliation', task_id: 'task-1', output_refs: [] });
    render(<VideoComposeNode id="compose" type="videoComposeNode" data={{ displayName: 'Compose', remixManifestRef: { artifactId: 'manifest', revision: 2 }, remixInputFingerprint: 'current-fingerprint', remixInputsCurrent: true, remixNodeRevision: 2, remixOperationId: 'op-1', unresolvedRequiredEvents: [], remixEvents: [], remixSelections: [] }} selected={false} draggable={true} dragging={false} selectable={true} deletable={true} zIndex={0} isConnectable={true} positionAbsoluteX={0} positionAbsoluteY={0} />);
    expect(await screen.findByRole('button', { name: '重新检查任务' })).toBeEnabled();
    expect(screen.getByRole('button', { name: '强制重新导出' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: '重新检查任务' }));
    await waitFor(() => expect(reconcileRemixOperation).toHaveBeenCalledWith('demo', 'op-1'));
  });

  it('stops polling a stale render and only creates a successor after explicit regeneration', async () => {
    let pollAgain: (() => void) | undefined;
    const interval = vi.spyOn(window, 'setInterval').mockImplementation(((callback: TimerHandler) => {
      pollAgain = callback as () => void;
      return 42;
    }) as typeof window.setInterval);
    fetchRemixArtifact.mockResolvedValue({ id: 'manifest', schema: 'composition_manifest.v1', revision: 2, upstream_refs: [], payload: {
      output_spec: { width: 1080, height: 1920, fps: 30, duration_frames: 60 }, media_bindings: [], layers: [], subtitle_events: [], audio_events: [], manual_overrides: [], source_versions: [],
    } });
    fetchRemixOperation.mockResolvedValue({ operation_id: 'op-stale', status: 'stale', task_id: 'task-old', output_refs: [] });
    renderRemixManifest.mockResolvedValue({ operation_id: 'op-successor', task_key: 'render-successor', task_type: 'remix_render', task_id: 'task-new', job_id: 'job-new' });

    render(<VideoComposeNode id="compose" type="videoComposeNode" data={{ displayName: 'Compose', remixManifestRef: { artifactId: 'manifest', revision: 2 }, remixInputFingerprint: 'current-fingerprint', remixInputsCurrent: true, remixNodeRevision: 2, remixOperationId: 'op-stale', unresolvedRequiredEvents: [], remixEvents: [], remixSelections: [] }} selected={false} draggable={true} dragging={false} selectable={true} deletable={true} zIndex={0} isConnectable={true} positionAbsoluteX={0} positionAbsoluteY={0} />);

    expect(await screen.findByText('导出结果已过期，请明确重新导出。')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'videoCompose.node.remixSubmit' })).toBeDisabled();
    expect(fetchRemixOperation).toHaveBeenCalledTimes(1);
    await act(async () => { pollAgain?.(); pollAgain?.(); });
    expect(fetchRemixOperation).toHaveBeenCalledTimes(1);
    expect(renderRemixManifest).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: '强制重新导出' }));
    await waitFor(() => expect(renderRemixManifest).toHaveBeenCalledWith('demo', expect.objectContaining({ regenerate: true })));
    expect(canvasActions.updateNodeData).toHaveBeenCalledWith('compose', { remixOperationId: 'op-successor' });
    interval.mockRestore();
  });
  it('publishes one manifest under StrictMode while preparation is pending', async () => {
    let release!: (value: { manifestRef: { artifactId: string; revision: number }; events: []; selections: []; unresolvedRequiredEvents: [] }) => void;
    prepareNodeRemix.mockImplementation(() => new Promise((resolve) => { release = resolve; }));
    render(<StrictMode><VideoComposeNode id="compose" type="videoComposeNode" data={{ displayName: 'Compose', autoPrepareRequested: true }} selected={false} draggable={true} dragging={false} selectable={true} deletable={true} zIndex={0} isConnectable={true} positionAbsoluteX={0} positionAbsoluteY={0} /></StrictMode>);
    expect(document.querySelector('[data-handle="target"]')).toBeTruthy();
    expect(document.querySelector('[data-handle="source"]')).toBeTruthy();
    await waitFor(() => expect(prepareNodeRemix).toHaveBeenCalled());
    expect(prepareNodeRemix).toHaveBeenCalledTimes(1);
    release({ manifestRef: { artifactId: 'manifest', revision: 1 }, events: [], selections: [], unresolvedRequiredEvents: [] });
  });

  it('does not export an already prepared manifest after narration is cleared', async () => {
    fetchRemixArtifact.mockResolvedValue({ id: 'manifest', schema: 'composition_manifest.v1', revision: 1, upstream_refs: [], payload: {
      output_spec: { width: 1080, height: 1920, fps: 30, duration_frames: 60 }, media_bindings: [], layers: [], subtitle_events: [], audio_events: [], manual_overrides: [], source_versions: [],
    } });
    graphState.nodes = [{ id: 'audio', type: 'audioNode', data: { audioUrl: null, remixMedia: { media_hash: 'old' } } }];
    remixGraphFingerprint.mockReturnValue('cleared-narration-fingerprint');
    render(<VideoComposeNode id="compose" type="videoComposeNode" data={{
      displayName: 'Compose', remixManifestRef: { artifactId: 'manifest', revision: 1 },
      remixInputFingerprint: 'prepared-with-audio-fingerprint', remixNodeRevision: 1,
      unresolvedRequiredEvents: [], remixEvents: [], remixSelections: [],
    }} selected={false} draggable={true} dragging={false} selectable={true} deletable={true} zIndex={0} isConnectable={true} positionAbsoluteX={0} positionAbsoluteY={0} />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'videoCompose.node.remixSubmit' })).toBeDisabled());
    expect(renderRemixManifest).not.toHaveBeenCalled();
  });

  it('shows exact-result preview and MP4, JSON, and SRT downloads for the rendered revision', async () => {
    fetchRemixArtifact.mockResolvedValue({ id: 'manifest', schema: 'composition_manifest.v1', revision: 2, upstream_refs: [], payload: {
      output_spec: { width: 1080, height: 1920, fps: 30, duration_frames: 60 }, media_bindings: [], layers: [],
      subtitle_events: [{ text: '咖啡', start_frame: 0, end_frame: 30 }],
      audio_events: [{ media_id: 'voice', role: 'narration', start_frame: 0, end_frame: 60, volume: 1 }], manual_overrides: [], source_versions: [],
    } });
    render(<VideoComposeNode id="compose" type="videoComposeNode" data={{
      displayName: 'Compose', remixManifestRef: { artifactId: 'manifest', revision: 2 },
      remixRenderManifestRef: { artifactId: 'manifest', revision: 2 }, remixInputFingerprint: 'current-fingerprint',
      remixNodeRevision: 2, unresolvedRequiredEvents: [], remixEvents: [], remixSelections: [],
      resultVideoUrl: '/api/v1/projects/demo/media/freezone/remix_renders/output.mp4',
    }} selected={false} draggable={true} dragging={false} selectable={true} deletable={true} zIndex={0} isConnectable={true} positionAbsoluteX={0} positionAbsoluteY={0} />);
    expect(await screen.findByText('预览与导出一致')).toBeTruthy();
    expect(screen.getByText('下载 MP4').getAttribute('href')).toContain('/files/');
    expect(screen.getByText('下载 JSON').getAttribute('download')).toBe('manifest-r2.json');
    expect(screen.getByText('下载 SRT').getAttribute('href')).toContain('application/x-subrip');
  });

  it('never exposes the old draft or downloads while a new manifest ref is loading', async () => {
    let resolveNext!: (value: unknown) => void;
    fetchRemixArtifact
      .mockResolvedValueOnce({ id: 'old', schema: 'composition_manifest.v1', revision: 1, upstream_refs: [], payload: {
        output_spec: { width: 1080, height: 1920, fps: 30, duration_frames: 60 }, media_bindings: [], layers: [], subtitle_events: [{ text: '旧字幕', start_frame: 0, end_frame: 30 }], audio_events: [], manual_overrides: [], source_versions: [],
      } })
      .mockImplementationOnce(() => new Promise((resolve) => { resolveNext = resolve; }));
    const base = { displayName: 'Compose', remixInputFingerprint: 'current-fingerprint', remixNodeRevision: 1, unresolvedRequiredEvents: [], remixEvents: [], remixSelections: [], resultVideoUrl: '/api/v1/projects/demo/media/out.mp4' };
    const view = render(<VideoComposeNode id="compose" type="videoComposeNode" data={{ ...base, remixManifestRef: { artifactId: 'old', revision: 1 }, remixRenderManifestRef: { artifactId: 'old', revision: 1 } }} selected={false} draggable={true} dragging={false} selectable={true} deletable={true} zIndex={0} isConnectable={true} positionAbsoluteX={0} positionAbsoluteY={0} />);
    expect(await screen.findByDisplayValue('旧字幕')).toBeTruthy();
    view.rerender(<VideoComposeNode id="compose" type="videoComposeNode" data={{ ...base, remixManifestRef: { artifactId: 'new', revision: 2 }, remixRenderManifestRef: { artifactId: 'new', revision: 2 } }} selected={false} draggable={true} dragging={false} selectable={true} deletable={true} zIndex={0} isConnectable={true} positionAbsoluteX={0} positionAbsoluteY={0} />);
    expect(screen.queryByDisplayValue('旧字幕')).toBeNull();
    expect(screen.queryByText('预览成片')).toBeNull();
    expect(screen.queryByText('下载 MP4')).toBeNull();
    expect(screen.queryByText('下载 JSON')).toBeNull();
    expect(screen.getByRole('button', { name: 'videoCompose.node.remixSubmit' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '更新清单' })).toBeDisabled();
    resolveNext({ id: 'new', schema: 'composition_manifest.v1', revision: 2, upstream_refs: [], payload: { output_spec: { width: 1080, height: 1920, fps: 30, duration_frames: 60 }, media_bindings: [], layers: [], subtitle_events: [], audio_events: [], manual_overrides: [], source_versions: [] } });
  });

  it('regenerates from graph inputs after the saved manifest fails to load', async () => {
    fetchRemixArtifact.mockRejectedValue(new Error('artifact missing'));
    prepareNodeRemix.mockResolvedValue({ manifestRef: { artifactId: 'replacement', revision: 1 }, events: [], selections: [], unresolvedRequiredEvents: [] });
    render(<VideoComposeNode id="compose" type="videoComposeNode" data={{
      displayName: 'Compose', remixManifestRef: { artifactId: 'missing', revision: 3 },
      remixManifestLoadState: 'failed', remixInputFingerprint: 'current-fingerprint', remixNodeRevision: 3,
      unresolvedRequiredEvents: [], remixEvents: [], remixSelections: [],
    }} selected={false} draggable={true} dragging={false} selectable={true} deletable={true} zIndex={0} isConnectable={true} positionAbsoluteX={0} positionAbsoluteY={0} />);
    const regenerate = await screen.findByRole('button', { name: '重新生成合成清单' });
    expect(regenerate).toBeEnabled();
    fireEvent.click(regenerate);
    await waitFor(() => expect(prepareNodeRemix).toHaveBeenCalledWith('demo', graphState.nodes, graphState.edges, 'compose', null));
  });

  it('does not let an unrelated edit dismiss unresolved override conflicts', async () => {
    fetchRemixArtifact.mockResolvedValue({ id: 'manifest', schema: 'composition_manifest.v1', revision: 2, upstream_refs: [], payload: {
      output_spec: { width: 1080, height: 1920, fps: 30, duration_frames: 60 }, media_bindings: [], layers: [], subtitle_events: [{ text: '当前字幕', start_frame: 0, end_frame: 30 }], audio_events: [], manual_overrides: [], source_versions: [],
    } });
    render(<VideoComposeNode id="compose" type="videoComposeNode" data={{ displayName: 'Compose', remixManifestRef: { artifactId: 'manifest', revision: 2 }, remixInputFingerprint: 'current-fingerprint', remixNodeRevision: 2, unresolvedRequiredEvents: [], remixManifestConflicts: ['旧画面覆盖与新时间轴冲突'], remixEvents: [], remixSelections: [] }} selected={false} draggable={true} dragging={false} selectable={true} deletable={true} zIndex={0} isConnectable={true} positionAbsoluteX={0} positionAbsoluteY={0} />);
    fireEvent.change(await screen.findByLabelText('字幕 1'), { target: { value: '无关修改' } });
    expect(screen.getByRole('button', { name: '保存清单修改' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'videoCompose.node.remixSubmit' })).toBeDisabled();
    expect(screen.getByText(/旧画面覆盖与新时间轴冲突/)).toBeTruthy();
    expect(reviseRemixArtifact).not.toHaveBeenCalled();
  });

  it('offers per-conflict safe rebase and bulk accept-upstream controls', async () => {
    const override = { target: 'subtitle_events' as const, identity: { start_frame: 0, end_frame: 30 }, field: 'text', base_value: '旧字幕', value: '手工字幕' };
    fetchRemixArtifact.mockResolvedValue({ id: 'manifest', schema: 'composition_manifest.v1', revision: 2, upstream_refs: [], payload: {
      output_spec: { width: 1080, height: 1920, fps: 30, duration_frames: 60 }, media_bindings: [], layers: [], subtitle_events: [{ text: '新上游字幕', start_frame: 0, end_frame: 30 }], audio_events: [], manual_overrides: [override], source_versions: [],
    } });
    const conflict = { key: 'subtitle_events:text:{"start_frame":0,"end_frame":30}', message: 'subtitle_events.text 的上游值已变化', override, canRebase: true };
    render(<VideoComposeNode id="compose" type="videoComposeNode" data={{ displayName: 'Compose', remixManifestRef: { artifactId: 'manifest', revision: 2 }, remixInputFingerprint: 'current-fingerprint', remixNodeRevision: 2, unresolvedRequiredEvents: [], remixManifestConflicts: [conflict], remixEvents: [], remixSelections: [] }} selected={false} draggable={true} dragging={false} selectable={true} deletable={true} zIndex={0} isConnectable={true} positionAbsoluteX={0} positionAbsoluteY={0} />);
    expect(await screen.findByRole('button', { name: '保留手工值' })).toBeEnabled();
    expect(screen.getByRole('button', { name: '采用上游值' })).toBeEnabled();
    expect(screen.getByRole('button', { name: '全部采用上游' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: '保留手工值' }));
    expect(await screen.findByDisplayValue('手工字幕')).toBeTruthy();
    expect(canvasActions.updateNodeData).toHaveBeenCalledWith('compose', expect.objectContaining({ remixManifestConflicts: [], remixManifestDirty: true }));
  });

  it('blocks recompile while manifest overrides are unsaved', async () => {
    fetchRemixArtifact.mockResolvedValue({ id: 'manifest', schema: 'composition_manifest.v1', revision: 2, upstream_refs: [], payload: {
      output_spec: { width: 1080, height: 1920, fps: 30, duration_frames: 60 }, media_bindings: [], layers: [], subtitle_events: [{ text: '保存前', start_frame: 0, end_frame: 30 }], audio_events: [], manual_overrides: [], source_versions: [],
    } });
    render(<VideoComposeNode id="compose" type="videoComposeNode" data={{ displayName: 'Compose', remixManifestRef: { artifactId: 'manifest', revision: 2 }, remixInputFingerprint: 'current-fingerprint', remixNodeRevision: 2, unresolvedRequiredEvents: [], remixEvents: [], remixSelections: [] }} selected={false} draggable={true} dragging={false} selectable={true} deletable={true} zIndex={0} isConnectable={true} positionAbsoluteX={0} positionAbsoluteY={0} />);
    fireEvent.change(await screen.findByLabelText('字幕 1'), { target: { value: '尚未保存' } });
    expect(screen.getByRole('button', { name: '更新清单' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '更新清单' }));
    expect(prepareNodeRemix).not.toHaveBeenCalled();
    expect(screen.getByDisplayValue('尚未保存')).toBeTruthy();
  });

  it('reacts to a deep ancestor fingerprint change and immediately marks the preview stale', async () => {
    fetchRemixArtifact.mockResolvedValue({ id: 'manifest', schema: 'composition_manifest.v1', revision: 2, upstream_refs: [], payload: {
      output_spec: { width: 1080, height: 1920, fps: 30, duration_frames: 60 }, media_bindings: [], layers: [], subtitle_events: [], audio_events: [], manual_overrides: [], source_versions: [],
    } });
    render(<VideoComposeNode id="compose" type="videoComposeNode" data={{ displayName: 'Compose', remixManifestRef: { artifactId: 'manifest', revision: 2 }, remixRenderManifestRef: { artifactId: 'manifest', revision: 2 }, remixInputFingerprint: 'current-fingerprint', remixNodeRevision: 2, unresolvedRequiredEvents: [], remixEvents: [], remixSelections: [], resultVideoUrl: '/api/v1/projects/demo/media/out.mp4' }} selected={false} draggable={true} dragging={false} selectable={true} deletable={true} zIndex={0} isConnectable={true} positionAbsoluteX={0} positionAbsoluteY={0} />);
    expect(await screen.findByText('预览与导出一致')).toBeTruthy();
    remixGraphFingerprint.mockReturnValue('deep-ancestor-revision-changed');
    act(() => graphState.listeners.forEach((listener) => listener()));
    expect(await screen.findByText('预览已过期')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'videoCompose.node.remixSubmit' })).toBeDisabled();
  });

  it('locks manifest inputs while a delayed revision save is in flight', async () => {
    let finishSave!: (value: unknown) => void;
    const payload = { output_spec: { width: 1080, height: 1920, fps: 30, duration_frames: 60 }, media_bindings: [], layers: [], subtitle_events: [{ text: '初稿', start_frame: 0, end_frame: 30 }], audio_events: [], manual_overrides: [], source_versions: [] };
    fetchRemixArtifact.mockResolvedValue({ id: 'manifest', schema: 'composition_manifest.v1', revision: 2, upstream_refs: [], payload });
    reviseRemixArtifact.mockImplementation(() => new Promise((resolve) => { finishSave = resolve; }));
    render(<VideoComposeNode id="compose" type="videoComposeNode" data={{ displayName: 'Compose', remixManifestRef: { artifactId: 'manifest', revision: 2 }, remixInputFingerprint: 'current-fingerprint', remixNodeRevision: 2, unresolvedRequiredEvents: [], remixEvents: [], remixSelections: [] }} selected={false} draggable={true} dragging={false} selectable={true} deletable={true} zIndex={0} isConnectable={true} positionAbsoluteX={0} positionAbsoluteY={0} />);
    const input = await screen.findByLabelText('字幕 1');
    fireEvent.change(input, { target: { value: '准备保存' } });
    fireEvent.click(screen.getByRole('button', { name: '保存清单修改' }));
    expect(input).toBeDisabled();
    fireEvent.change(input, { target: { value: '保存过程中又改了' } });
    expect(input).toHaveValue('准备保存');
    await act(async () => finishSave({ id: 'manifest', schema: 'composition_manifest.v1', revision: 3, upstream_refs: [], payload: { ...payload, subtitle_events: [{ text: '准备保存', start_frame: 0, end_frame: 30 }] } }));
  });
});
