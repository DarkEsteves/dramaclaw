import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useCanvasStore } from '@/stores/canvasStore';
import type { ReferenceDirectorState } from '@/api/referenceDirector';
import { getReferenceDirectorState, confirmReferenceDecision, confirmReferenceSpeechCorrection, confirmReferenceMaterialReview, confirmReferenceOutputReview, associateReferenceMaterialUpload, getReferenceMaterialSpeechStatus, saveReferenceMaterialSpeechCorrection, confirmReferenceMaterialReviewBatch } from '@/api/referenceDirector';
import { REFERENCE_ANALYSIS_STATUS_EVENT } from '@/features/freezone/referenceAnalysisEvents';
import { ReferenceDirectorPanel } from './ReferenceDirectorPanel';
const scope = vi.hoisted(() => ({ current: true }));
vi.mock('@/api/referenceDirector', async (original) => ({ ...await original<typeof import('@/api/referenceDirector')>(), getReferenceDirectorState: vi.fn(), confirmReferenceDecision: vi.fn(), confirmReferenceSpeechCorrection: vi.fn(), confirmReferenceMaterialReview: vi.fn(), confirmReferenceOutputReview: vi.fn(), associateReferenceMaterialUpload: vi.fn(), getReferenceMaterialSpeechStatus: vi.fn(), saveReferenceMaterialSpeechCorrection: vi.fn(), confirmReferenceMaterialReviewBatch: vi.fn() }));
const canvasSync = vi.hoisted(() => ({ baseline: { nodes: [] as unknown[], edges: [] as unknown[], revision: 1 } }));
vi.mock('@/features/freezone/canvasSyncRuntime', () => ({ flushFreezoneCanvasRuntime: vi.fn(async () => true), captureFreezoneCanvasScope: () => () => scope.current, getFreezoneCanvasBaseline: () => canvasSync.baseline, applyRemoteFreezoneCanvas: vi.fn(() => true) }));
vi.mock('@/api/canvas', () => ({ getFreezoneCanvas: vi.fn() }));
import { getFreezoneCanvas } from '@/api/canvas';
import { flushFreezoneCanvasRuntime, applyRemoteFreezoneCanvas } from '@/features/freezone/canvasSyncRuntime';
const props = { projectId: 'project', canvasId: 'canvas', agentId: 'main', connected: true };
const empty = (): ReferenceDirectorState => ({ commissions: [], decisions: [], material_work: [], speech_corrections: [] });
const state = (): ReferenceDirectorState => ({ ...empty(), commissions: [{ commission_ref: { artifact_id: 'commission', revision: 1 }, project_id: 'project', canvas_id: 'canvas', phase: 'awaiting_direction', goal: '复刻产品问答', analysis_node_id: 'story' }], decisions: [{ decision_ref: { artifact_id: 'direction', revision: 1 }, commission_ref: { artifact_id: 'commission', revision: 1 }, kind: 'direction', status: 'pending', summary: '保留双人问答；换成用户产品；重新制作人物', proposal: { keep: ['双人问答', '反转结尾'], replace: ['产品'] } }] });
const preview = () => ({ preview_ref: { artifact_id: 'preview', revision: 1 }, source_plan_ref: { artifact_id: 'plan', revision: 1 }, node_id: 'plan-node', take_id: 'take', lexical_change: true, blockers: [], corrections: [{ word_id: 'w1', text: '品牌', reason: '原声正确，识别同音错字' }], corrected_words: [{ id: 'w1', text: '品牌', start_us: 0, end_us: 100000 }], cues: [{ turn_id: 'turn', text: '品牌你好' }] });
const seedPlan = () => useCanvasStore.setState({ nodes: [{ id: 'plan-node', type: 'scriptNode', position: { x: 0, y: 0 }, data: { remixRole: 'production_plan', productionPlanV2Ref: { artifactId: 'plan', revision: 1 }, productionPlanV2Draft: { goal: '原方案' }, productionPlanV2Dirty: false } }, { id: 'compose', type: 'videoComposeNode', position: { x: 0, y: 0 }, data: { productionPlanNodeId: 'plan-node', resultVideoUrl: 'preview.mp4' } }] as never, edges: [] });
const saved = { plan: { id: 'plan', revision: 2, schema: 'production_plan.v2', payload: { goal: '校正后的方案' } }, node_id: 'plan-node' };
const emit = (canvas = 'canvas') => act(() => window.dispatchEvent(new CustomEvent(REFERENCE_ANALYSIS_STATUS_EVENT, { detail: { run: { project_id: 'project', canvas_id: canvas, agent_id: 'main' } } })));
describe('ReferenceDirectorPanel', () => {
  beforeEach(() => { vi.clearAllMocks(); scope.current = true; useCanvasStore.setState({ nodes: [], edges: [] }); vi.mocked(getReferenceDirectorState).mockResolvedValue(empty()); });
  it('hides empty progress and restores it when a scoped production starts', async () => {
    render(<ReferenceDirectorPanel {...props} />);
    await waitFor(() => expect(getReferenceDirectorState).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole('region', { name: '虾导制作进度' })).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
    vi.mocked(getReferenceDirectorState).mockResolvedValue(state());
    emit();
    await screen.findByText('等待确认复刻方向');
    expect(getReferenceDirectorState).toHaveBeenCalledTimes(2);
  });
  it('keeps direction approval in ordinary chat instead of rendering a direction card', async () => {
    vi.mocked(getReferenceDirectorState).mockResolvedValue(state());
    render(<ReferenceDirectorPanel {...props} />);
    await screen.findByText('等待确认复刻方向');
    expect(screen.queryByRole('button', { name: '确认复刻方向' })).toBeNull();
    expect(screen.queryByText(/保留双人问答/)).toBeNull();
    expect(confirmReferenceDecision).not.toHaveBeenCalled();
  });
  it('loads only on reconnect, scoped status events and manual refresh, without polling', async () => {
    vi.mocked(getReferenceDirectorState).mockResolvedValue(state());
    const view = render(<ReferenceDirectorPanel {...props} />);
    await screen.findByText('等待确认复刻方向');
    emit('other');
    expect(getReferenceDirectorState).toHaveBeenCalledTimes(1);
    emit();
    await waitFor(() => expect(getReferenceDirectorState).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole('button', { name: '刷新制作状态' }));
    await waitFor(() => expect(getReferenceDirectorState).toHaveBeenCalledTimes(3));
    view.rerender(<ReferenceDirectorPanel {...props} connected={false} />);
    view.rerender(<ReferenceDirectorPanel {...props} connected />);
    await waitFor(() => expect(getReferenceDirectorState).toHaveBeenCalledTimes(4));
  });
  it('ignores a late state response after switching canvases', async () => {
    let release!: (value: ReferenceDirectorState) => void;
    vi.mocked(getReferenceDirectorState).mockReturnValueOnce(new Promise((resolve) => { release = resolve; }));
    const view = render(<ReferenceDirectorPanel {...props} />);
    view.rerender(<ReferenceDirectorPanel {...props} canvasId="other" />);
    await act(async () => { release(state()); });
    expect(screen.queryByText('等待确认复刻方向')).toBeNull();
  });
  it('lists dependencies and required uploads and focuses existing nodes', async () => {
    useCanvasStore.setState({ nodes: [{ id: 'upload-node', type: 'imageNode', position: { x: 0, y: 0 }, data: {} }] as never });
    vi.mocked(getReferenceDirectorState).mockResolvedValue({ ...state(), decisions: [], material_work: [{ work_ref: { artifact_id: 'work', revision: 1 }, plan_ref: { artifact_id: 'plan', revision: 1 }, requirements: [{ id: 'final', description: '产品特写视频', final_step_id: 'clip' }], steps: [{ id: 'product', label: '产品包装图', source: 'upload', status: 'awaiting_upload', node_id: 'upload-node', depends_on: [] }, { id: 'clip', label: '产品特写', source: 'generate', status: 'planned', depends_on: ['product'] }] }] });
    render(<ReferenceDirectorPanel {...props} />);
    await screen.findByText('等待上传');
    expect(screen.getByText(/依赖：产品包装图/)).toBeTruthy();
    expect(screen.getByText('产品特写视频')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '查看产品包装图' }));
    expect(useCanvasStore.getState().selectedNodeId).toBe('upload-node');
  });
  it('keeps material work internal in conversation mode while showing real media review', async () => {
    vi.mocked(getReferenceDirectorState).mockResolvedValue({ ...empty(),
      material_work: [{ work_ref: { artifact_id: 'work', revision: 1 },
        plan_ref: { artifact_id: 'plan', revision: 1 },
        requirements: [{ id: 'final', description: 'A planned clip', final_step_id: 'clip' }],
        steps: [{ id: 'clip', label: 'Planned clip', source: 'generate', status: 'planned', depends_on: [] }] }],
      material_reviews: [{ review_ref: { artifact_id: 'review', revision: 1 },
        work_ref: { artifact_id: 'work', revision: 1 }, step_id: 'clip',
        resource: { media_path: 'media/clip.png', media_kind: 'image', node_id: 'image' },
        technical: { status: 'passed', checks: [], findings: [] },
        content: { status: 'unavailable', checks: [], findings: ['Review actual output'] } }],
    } as ReferenceDirectorState);
    render(<ReferenceDirectorPanel {...props} conversationMode />);
    await screen.findByText('Review actual output');
    expect(screen.queryByText('素材制作依赖')).toBeNull();
    expect(screen.queryByText('A planned clip')).toBeNull();
    expect(screen.getByRole('img', { name: '待审看素材' })).toBeTruthy();
  });
  it('shows speech changes and applies them once while preserving the linked preview', async () => {
    seedPlan();
    vi.mocked(getReferenceDirectorState).mockResolvedValueOnce({ ...state(), decisions: [], speech_corrections: [preview()] }).mockResolvedValue(empty());
    vi.mocked(confirmReferenceSpeechCorrection).mockResolvedValue(saved);
    render(<ReferenceDirectorPanel {...props} />);
    await screen.findByText('品牌你好');
    expect(screen.getByText(/原声正确/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '一次校对并应用' }));
    await waitFor(() => expect(useCanvasStore.getState().nodes[0].data.productionPlanV2Ref).toEqual({ artifactId: 'plan', revision: 2 }));
    expect(confirmReferenceSpeechCorrection).toHaveBeenCalledWith('project', 'canvas', 'preview');
    expect(useCanvasStore.getState().nodes[1].data).toMatchObject({ resultVideoUrl: 'preview.mp4', remixInputsCurrent: false });
  });
  it('keeps edits made during speech confirmation and displays a conflict', async () => {
    seedPlan();
    vi.mocked(getReferenceDirectorState).mockResolvedValue({ ...empty(), speech_corrections: [preview()] });
    vi.mocked(confirmReferenceSpeechCorrection).mockImplementation(async () => { useCanvasStore.getState().updateNodeData('plan-node', { productionPlanV2Dirty: true, productionPlanV2Draft: { goal: '我的修改' } }); return saved; });
    render(<ReferenceDirectorPanel {...props} />);
    fireEvent.click(await screen.findByRole('button', { name: '一次校对并应用' }));
    await screen.findByText(/本地修改已保留/);
    expect(useCanvasStore.getState().nodes[0].data.productionPlanV2Draft).toEqual({ goal: '我的修改' });
    expect(useCanvasStore.getState().nodes[0].data.productionPlanV2Ref).toEqual({ artifactId: 'plan', revision: 1 });
  });
  it('blocks stale previews and missing-word evidence before any speech mutation', async () => {
    seedPlan();
    useCanvasStore.getState().updateNodeData('plan-node', { productionPlanV2Ref: { artifactId: 'plan', revision: 3 } });
    vi.mocked(getReferenceDirectorState).mockResolvedValue({ ...empty(), speech_corrections: [preview()] });
    render(<ReferenceDirectorPanel {...props} />);
    fireEvent.click(await screen.findByRole('button', { name: '一次校对并应用' }));
    await screen.findByText(/方案版本已变化/);
    expect(confirmReferenceSpeechCorrection).not.toHaveBeenCalled();
  });
  it('does not backfill speech into a new canvas session', async () => {
    seedPlan();
    vi.mocked(getReferenceDirectorState).mockResolvedValue({ ...empty(), speech_corrections: [preview()] });
    vi.mocked(confirmReferenceSpeechCorrection).mockImplementation(async () => { scope.current = false; seedPlan(); return saved; });
    render(<ReferenceDirectorPanel {...props} />);
    fireEvent.click(await screen.findByRole('button', { name: '一次校对并应用' }));
    await waitFor(() => expect(confirmReferenceSpeechCorrection).toHaveBeenCalledTimes(1));
    expect(useCanvasStore.getState().nodes[0].data.productionPlanV2Ref).toEqual({ artifactId: 'plan', revision: 1 });
  });
});


