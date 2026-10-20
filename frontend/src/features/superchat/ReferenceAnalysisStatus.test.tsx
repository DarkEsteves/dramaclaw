import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReferenceAnalysisRun } from '@/api/referenceAnalysisRuns';
import { ReferenceAnalysisStatus } from './ReferenceAnalysisStatus';
import { REFERENCE_ANALYSIS_STATUS_EVENT } from '@/features/freezone/referenceAnalysisEvents';

const fake = vi.hoisted(() => ({ nodes: [] as unknown[], edges: [] as unknown[], baseline: { nodes: [] as unknown[], edges: [] as unknown[], revision: 1 }, select: vi.fn(), focus: vi.fn() }));
vi.mock('@/api/referenceAnalysisRuns', () => ({ listReferenceAnalysisRuns: vi.fn(async () => []), retryReferenceAnalysisRun: vi.fn() }));
vi.mock('@/api/canvas', () => ({ getFreezoneCanvas: vi.fn(async () => ({ nodes: [], edges: [], revision: 2 })) }));
vi.mock('@/features/freezone/canvasSyncRuntime', () => ({ applyRemoteFreezoneCanvas: vi.fn(() => true), captureFreezoneCanvasScope: () => () => true, getFreezoneCanvasBaseline: () => fake.baseline }));
vi.mock('@/stores/canvasStore', async (importOriginal) => ({ ...await importOriginal<typeof import('@/stores/canvasStore')>(), useCanvasStore: { getState: () => ({ nodes: fake.nodes, edges: fake.edges, setSelectedNode: fake.select, requestFocusNode: fake.focus, onNodesChange: vi.fn() }) } }));

import { listReferenceAnalysisRuns, retryReferenceAnalysisRun } from '@/api/referenceAnalysisRuns';
import { getFreezoneCanvas } from '@/api/canvas';
import { applyRemoteFreezoneCanvas } from '@/features/freezone/canvasSyncRuntime';

const run = (overrides: Partial<ReferenceAnalysisRun> = {}): ReferenceAnalysisRun => ({ run_id: 'run-1', project_id: 'project', canvas_id: 'canvas', agent_id: 'main', skill_id: 'reference-video-remix', source_node_id: 'video', analysis_node_id: 'story', source_video_url: '/source.mp4', status: 'analyzing', created_at: '2026-10-10T06:00:00Z', updated_at: '2026-10-10T06:00:00Z', ...overrides });
const event = (value: ReferenceAnalysisRun) => act(() => window.dispatchEvent(new CustomEvent(REFERENCE_ANALYSIS_STATUS_EVENT, { detail: { run: value } })));

