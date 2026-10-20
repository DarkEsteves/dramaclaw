import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { getReferenceProductionState, getReferenceProductionHistory } from '@/api/referenceDirector';
import { useAuthStore } from '@/stores/auth-store';
import { refreshProductionCanvas } from './productionCanvasRefresh';
import { ReferenceProductionPanel } from './ReferenceProductionPanel';
import { productionCardRefs } from './referenceConversationCards';

vi.mock('@/api/referenceDirector', async (original) => ({
  ...await original<typeof import('@/api/referenceDirector')>(),
  getReferenceProductionState: vi.fn(), getReferenceProductionHistory: vi.fn(),
}));
vi.mock('./productionCanvasRefresh', () => ({ refreshProductionCanvas: vi.fn(async () => {}) }));
const props = { projectId: 'p', canvasId: 'c', agentId: 'main', connected: true };
const branch = (id = 'commission') => ({
  schema_version: 'reference_production_state.v1' as const,
  scope: { project_id: 'p', canvas_id: 'c', agent_id: 'main' },
  commission_ref: { artifact_id: id, revision: 1 },
  plan_ref: { artifact_id: 'plan', revision: 1 },
  work_ref: { artifact_id: 'work', revision: 6 },
  projection_revision: 'facts',
  analysis_node_id: 'source',
  phase: 'preparing_materials' as const,
  execution_status: 'waiting_user' as const,
  progress: { materials_total: 9, materials_available: 9, materials_accepted: 5,
    final_materials_total: 7, final_materials_accepted: 3, user_action_count: 4 },
  blockers: [],
  next_action: { owner: 'user' as const, kind: 'review_materials', action_id: 'voices' },
  director_state: { commissions: [{ commission_ref: { artifact_id: id, revision: 1 },
    project_id: 'p', canvas_id: 'c', phase: 'preparing_materials' as const, goal: '黑咖啡复刻' }],
    decisions: [], material_work: [], speech_corrections: [], material_reviews: [] },
});

describe('ReferenceProductionPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    useAuthStore.setState({ username: 'alice' });
    vi.mocked(getReferenceProductionState).mockResolvedValue({ branches: [branch()], selection_required: false });
  });
  it('shows authoritative current counts in the compact panel', async () => {
    render(<ReferenceProductionPanel {...props} />);
    await screen.findByText(/素材就绪 9\/9/);
    expect(screen.getByText(/已采用 5\/9/)).toBeTruthy();
    expect(screen.getByText(/4 项待审看/)).toBeTruthy();
    expect(screen.getByRole('button', { name: '展开虾导制作进度' })).toBeTruthy();
  });
  it('keeps only pending actions in the conversation surface', async () => {
    const current = branch();
    current.director_state.commissions[0].phase = 'awaiting_direction' as never;
    current.director_state.decisions = [{
      decision_ref: { artifact_id: 'direction', revision: 1 },
      commission_ref: current.commission_ref,
      kind: 'direction', status: 'pending', summary: '保留双人问答，换掉产品', proposal: { keep: '双人问答' },
    }] as never;
    vi.mocked(getReferenceProductionState).mockResolvedValue({ branches: [current], selection_required: false });
    const summary = document.createElement('div'); document.body.append(summary);
    render(<ReferenceProductionPanel {...props} summaryTarget={summary} conversationMode />);
    await screen.findByRole('article', { name: '作品制作操作' });
    expect(screen.queryByRole('button', { name: '确认复刻方向' })).toBeNull();
    expect(screen.queryByText('等待确认复刻方向')).toBeNull();
    expect(screen.queryByRole('button', { name: '查看历史记录' })).toBeNull();
    expect(summary.textContent).toBe('');
    summary.remove();
  });
  it('does not expose a second material work choice or replay its historical card', async () => {
    const current = { ...branch(), work_ref: null,
      candidate_work_refs: [{ artifact_id: 'work-one', revision: 1 }] };
    vi.mocked(getReferenceProductionState).mockResolvedValue({ branches: [current], selection_required: false });
    render(<ReferenceProductionPanel {...props} conversationMode />);
    await screen.findByRole('article', { name: '作品制作操作' });
    expect(screen.queryByText(/采用素材清单/)).toBeNull();
    expect(productionCardRefs([{ uiEvents: [{
      type: 'reference.production.card', project_id: 'p', canvas_id: 'c', agent_id: 'main',
      commission_ref: { artifact_id: 'commission', revision: 1 },
      ref: { artifact_id: 'work-one', revision: 1 }, schema: 'material_work_package.v1',
    }] }] as never, 'p', 'c', 'main', 'commission')).toEqual([]);
  });
  it('persists collapse preference within the same account and production', async () => {
    const view = render(<ReferenceProductionPanel {...props} />);
    await screen.findByRole('button', { name: '展开虾导制作进度' });
    fireEvent.click(screen.getByRole('button', { name: '展开虾导制作进度' }));
    expect(screen.getByRole('button', { name: '收起虾导制作进度' })).toBeTruthy();
    view.unmount();
    render(<ReferenceProductionPanel {...props} />);
    await screen.findByRole('button', { name: '收起虾导制作进度' });
  });
  it('does not merge two independent plan branches', async () => {
    vi.mocked(getReferenceProductionState).mockResolvedValue({ branches: [branch('a'), branch('b')],
                                                             selection_required: true });
    render(<ReferenceProductionPanel {...props} />);
    await screen.findByRole('combobox', { name: '选择作品方案进度' });
    expect(screen.queryByText(/素材就绪 9\/9/)).toBeNull();
    fireEvent.change(screen.getByRole('combobox', { name: '选择作品方案进度' }), { target: { value: 'b' } });
    await screen.findByText(/素材就绪 9\/9/);
  });
  it('refreshes on scoped events without polling or old inventory reads', async () => {
    render(<ReferenceProductionPanel {...props} />);
    await screen.findByText(/素材就绪 9\/9/);
    expect(getReferenceProductionState).toHaveBeenCalledTimes(1);
    act(() => window.dispatchEvent(new CustomEvent('reference-director-status',
      { detail: { project_id: 'p', canvas_id: 'other' } })));
    expect(getReferenceProductionState).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: '刷新制作状态' }));
    await waitFor(() => expect(getReferenceProductionState).toHaveBeenCalledTimes(2));
    expect(screen.getByRole('button', { name: '展开虾导制作进度' })).toBeTruthy();
  });
  it('does not let a late response from the old canvas overwrite current state', async () => {
    let release!: (value: { branches: ReturnType<typeof branch>[]; selection_required: boolean }) => void;
    vi.mocked(getReferenceProductionState).mockReturnValueOnce(new Promise((resolve) => { release = resolve; }));
    const view = render(<ReferenceProductionPanel {...props} />);
    vi.mocked(getReferenceProductionState).mockResolvedValue({ branches: [], selection_required: false });
    view.rerender(<ReferenceProductionPanel {...props} canvasId="other" />);
    await act(async () => release({ branches: [branch()], selection_required: false }));
    expect(screen.queryByText(/素材就绪 9\/9/)).toBeNull();
  });
});