it('does not offer to apply a preview with missing-word blockers', async () => {
  vi.clearAllMocks(); scope.current = true; seedPlan();
  vi.mocked(getReferenceDirectorState).mockResolvedValue({ ...empty(), speech_corrections: [{ ...preview(), blockers: ['实际素材缺少方案台词，需要返工素材'] }] });
  render(<ReferenceDirectorPanel {...props} />);
  await screen.findByText('实际素材缺少方案台词，需要返工素材');
  const button = screen.getByRole('button', { name: '一次校对并应用' });
  expect(button.hasAttribute('disabled')).toBe(true);
  fireEvent.click(button);
  expect(confirmReferenceSpeechCorrection).not.toHaveBeenCalled();
});


it('shows the original word, corrected word and actual source playback', async () => {
  vi.clearAllMocks(); scope.current = true; seedPlan();
  vi.mocked(getReferenceDirectorState).mockResolvedValue({ ...empty(), speech_corrections: [{ ...preview(), original_words: [{ id: 'w1', text: '品排', start_us: 0, end_us: 100000 }], source_url: '/static/projects/project/media/dialogue.mp4', source_node_id: 'video', target_text: '主持人：品牌你好' }] });
  const view = render(<ReferenceDirectorPanel {...props} />);
  await screen.findByText('品排 → 品牌');
  expect(screen.getByText('主持人：品牌你好')).toBeTruthy();
  expect(view.container.querySelector('audio')?.getAttribute('src')).toBe('/static/projects/project/media/dialogue.mp4');
});