describe('ReferenceAnalysisStatus', () => {
  beforeEach(() => { vi.clearAllMocks(); vi.mocked(listReferenceAnalysisRuns).mockResolvedValue([]); fake.nodes = []; fake.edges = []; fake.baseline = { nodes: [], edges: [], revision: 1 }; });
  it('hides empty progress and shows a newly submitted analysis through its event', async () => {
    render(<ReferenceAnalysisStatus projectId="project" canvasId="canvas" agentId="main" connected />);
    await waitFor(() => expect(listReferenceAnalysisRuns).toHaveBeenCalledTimes(1));
    expect(screen.queryByLabelText('参考视频复刻状态')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
    event(run());
    await screen.findByText('原片分析中');
    expect(listReferenceAnalysisRuns).toHaveBeenCalledTimes(1);
  });
  it('keeps routine analysis in the conversation without a separate status card', async () => {
    const summary = document.createElement('div'); document.body.append(summary);
    vi.mocked(listReferenceAnalysisRuns).mockResolvedValue([run()]);
    render(<ReferenceAnalysisStatus projectId="project" canvasId="canvas" agentId="main" connected summaryTarget={summary} conversationMode />);
    await waitFor(() => expect(getFreezoneCanvas).toHaveBeenCalledTimes(1));
    expect(screen.queryByLabelText('参考视频复刻状态')).toBeNull();
    expect(summary.textContent).toBe('');
    summary.remove();
  });
  it('keeps an interrupted analysis recovery action in conversation mode', async () => {
    vi.mocked(listReferenceAnalysisRuns).mockResolvedValue([run({ status: 'resume_failed', error: '模型连接中断' })]);
    render(<ReferenceAnalysisStatus projectId="project" canvasId="canvas" agentId="main" connected conversationMode />);
    expect(await screen.findByRole('button', { name: '继续制作流程' })).toBeTruthy();
    expect(screen.getByText('模型连接中断')).toBeTruthy();
  });
  it('restores a background analysis and completes through one scoped event without status polling', async () => {
    vi.mocked(listReferenceAnalysisRuns).mockResolvedValue([run()]);
    render(<ReferenceAnalysisStatus projectId="project" canvasId="canvas" agentId="main" connected />);
    await screen.findByText('原片分析中');
    await waitFor(() => expect(getFreezoneCanvas).toHaveBeenCalledTimes(1));
    event(run({ status: 'analysis_ready', updated_at: '2026-10-10T06:02:00Z' }));
    await screen.findByText('原片分析已完成，等待确认复刻方向');
    await waitFor(() => expect(getFreezoneCanvas).toHaveBeenCalledTimes(2));
    event(run({ status: 'analysis_ready', updated_at: '2026-10-10T06:02:00Z' }));
    await waitFor(() => expect(listReferenceAnalysisRuns).toHaveBeenCalledTimes(1));
    expect(getFreezoneCanvas).toHaveBeenCalledTimes(2);
    expect(applyRemoteFreezoneCanvas).toHaveBeenCalledTimes(2);
  });
  it('ignores events from other canvas scopes and agents', async () => {
    render(<ReferenceAnalysisStatus projectId="project" canvasId="canvas" agentId="main" connected />);
    await waitFor(() => expect(listReferenceAnalysisRuns).toHaveBeenCalled());
    event(run({ canvas_id: 'other' }));
    event(run({ agent_id: 'other-agent' }));
    expect(screen.queryByText('原片分析中')).toBeNull();
    expect(getFreezoneCanvas).not.toHaveBeenCalled();
  });
  it('allows retrying only the agent continuation and keeps the original analysis', async () => {
    vi.mocked(listReferenceAnalysisRuns).mockResolvedValue([run({ status: 'resume_failed', error: '模型连接中断' })]);
    vi.mocked(retryReferenceAnalysisRun).mockResolvedValue(run({ status: 'resuming' }));
    render(<ReferenceAnalysisStatus projectId="project" canvasId="canvas" agentId="main" connected />);
    await screen.findByText('模型连接中断');
    fireEvent.click(screen.getByRole('button', { name: '继续制作流程' }));
    await waitFor(() => expect(retryReferenceAnalysisRun).toHaveBeenCalledWith('project', 'canvas', 'run-1'));
    await screen.findByText('虾导正在整理复刻建议');
    expect(listReferenceAnalysisRuns).toHaveBeenCalledTimes(1);
  });
  it('checks a stale run through the same retry endpoint only after the user clicks', async () => {
    vi.mocked(listReferenceAnalysisRuns).mockResolvedValue([run({ status: 'stale', error: '需核对原片证据' })]);
    vi.mocked(retryReferenceAnalysisRun).mockResolvedValue(run({ status: 'analysis_ready', updated_at: '2026-10-10T06:02:00Z' }));
    render(<ReferenceAnalysisStatus projectId="project" canvasId="canvas" agentId="main" connected />);
    const retry = await screen.findByRole('button', { name: '核对并继续' });
    expect(retryReferenceAnalysisRun).not.toHaveBeenCalled();
    fireEvent.click(retry);
    await waitFor(() => expect(retryReferenceAnalysisRun).toHaveBeenCalledWith('project', 'canvas', 'run-1'));
    await screen.findByText('原片分析已完成，等待确认复刻方向');
    expect(listReferenceAnalysisRuns).toHaveBeenCalledTimes(1);
  });
  it('keeps a stale run stopped when server evidence validation rejects the retry', async () => {
    vi.mocked(listReferenceAnalysisRuns).mockResolvedValue([run({ status: 'stale' })]);
    vi.mocked(retryReferenceAnalysisRun).mockRejectedValue(new Error('原片证据不匹配，无法继续'));
    render(<ReferenceAnalysisStatus projectId="project" canvasId="canvas" agentId="main" connected />);
    fireEvent.click(await screen.findByRole('button', { name: '核对并继续' }));
    await screen.findByText('原片证据不匹配，无法继续');
    expect(screen.queryByText('虾导正在整理复刻建议')).toBeNull();
    await waitFor(() => expect(screen.getByRole('button', { name: '核对并继续' })).not.toBeDisabled());
  });
  it('keeps late completion from a previous canvas away from the active canvas', async () => {
    let resolveCanvas!: (value: { nodes: []; edges: []; revision: number }) => void;
    vi.mocked(listReferenceAnalysisRuns).mockResolvedValue([run()]);
    vi.mocked(getFreezoneCanvas).mockImplementationOnce(() => new Promise((resolve) => { resolveCanvas = resolve; }));
    const view = render(<ReferenceAnalysisStatus projectId="project" canvasId="canvas" agentId="main" connected />);
    await waitFor(() => expect(getFreezoneCanvas).toHaveBeenCalledTimes(1));
    vi.mocked(listReferenceAnalysisRuns).mockResolvedValue([]);
    view.rerender(<ReferenceAnalysisStatus projectId="project" canvasId="other" agentId="main" connected />);
    resolveCanvas({ nodes: [], edges: [], revision: 2 });
    await waitFor(() => expect(listReferenceAnalysisRuns).toHaveBeenCalledTimes(2));
    expect(applyRemoteFreezoneCanvas).not.toHaveBeenCalled();
  });
  it('does not apply or autosave a canvas when local and server result edits conflict', async () => {
    const base = { id: 'story', type: 'videoStoryNode', position: { x: 0, y: 0 }, data: { rows: [] } };
    fake.baseline.nodes = [base];
    fake.nodes = [{ ...base, data: { rows: [{ narrative: 'manual' }] } }];
    vi.mocked(getFreezoneCanvas).mockResolvedValueOnce({ nodes: [{ ...base, data: { rows: [{ narrative: 'generated' }] } }], edges: [], revision: 2 });
    vi.mocked(listReferenceAnalysisRuns).mockResolvedValue([run({ status: 'analysis_ready' })]);
    render(<ReferenceAnalysisStatus projectId="project" canvasId="canvas" agentId="main" connected />);
    await screen.findByText(/同时修改/);
    expect(applyRemoteFreezoneCanvas).not.toHaveBeenCalled();
    expect(fake.nodes).toEqual([{ ...base, data: { rows: [{ narrative: 'manual' }] } }]);
  });
  it('reloads persisted runs once after reconnecting without starting a polling loop', async () => {
    vi.mocked(listReferenceAnalysisRuns).mockResolvedValue([run()]);
    const view = render(<ReferenceAnalysisStatus projectId="project" canvasId="canvas" agentId="main" connected />);
    await screen.findByText('原片分析中');
    view.rerender(<ReferenceAnalysisStatus projectId="project" canvasId="canvas" agentId="main" connected={false} />);
    vi.mocked(listReferenceAnalysisRuns).mockResolvedValue([run({ status: 'completed' })]);
    view.rerender(<ReferenceAnalysisStatus projectId="project" canvasId="canvas" agentId="main" connected />);
    await screen.findByText('原片分析已完成，请查看虾导回复');
    expect(listReferenceAnalysisRuns).toHaveBeenCalledTimes(2);
  });
});


describe('reference director phase status', () => {
  beforeEach(() => { vi.clearAllMocks(); fake.nodes = []; fake.edges = []; fake.baseline = { nodes: [], edges: [], revision: 1 }; });
  it.each([
    ['awaiting_direction', '等待确认复刻方向'],
    ['awaiting_plan_review', '虾导正在审核作品方案'],
    ['awaiting_plan_confirmation', '等待确认作品方案'],
    ['preparing_materials', '正在准备制作素材'],
    ['aligning_speech', '正在校对实际语音'],
    ['composing', '正在合成作品'],
    ['reviewing_output', '正在检查成片'],
  ] as const)('shows %s as its real phase without a failure alert', async (status, label) => {
    vi.mocked(listReferenceAnalysisRuns).mockResolvedValue([run({ status })]);
    render(<ReferenceAnalysisStatus projectId="project" canvasId="canvas" agentId="main" connected />);
    await screen.findByText(label);
    expect(screen.queryByRole('alert')).toBeNull();
  });
});


it('uses the persisted director phase while a continuation is resuming', async () => {
  vi.mocked(listReferenceAnalysisRuns).mockResolvedValue([run({ status: 'resuming', phase: 'awaiting_plan_confirmation', commission_ref: { artifact_id: 'commission', revision: 4 } })]);
  render(<ReferenceAnalysisStatus projectId="project" canvasId="canvas" agentId="main" connected />);
  await screen.findByText('等待确认作品方案');
});

it('moves an old continuation interruption out of the active production surface', async () => {
  vi.mocked(listReferenceAnalysisRuns).mockResolvedValue([run({ status: 'resume_failed',
    error: 'old interrupted turn', commission_ref: { artifact_id: 'commission', revision: 1 } })]);
  render(<ReferenceAnalysisStatus projectId="project" canvasId="canvas" agentId="main" connected
    currentProduction={{ scope: { project_id: 'project', canvas_id: 'canvas', agent_id: 'main' },
      commission_ref: { artifact_id: 'commission', revision: 6 }, analysis_node_id: 'story',
      phase: 'preparing_materials', execution_status: 'waiting_user' } as never} />);
  await waitFor(() => expect(listReferenceAnalysisRuns).toHaveBeenCalled());
  expect(screen.queryByText('原片分析已保留，虾导续接中断')).toBeNull();
  expect(screen.queryByRole('button', { name: '继续制作流程' })).toBeNull();
});

it('separates completed analysis from failed context delivery and browser wait', async () => {
  vi.mocked(listReferenceAnalysisRuns).mockResolvedValue([run({ status: 'resume_failed', execution_blocker: { code: 'browser_required', next_action: 'open_canvas', message: '等待打开画布' } })]);
  render(<ReferenceAnalysisStatus projectId="project" canvasId="canvas" agentId="main" connected />);
  await screen.findByText('原片分析已完成');
  await screen.findByText('等待打开画布');
  expect(screen.getByRole('button', { name: '继续处理' })).toBeTruthy();
});