it('loads history only on demand and ignores an old branch history error', async () => {
  vi.clearAllMocks(); useAuthStore.setState({ username: 'alice' });
  let fail!: (error: Error) => void;
  vi.mocked(getReferenceProductionHistory).mockReturnValueOnce(new Promise((_, reject) => { fail = reject; }));
  vi.mocked(getReferenceProductionState).mockResolvedValue({ branches: [branch('a'), branch('b')], selection_required: true });
  render(<ReferenceProductionPanel {...props} />);
  const select = await screen.findByRole('combobox', { name: '选择作品方案进度' });
  expect(getReferenceProductionHistory).not.toHaveBeenCalled();
  fireEvent.change(select, { target: { value: 'a' } });
  fireEvent.click(await screen.findByRole('button', { name: '查看历史记录' }));
  fireEvent.change(select, { target: { value: 'b' } });
  await act(async () => fail(new Error('旧方案读取失败')));
  expect(screen.queryByText('旧方案读取失败')).toBeNull();
});
it('does not restore old account history after leaving and returning to the same scope', async () => {
  vi.clearAllMocks(); useAuthStore.setState({ username: 'alice' });
  let release!: (value: Awaited<ReturnType<typeof getReferenceProductionHistory>>) => void;
  vi.mocked(getReferenceProductionState).mockResolvedValue({ branches: [branch()], selection_required: false });
  vi.mocked(getReferenceProductionHistory).mockReturnValueOnce(new Promise((resolve) => { release = resolve; }));
  render(<ReferenceProductionPanel {...props} />);
  fireEvent.click(await screen.findByRole('button', { name: '查看历史记录' }));
  act(() => useAuthStore.setState({ username: 'bob' }));
  await waitFor(() => expect(getReferenceProductionState).toHaveBeenCalledTimes(2));
  act(() => useAuthStore.setState({ username: 'alice' }));
  await waitFor(() => expect(getReferenceProductionState).toHaveBeenCalledTimes(3));
  await screen.findByRole('button', { name: '查看历史记录' });
  await act(async () => release({ items: [{ schema: 'material_work_package.v1', revision: 77 }], next_cursor: null }));
  expect(screen.queryByText(/版本 77/)).toBeNull();
});

it('shows a genuine stopped disposition when expanded and makes no automatic resume request', async () => {
  vi.clearAllMocks(); useAuthStore.setState({ username: 'alice' }); localStorage.clear();
  vi.mocked(getReferenceProductionState).mockResolvedValue({ ...branch(), execution_status: 'stopped' });
  render(<ReferenceProductionPanel {...props} />);
  await screen.findByRole('button', { name: '展开虾导制作进度' });
  expect(screen.getByRole('status').textContent).toContain('制作已停止');
  fireEvent.click(screen.getByRole('button', { name: '展开虾导制作进度' }));
  expect(screen.getByRole('status').textContent).toContain('制作已停止');
  expect(getReferenceProductionState).toHaveBeenCalledTimes(1);
});
it('waits for canvas_changed merge before reading progress and ignores a late old scope merge', async () => {
  localStorage.clear(); vi.clearAllMocks(); useAuthStore.setState({ username: 'alice' });
  let release!: () => void;
  vi.mocked(getReferenceProductionState).mockResolvedValue({ branches: [branch()], selection_required: false });
  vi.mocked(refreshProductionCanvas).mockReturnValueOnce(new Promise((resolve) => { release = resolve; }));
  const view = render(<ReferenceProductionPanel {...props} />);
  await screen.findByText(/素材就绪 9\/9/);
  act(() => window.dispatchEvent(new CustomEvent('reference-director-status', { detail: { project_id: 'p', canvas_id: 'c', canvas_changed: true } })));
  await waitFor(() => expect(refreshProductionCanvas).toHaveBeenCalledTimes(1));
  expect(getReferenceProductionState).toHaveBeenCalledTimes(1);
  view.rerender(<ReferenceProductionPanel {...props} canvasId="other" />);
  vi.mocked(getReferenceProductionState).mockResolvedValue({ branches: [], selection_required: false });
  await act(async () => release());
  expect(getReferenceProductionState).toHaveBeenCalledTimes(2);
});