it('shows separate material checks and confirms user viewing once', async () => {
  vi.clearAllMocks(); scope.current = true;
  const review = { review_ref: { artifact_id: 'review', revision: 1 }, work_ref: { artifact_id: 'work', revision: 1 }, step_id: 'product', resource: { media_path: 'media/product.png', media_kind: 'image' as const, node_id: 'image' }, technical: { status: 'passed' as const, checks: ['文件可解码'], findings: [] }, content: { status: 'unavailable' as const, checks: [], findings: ['需要核对产品包装'] } };
  vi.mocked(getReferenceDirectorState).mockResolvedValueOnce({ ...empty(), material_reviews: [review] }).mockResolvedValue(empty());
  vi.mocked(confirmReferenceMaterialReview).mockResolvedValue({ review_ref: { artifact_id: 'review', revision: 2 } });
  render(<ReferenceDirectorPanel {...props} />);
  await screen.findByText('需要核对产品包装');
  expect(screen.getByText('文件可解码')).toBeTruthy();
  expect(screen.getByRole('img', { name: '待审看素材' }).getAttribute('src')).toBe('/static/projects/project/media/product.png');
  fireEvent.click(screen.getByRole('button', { name: '我已审看，确认素材内容' }));
  await waitFor(() => expect(confirmReferenceMaterialReview).toHaveBeenCalledWith('project', 'canvas', 'review'));
  await waitFor(() => expect(getReferenceDirectorState).toHaveBeenCalledTimes(2));
});


it('replaces host check codes and English fallbacks with a readable review hint', async () => {
  vi.clearAllMocks(); scope.current = true;
  const review = { review_ref: { artifact_id: 'review', revision: 1 }, work_ref: { artifact_id: 'work', revision: 1 }, step_id: 'product',
    resource: { media_path: 'media/product.mp4', media_kind: 'video' as const, node_id: 'video' },
    technical: { status: 'passed' as const, checks: ['current_canvas_node', 'project_file_hash', 'native_media_probe'], findings: [] },
    content: { status: 'unavailable' as const, checks: [], findings: ['Actual content inspection unavailable; review the current material and confirm.'] } };
  vi.mocked(getReferenceDirectorState).mockResolvedValue({ ...empty(), material_reviews: [review] });
  vi.mocked(getReferenceMaterialSpeechStatus).mockResolvedValue({ status: 'pending', message: '识别进行中' });
  const view = render(<ReferenceDirectorPanel {...props} />);
  const card = await screen.findByRole('article', { name: '素材确认' });
  // The conclusion is visible; machine codes live behind the disclosure.
  expect(within(card).getByText('文件可用')).toBeTruthy();
  expect(within(card).getByText('请完整观看并试听，重点确认画面主体与声音是否符合预期。')).toBeTruthy();
  expect(within(card).queryByText('current_canvas_node')).toBeNull();
  expect(within(card).queryByText(/Actual content inspection unavailable/)).toBeNull();
  fireEvent.click(within(card).getByText('检查详情'));
  expect(within(card).getByText('画布素材与当前节点一致')).toBeTruthy();
  expect(within(card).getByText('文件完整未变')).toBeTruthy();
  expect(within(card).getByText('系统未做自动内容判断，请以你的审看为准。')).toBeTruthy();
  // Exactly one primary confirmation action; the batch selector stays collapsed.
  const confirm = within(card).getAllByRole('button', { name: '我已审看，确认素材内容' });
  expect(confirm).toHaveLength(1);
  expect(confirm[0].className).toContain('tap-button-primary');
  const batch = within(card).getByText('一次确认本阶段的全部素材');
  expect(batch.closest('details')?.hasAttribute('open')).toBe(false);
  view.unmount();
});

it('restores standalone director updates only for the current project canvas and agent', async () => {
  vi.clearAllMocks(); scope.current = true;
  vi.mocked(getReferenceDirectorState).mockResolvedValue(state());
  render(<ReferenceDirectorPanel {...props} />);
  await screen.findByText('等待确认复刻方向');
  const update = (detail: Record<string, string>) => act(() => window.dispatchEvent(new CustomEvent('reference-director-status', { detail })));
  update({ project_id: 'other', canvas_id: 'canvas', agent_id: 'main' });
  update({ project_id: 'project', canvas_id: 'other', agent_id: 'main' });
  update({ project_id: 'project', canvas_id: 'canvas', agent_id: 'other' });
  expect(getReferenceDirectorState).toHaveBeenCalledTimes(1);
  update({ project_id: 'project', canvas_id: 'canvas', agent_id: 'main' });
  await waitFor(() => expect(getReferenceDirectorState).toHaveBeenCalledTimes(2));
  update({ project_id: 'project', canvas_id: 'canvas' });
  await waitFor(() => expect(getReferenceDirectorState).toHaveBeenCalledTimes(3));
});


const outputReview = () => ({ review_ref: { artifact_id: 'output', revision: 1 }, commission_ref: { artifact_id: 'commission', revision: 4 }, manifest_ref: { artifact_id: 'manifest', revision: 1 }, plan_ref: { artifact_id: 'plan', revision: 2 }, compose_node_id: 'compose', media_url: '/static/projects/project/output/final.mp4', status: 'needs_review' as const, technical: { status: 'passed' as const, checks: ['实际文件可播放'], findings: [] }, content: { status: 'unavailable' as const, checks: [], findings: ['等待用户检查完整声画'] }, findings: [] });
it('plays actual output and requires whole-media viewing before confirming once', async () => {
  vi.clearAllMocks(); scope.current = true;
  vi.mocked(getReferenceDirectorState).mockResolvedValueOnce({ ...empty(), output_reviews: [outputReview()] }).mockResolvedValue(empty());
  vi.mocked(confirmReferenceOutputReview).mockResolvedValue({ review_ref: { artifact_id: 'output', revision: 2 }, review: { ...outputReview(), status: 'passed' }, commission: { ...state().commissions[0], phase: 'completed' } });
  const view = render(<ReferenceDirectorPanel {...props} />);
  await screen.findByText('等待用户检查完整声画');
  expect(view.container.querySelector('video')?.getAttribute('src')).toBe('/static/projects/project/output/final.mp4');
  const button = screen.getByRole('button', { name: '确认成片交付' });
  expect(button.hasAttribute('disabled')).toBe(true);
  fireEvent.click(button);
  expect(confirmReferenceOutputReview).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('checkbox', { name: '已完整观看并试听' }));
  expect(button.hasAttribute('disabled')).toBe(false);
  fireEvent.click(button);
  await waitFor(() => expect(confirmReferenceOutputReview).toHaveBeenCalledWith('project', 'canvas', outputReview().review_ref));
  expect(confirmReferenceOutputReview).toHaveBeenCalledTimes(1);
  await waitFor(() => expect(getReferenceDirectorState).toHaveBeenCalledTimes(2));
});
it('keeps output approval disabled when technical checks fail', async () => {
  vi.clearAllMocks(); scope.current = true;
  vi.mocked(getReferenceDirectorState).mockResolvedValue({ ...empty(), output_reviews: [{ ...outputReview(), technical: { status: 'rejected', checks: [], findings: ['文件没有可用声音轨道'] } }] });
  render(<ReferenceDirectorPanel {...props} />);
  await screen.findByText('文件没有可用声音轨道');
  fireEvent.click(screen.getByRole('checkbox', { name: '已完整观看并试听' }));
  const button = screen.getByRole('button', { name: '确认成片交付' });
  expect(button.hasAttribute('disabled')).toBe(true);
  fireEvent.click(button);
  expect(confirmReferenceOutputReview).not.toHaveBeenCalled();
});
it('focuses a native editable composition and confirms its saved decision', async () => {
  vi.clearAllMocks(); scope.current = true; seedPlan();
  const decision = { ...state().decisions[0], kind: 'composition' as const, summary: '可编辑工程已准备好，请检查画布中的合成内容', proposal: { compose_node_id: 'compose', manifest_ref: { artifact_id: 'manifest', revision: 1 } } };
  vi.mocked(getReferenceDirectorState).mockResolvedValueOnce({ ...empty(), decisions: [decision] }).mockResolvedValue(empty());
  vi.mocked(confirmReferenceDecision).mockResolvedValue({ commission: { ...state().commissions[0], phase: 'completed' } });
  render(<ReferenceDirectorPanel {...props} />);
  await screen.findByText(decision.summary);
  fireEvent.click(screen.getByRole('button', { name: '查看可编辑合成' }));
  expect(useCanvasStore.getState().selectedNodeId).toBe('compose');
  fireEvent.click(screen.getByRole('button', { name: '确认可编辑合成' }));
  await waitFor(() => expect(confirmReferenceDecision).toHaveBeenCalledWith('project', 'canvas', decision.decision_ref.artifact_id));
});


it('safely restores server-created plan nodes on canvas_changed while retaining unrelated local edits', async () => {
  vi.clearAllMocks(); scope.current = true;
  const baseNode = { id: 'local', type: 'imageNode', position: { x: 0, y: 0 }, data: { label: 'old' } };
  const localNode = { ...baseNode, data: { label: 'my edit' } };
  const serverNode = { id: 'new-plan', type: 'scriptNode', position: { x: 200, y: 0 }, data: { remixRole: 'production_plan' } };
  canvasSync.baseline = { nodes: [baseNode], edges: [], revision: 1 };
  useCanvasStore.setState({ nodes: [localNode] as never, edges: [] });
  vi.mocked(getFreezoneCanvas).mockResolvedValue({ revision: 2, nodes: [baseNode, serverNode], edges: [] } as never);
  vi.mocked(getReferenceDirectorState).mockResolvedValue(empty());
  render(<ReferenceDirectorPanel {...props} />);
  await waitFor(() => expect(getReferenceDirectorState).toHaveBeenCalledTimes(1));
  act(() => window.dispatchEvent(new CustomEvent('reference-director-status', { detail: { project_id: 'project', canvas_id: 'canvas', canvas_changed: true } })));
  await waitFor(() => expect(applyRemoteFreezoneCanvas).toHaveBeenCalledTimes(1));
  const merge = vi.mocked(applyRemoteFreezoneCanvas).mock.calls[0][3];
  expect(merge?.([], [], [], [])).toMatchObject({ nodes: [{ id: 'local', data: { label: 'my edit' } }, { id: 'new-plan' }] });
});
it('retains the local canvas and reports a concurrent-content conflict before applying remote changes', async () => {
  vi.clearAllMocks(); scope.current = true;
  const baseNode = { id: 'plan', type: 'scriptNode', position: { x: 0, y: 0 }, data: { script: 'old' } };
  canvasSync.baseline = { nodes: [baseNode], edges: [], revision: 1 };
  const localNode = { ...baseNode, data: { script: 'local' } };
  useCanvasStore.setState({ nodes: [localNode] as never, edges: [] });
  vi.mocked(getFreezoneCanvas).mockResolvedValue({ revision: 2, nodes: [{ ...baseNode, data: { script: 'server' } }], edges: [] } as never);
  vi.mocked(getReferenceDirectorState).mockResolvedValue(empty());
  render(<ReferenceDirectorPanel {...props} />);
  await waitFor(() => expect(getReferenceDirectorState).toHaveBeenCalledTimes(1));
  act(() => window.dispatchEvent(new CustomEvent('reference-director-status', { detail: { project_id: 'project', canvas_id: 'canvas', canvas_changed: true } })));
  await screen.findByText(/同时修改/);
  expect(applyRemoteFreezoneCanvas).not.toHaveBeenCalled();
  expect(useCanvasStore.getState().nodes).toEqual([localNode]);
});


it('does not apply a late server-created canvas after the active session changes', async () => {
  vi.clearAllMocks(); scope.current = true;
  canvasSync.baseline = { nodes: [], edges: [], revision: 1 };
  useCanvasStore.setState({ nodes: [], edges: [] });
  let release!: (value: Awaited<ReturnType<typeof getFreezoneCanvas>>) => void;
  vi.mocked(getFreezoneCanvas).mockReturnValue(new Promise((resolve) => { release = resolve; }));
  vi.mocked(getReferenceDirectorState).mockResolvedValue(empty());
  const view = render(<ReferenceDirectorPanel {...props} />);
  await waitFor(() => expect(getReferenceDirectorState).toHaveBeenCalledTimes(1));
  act(() => window.dispatchEvent(new CustomEvent('reference-director-status', { detail: { project_id: 'project', canvas_id: 'canvas', canvas_changed: true } })));
  await waitFor(() => expect(getFreezoneCanvas).toHaveBeenCalledTimes(1));
  view.rerender(<ReferenceDirectorPanel {...props} canvasId="other" />);
  await act(async () => { release({ revision: 2, nodes: [{ id: 'old-plan', type: 'scriptNode', position: { x: 0, y: 0 }, data: {} }], edges: [] } as never); });
  expect(applyRemoteFreezoneCanvas).not.toHaveBeenCalled();
  expect(useCanvasStore.getState().nodes).toEqual([]);
});

it('renders the backend output review with optional check details omitted', async () => {
  vi.clearAllMocks(); scope.current = true;
  vi.mocked(getReferenceDirectorState).mockResolvedValue({ ...empty(), output_reviews: [{ ...outputReview(), technical: { status: 'passed', checks: ['encoded_dimensions'] }, content: { status: 'unavailable' } }] });
  render(<ReferenceDirectorPanel {...props} />);
  await screen.findByText('画面尺寸');
  expect(screen.getByRole('checkbox', { name: '已完整观看并试听' })).toBeTruthy();
});

 it('associates an uploaded current image with the durable pending requirement after saving the canvas', async () => {
    useCanvasStore.setState({ nodes: [{ id: 'uploaded', type: 'imageNode', position: {x:0,y:0}, data: {imageUrl:'/static/projects/project/picture.png', label:'包装照片'} }] as never });
    vi.mocked(getReferenceDirectorState).mockResolvedValue({ ...empty(), material_work: [{work_ref:{artifact_id:'work',revision:3},plan_ref:{artifact_id:'plan',revision:1},requirements:[{id:'product',description:'产品图',final_step_id:'image'}],steps:[{id:'image',label:'产品图',media_kind:'image',source:'upload',status:'awaiting_upload',depends_on:[]}]}] });
    vi.mocked(associateReferenceMaterialUpload).mockResolvedValue({work_ref:{artifact_id:'work',revision:4}} as never);
    render(<ReferenceDirectorPanel {...props} />);
    fireEvent.change(await screen.findByLabelText('选择产品图素材'), {target:{value:'uploaded'}});
    fireEvent.click(screen.getByRole('button',{name:'关联产品图'}));
    await waitFor(() => expect(associateReferenceMaterialUpload).toHaveBeenCalledWith('project','canvas',{artifact_id:'work',revision:3},'image','uploaded'));
    const flushCallOrder = vi.mocked(flushFreezoneCanvasRuntime).mock.invocationCallOrder;
    const associateCallOrder = vi.mocked(associateReferenceMaterialUpload).mock.invocationCallOrder;
    expect(flushCallOrder[flushCallOrder.length - 1]).toBeLessThan(associateCallOrder[associateCallOrder.length - 1]!);
 });

it('does not associate uploaded media when canvas saving fails', async () => {
  vi.clearAllMocks();
  useCanvasStore.setState({nodes:[{id:'uploaded',type:'imageNode',position:{x:0,y:0},data:{imageUrl:'/static/projects/project/picture.png'}}] as never});
  vi.mocked(getReferenceDirectorState).mockResolvedValue({...empty(),material_work:[{work_ref:{artifact_id:'work',revision:3},plan_ref:{artifact_id:'plan',revision:1},requirements:[{id:'product',description:'产品图',final_step_id:'image'}],steps:[{id:'image',label:'产品图',media_kind:'image',source:'upload',status:'awaiting_upload',depends_on:[]}]}]});
  vi.mocked(flushFreezoneCanvasRuntime).mockResolvedValueOnce(false);
  render(<ReferenceDirectorPanel {...props}/>);
  fireEvent.change(await screen.findByLabelText('选择产品图素材'),{target:{value:'uploaded'}});
  fireEvent.click(screen.getByRole('button',{name:'关联产品图'}));
  await screen.findByText('请先完成画布保存，再关联素材。');
  expect(associateReferenceMaterialUpload).not.toHaveBeenCalled();
});

it('shows that awaiting_direction alone has no confirmation proposal', async () => {
  vi.mocked(getReferenceDirectorState).mockResolvedValue({ commissions: [{ commission_ref: { artifact_id: 'commission', revision: 1 }, project_id: 'project', canvas_id: 'canvas', goal: '复刻', phase: 'awaiting_direction' }], decisions: [], material_work: [], material_reviews: [], output_reviews: [], speech_corrections: [] });
  render(<ReferenceDirectorPanel projectId="project" canvasId="canvas" agentId="main" connected />);
  await screen.findByText('方向建议尚未准备完成，请查看续接状态。');
  expect(screen.queryByRole('button', { name: '确认复刻方向' })).toBeNull();
});

const speechSummary = {
  observation_ref: { artifact_id: 'observation', revision: 1 }, target_signature: 'target',
  target_text: '品牌你好', recognized_text: '品排你好',
  recognized_words: [{ id: 'w', text: '品排', start_us: 0, end_us: 500000 }, { id: 'w2', text: '你好', start_us: 500000, end_us: 1000000 }],
  recognized_segments: [], timing_granularity: 'word' as const, transcript_match: 'review_required',
  audio_review: 'unavailable', lip_review: 'unavailable', source_duration_us: 1000000,
};
const speechMaterial = () => ({ review_ref: { artifact_id: 'review-audio', revision: 1 }, work_ref: { artifact_id: 'work', revision: 1 }, step_id: 'voice', resource: { media_path: 'media/voice.wav', media_kind: 'audio' as const, node_id: 'voice-node' }, technical: { status: 'passed' as const, checks: [], findings: [] }, content: { status: 'unavailable' as const, checks: [], findings: [] } });
it('loads readonly dialogue evidence on the actual expanded material card and saves a correction separately from approval', async () => {
  vi.clearAllMocks(); scope.current = true;
  vi.mocked(getReferenceMaterialSpeechStatus).mockResolvedValue({ status: 'ready', work_ref: speechMaterial().work_ref, plan_ref: { artifact_id: 'plan', revision: 1 }, step_id: 'voice', node_id: 'voice-node', summary: speechSummary });
  vi.mocked(saveReferenceMaterialSpeechCorrection).mockRejectedValue(new Error('词校正不匹配'));
  render(<ReferenceDirectorPanel {...props} controlledState={{ ...empty(), material_reviews: [speechMaterial()] }} defaultExpanded={false} />);
  expect(getReferenceMaterialSpeechStatus).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '展开虾导制作进度' }));
  await screen.findByText('品排你好');
  expect(getReferenceMaterialSpeechStatus).toHaveBeenCalledWith('project', 'canvas', { work_ref: speechMaterial().work_ref, step_id: 'voice', node_id: 'voice-node', language: 'auto' }, 'main');
  fireEvent.change(screen.getByRole('textbox', { name: '识别词 1' }), { target: { value: '品牌' } });
  fireEvent.click(screen.getByRole('checkbox', { name: '已试听并核对本次修改' }));
  fireEvent.click(screen.getByRole('button', { name: '核对并保存' }));
  await screen.findByText('词校正不匹配');
  expect((screen.getByRole('textbox', { name: '识别词 1' }) as HTMLInputElement).value).toBe('品牌');
  expect(saveReferenceMaterialSpeechCorrection).toHaveBeenCalledWith('project', 'canvas', expect.objectContaining({ work_ref: speechMaterial().work_ref, step_id: 'voice', node_id: 'voice-node', confirmed: true, target_signature: 'target', observation_ref: speechSummary.observation_ref, corrections: [{ word_id: 'w', text: '品牌', reason: '试听后校正识别文字' }], command_id: expect.any(String) }));
  expect(confirmReferenceMaterialReview).not.toHaveBeenCalled();
});
it('requires independent listening acknowledgement before accepting an audio material', async () => {
  vi.clearAllMocks(); scope.current = true;
  vi.mocked(getReferenceMaterialSpeechStatus).mockResolvedValue({ status: 'pending', message: '对白检查进行中' });
  render(<ReferenceDirectorPanel {...props} controlledState={{ ...empty(), material_reviews: [speechMaterial()] }} />);
  const accept = await screen.findByRole('button', { name: '我已审看，确认素材内容' });
  expect(accept.hasAttribute('disabled')).toBe(true);
  fireEvent.click(screen.getByRole('checkbox', { name: '已完整试听当前素材' }));
  expect(accept.hasAttribute('disabled')).toBe(false);
  expect(confirmReferenceMaterialReview).not.toHaveBeenCalled();
});

it('submits only explicitly checked exact material reviews and retains choices on batch failure', async () => {
  vi.clearAllMocks(); scope.current = true;
  const first = { ...speechMaterial(), resource: { ...speechMaterial().resource, media_kind: 'image' as const } };
  const second = { ...first, review_ref: { artifact_id: 'review-other', revision: 4 }, step_id: 'other', resource: { ...first.resource, node_id: 'other-node' } };
  vi.mocked(confirmReferenceMaterialReviewBatch).mockRejectedValue(new Error('候选素材已变化'));
  render(<ReferenceDirectorPanel {...props} controlledState={{ ...empty(), material_reviews: [first, second] }} />);
  const card = await screen.findByRole('article', { name: '素材确认' });
  expect((within(card).getByRole('checkbox', { name: '已审看此素材，加入本次确认' }) as HTMLInputElement).checked).toBe(false);
  const submit = within(card).getByRole('button', { name: '确认已勾选的素材' });
  expect(submit.hasAttribute('disabled')).toBe(true);
  fireEvent.click(within(card).getByRole('button', { name: '下一条' }));
  const secondCheck = within(card).getByRole('checkbox', { name: '已审看此素材，加入本次确认' });
  fireEvent.click(secondCheck);
  fireEvent.click(submit);
  await screen.findByText('候选素材已变化');
  expect(confirmReferenceMaterialReviewBatch).toHaveBeenCalledWith('project', 'canvas', {
    items: [{ review_ref: second.review_ref, work_ref: second.work_ref, step_id: 'other', node_id: 'other-node' }], command_id: expect.any(String),
  });
  expect((within(card).getByRole('checkbox', { name: '已审看此素材，加入本次确认' }) as HTMLInputElement).checked).toBe(true);
  expect(confirmReferenceMaterialReview).not.toHaveBeenCalled();
});

it('re-reads saved speech evidence on a scoped completion even when progress counts and digest are unchanged', async () => {
  vi.clearAllMocks(); scope.current = true;
  vi.mocked(getReferenceMaterialSpeechStatus).mockResolvedValueOnce({ status: 'pending', message: '识别进行中' }).mockResolvedValue({ status: 'ready', work_ref: speechMaterial().work_ref, plan_ref: { artifact_id: 'plan', revision: 1 }, step_id: 'voice', node_id: 'voice-node', summary: speechSummary });
  const current = { ...empty(), material_reviews: [speechMaterial()] };
  const progress = { commission_ref: { artifact_id: 'commission', revision: 1 }, projection_revision: 'same', progress: { user_action_count: 1 } } as never;
  const view = render(<ReferenceDirectorPanel {...props} controlledState={current} progress={progress} />);
  await screen.findByText('识别进行中');
  view.rerender(<ReferenceDirectorPanel {...props} controlledState={{ ...current }} progress={{ ...progress as object } as never} />);
  await screen.findByText('品排你好');
  expect(getReferenceMaterialSpeechStatus).toHaveBeenCalledTimes(2);
});

it('opens the exact current production plan from the progress card', async () => {
  vi.clearAllMocks(); scope.current = true; seedPlan();
  render(<ReferenceDirectorPanel {...props} controlledState={{ ...empty(), commissions: [{ ...state().commissions[0], plan_ref: { artifact_id: 'plan', revision: 1 }, phase: 'preparing_materials' }] }} />);
  fireEvent.click(await screen.findByRole('button', { name: '查看作品方案' }));
  expect(useCanvasStore.getState().selectedNodeId).toBe('plan-node');
});

it('limits explicit batch selection to the supported twenty exact reviews', async () => {
  vi.clearAllMocks(); scope.current = true;
  const reviews = Array.from({ length: 21 }, (_, i) => ({ ...speechMaterial(), review_ref: { artifact_id: `review-${i}`, revision: 1 }, resource: { ...speechMaterial().resource, media_kind: 'image' as const } }));
  render(<ReferenceDirectorPanel {...props} controlledState={{ ...empty(), material_reviews: reviews }} />);
  const card = await screen.findByRole('article', { name: '素材确认' });
  for (let index = 1; index <= 20; index += 1) {
    expect(pagerStatus(card)).toBe(`${index}/21`);
    const check = within(card).getByRole('checkbox', { name: '已审看此素材，加入本次确认' });
    expect(check.hasAttribute('disabled')).toBe(false);
    fireEvent.click(check);
    fireEvent.click(within(card).getByRole('button', { name: '下一条' }));
  }
  expect(pagerStatus(card)).toBe('21/21');
  expect(within(card).getByRole('checkbox', { name: '已审看此素材，加入本次确认' }).hasAttribute('disabled')).toBe(true);
});

/** The pager prints a bare "n/m" now, scoped to its own group so it never collides with the card's confirmed chip. */
const pagerStatus = (card: HTMLElement) => within(within(card).getByRole('group', { name: '素材翻页' })).getByRole('status').textContent;

const stageMaterials = (): ReferenceDirectorState => ({ ...empty(),
  material_stages: [{ stage_id: 'work:stage:1', stage_number: 1, step_ids: ['first', 'second'], status: 'awaiting_confirmation', total: 2, confirmed: 0, reviewed: 0 }],
  material_reviews: ['first', 'second'].map((id) => ({ ...speechMaterial(), review_ref: { artifact_id: `review-${id}`, revision: 1 }, step_id: id,
    resource: { media_path: `media/${id}.png`, media_kind: 'image', node_id: id } })),
});

it('shows only compact progress until the whole material stage is technically ready', async () => {
  vi.clearAllMocks();
  const current = stageMaterials();
  current.material_stages![0].status = 'preparing';
  render(<ReferenceDirectorPanel {...props} conversationMode controlledState={current} />);
  const card = await screen.findByRole('article', { name: '第 1 阶段素材确认' });
  expect(within(card).getByText('本阶段素材正在准备，全部通过技术检查后统一审看。')).toBeTruthy();
  expect(within(card).queryByRole('img')).toBeNull();
  expect(within(card).queryByRole('checkbox')).toBeNull();
  expect(confirmReferenceMaterialReview).not.toHaveBeenCalled();
});

it('keeps one grouped card through partial approval and collapses the fully confirmed summary', async () => {
  vi.clearAllMocks();
  const current = stageMaterials();
  const refresh = vi.fn(async () => {});
  vi.mocked(confirmReferenceMaterialReview).mockResolvedValue({});
  const view = render(<ReferenceDirectorPanel {...props} conversationMode controlledState={current} onRefresh={refresh} />);
  let card = await screen.findByRole('article', { name: '第 1 阶段素材确认' });
  // One stage shows one candidate at a time; the rest are reachable by paging.
  expect(within(card).getAllByRole('img')).toHaveLength(1);
  expect(pagerStatus(card)).toBe('1/2');
  fireEvent.click(within(card).getAllByRole('button', { name: '我已审看，确认素材内容' })[0]);
  await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
  // A successful mutation must not locally remove a review before the read model arrives.
  expect(within(card).getAllByRole('img')).toHaveLength(1);
  const partial = stageMaterials();
  partial.material_stages![0].confirmed = 1;
  partial.material_stages![0].reviewed = 1;
  partial.material_reviews![0].content.status = 'passed';
  view.rerender(<ReferenceDirectorPanel {...props} conversationMode controlledState={partial} onRefresh={refresh} />);
  expect(await screen.findByText('本阶段素材已就绪，请统一审看 · 已确认 1/2')).toBeTruthy();
  card = screen.getByRole('article', { name: '第 1 阶段素材确认' });
  // The confirmed candidate drops out of the pager, leaving the pending one.
  expect(within(card).getAllByRole('img')).toHaveLength(1);
  expect(pagerStatus(card)).toBe('2/2');
  expect(within(card).getAllByRole('button', { name: '我已审看，确认素材内容' })).toHaveLength(1);
  const complete = stageMaterials();
  complete.material_stages![0] = { ...complete.material_stages![0], status: 'confirmed', confirmed: 2, reviewed: 2 };
  complete.material_reviews!.forEach((review) => { review.content.status = 'passed'; });
  view.rerender(<ReferenceDirectorPanel {...props} conversationMode controlledState={complete} onRefresh={refresh} />);
  expect(await screen.findByText('本阶段素材已全部确认（2/2）')).toBeTruthy();
  card = screen.getByRole('article', { name: '第 1 阶段素材确认' });
  expect(within(card).queryByRole('img')).toBeNull();
  const expand = within(card).getByRole('button', { name: '展开第 1 阶段素材确认' });
  expect(expand.getAttribute('aria-expanded')).toBe('false');
  fireEvent.click(expand);
  expect(within(card).getAllByRole('img')).toHaveLength(1);
  expect(pagerStatus(card)).toBe('1/2');
  expect(within(card).queryByRole('checkbox')).toBeNull();
  // The summary is durable server state, not an in-memory approval receipt.
  view.unmount();
  render(<ReferenceDirectorPanel {...props} conversationMode controlledState={complete} />);
  expect(await screen.findByText('本阶段素材已全部确认（2/2）')).toBeTruthy();
  expect(screen.queryByRole('img')).toBeNull();
});

it('pages through one stage candidate at a time instead of stacking them', async () => {
  vi.clearAllMocks();
  render(<ReferenceDirectorPanel {...props} conversationMode controlledState={stageMaterials()} />);
  const card = await screen.findByRole('article', { name: '第 1 阶段素材确认' });
  expect(within(card).getAllByRole('img')).toHaveLength(1);
  expect(pagerStatus(card)).toBe('1/2');
  expect(within(card).getByRole('button', { name: '上一条' }).hasAttribute('disabled')).toBe(true);
  fireEvent.click(within(card).getByRole('button', { name: '下一条' }));
  expect(pagerStatus(card)).toBe('2/2');
  expect(within(card).getByRole('button', { name: '下一条' }).hasAttribute('disabled')).toBe(true);
  expect(within(card).getAllByRole('img')).toHaveLength(1);
  fireEvent.click(within(card).getByRole('button', { name: '上一条' }));
  expect(pagerStatus(card)).toBe('1/2');
});

it('scopes batch confirmation to the selected exact reviews in one stage', async () => {
  vi.clearAllMocks();
  const current = stageMaterials();
  current.material_stages = [
    { ...current.material_stages![0], step_ids: ['first'], total: 1 },
    { ...current.material_stages![0], stage_id: 'work:stage:2', stage_number: 2, step_ids: ['second'], total: 1 },
  ];
  vi.mocked(confirmReferenceMaterialReviewBatch).mockResolvedValue({ items: [], replayed: false });
  render(<ReferenceDirectorPanel {...props} conversationMode controlledState={current} />);
  const first = await screen.findByRole('article', { name: '第 1 阶段素材确认' });
  const second = screen.getByRole('article', { name: '第 2 阶段素材确认' });
  fireEvent.click(within(first).getByRole('checkbox', { name: '已审看此素材，加入本次确认' }));
  fireEvent.click(within(second).getByRole('checkbox', { name: '已审看此素材，加入本次确认' }));
  fireEvent.click(within(first).getByRole('button', { name: '确认已勾选的素材' }));
  await waitFor(() => expect(confirmReferenceMaterialReviewBatch).toHaveBeenCalledTimes(1));
  expect(confirmReferenceMaterialReviewBatch).toHaveBeenCalledWith('project', 'canvas', {
    items: [{ review_ref: current.material_reviews![0].review_ref, work_ref: current.material_reviews![0].work_ref, step_id: 'first', node_id: 'first' }], command_id: expect.any(String),
  });
});

it('retains a confirmed stage summary even when no detailed reviews are supplied', async () => {
  render(<ReferenceDirectorPanel {...props} conversationMode controlledState={{ ...empty(), material_stages: [
    { stage_id: 'work:stage:1', stage_number: 1, step_ids: ['first'], status: 'confirmed', total: 1, confirmed: 1, reviewed: 1 },
  ] }} />);
  expect(await screen.findByText('本阶段素材已全部确认（1/1）')).toBeTruthy();
});

it('clears listening and batch selection when the current work or candidate resource changes', async () => {
  vi.clearAllMocks(); scope.current = true;
  vi.mocked(getReferenceMaterialSpeechStatus).mockResolvedValue({ status: 'pending', message: '识别进行中' });
  const old = { ...speechMaterial(), current_work_ref: { artifact_id: 'work', revision: 2 }, resource: { ...speechMaterial().resource, media_hash: 'old-hash' } };
  const sibling = { ...old, review_ref: { artifact_id: 'review-sibling', revision: 1 }, step_id: 'sibling', resource: { ...old.resource, node_id: 'sibling-node' } };
  const view = render(<ReferenceDirectorPanel {...props} controlledState={{ ...empty(), material_reviews: [old, sibling] }} />);
  fireEvent.click(await screen.findByRole('checkbox', { name: '已完整试听当前素材' }));
  fireEvent.click(screen.getByRole('checkbox', { name: '已审看此素材，加入本次确认' }));
  expect(screen.getByRole('button', { name: '确认已勾选的素材' }).hasAttribute('disabled')).toBe(false);
  const newer = { ...old, current_work_ref: { artifact_id: 'work', revision: 3 }, resource: { ...old.resource, media_hash: 'new-hash' } };
  const newerSibling = { ...newer, review_ref: sibling.review_ref, step_id: 'sibling', resource: { ...newer.resource, node_id: 'sibling-node' } };
  view.rerender(<ReferenceDirectorPanel {...props} controlledState={{ ...empty(), material_reviews: [newer, newerSibling] }} />);
  await waitFor(() => expect((screen.getByRole('checkbox', { name: '已完整试听当前素材' }) as HTMLInputElement).checked).toBe(false));
  expect((screen.getByRole('checkbox', { name: '已审看此素材，加入本次确认' }) as HTMLInputElement).checked).toBe(false);
  expect(screen.getByRole('button', { name: '确认已勾选的素材' }).hasAttribute('disabled')).toBe(true);
});
