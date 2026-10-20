import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ProductionPlanV2 } from '@/features/canvas/remix/productionPlanV2Model';
import { bindPlanSpeechSource } from '@/features/canvas/remix/productionPlanV2Edits';

const api = vi.hoisted(() => ({ createRemixArtifact: vi.fn(), attachRemixMedia: vi.fn(), fetchRemixArtifact: vi.fn(), fetchRemixOperation: vi.fn(), fetchRemixTaskStatus: vi.fn(), submitRemixProcessAction: vi.fn() }));
const canvasApi = vi.hoisted(() => ({ listFreezoneWorkflowRuns: vi.fn(), getFreezoneWorkflowRun: vi.fn() }));
const save = vi.hoisted(() => ({ flushProductionPlanV2AutoSave: vi.fn(), scheduleProductionPlanV2AutoSave: vi.fn() }));
const commands = vi.hoisted(() => ({ CANVAS_CHAT_COMMANDS_SCHEMA_VERSION: 'canvas_chat_commands.v1',
  FREEZONE_WORKFLOW_RUN_UPDATED_EVENT: 'freezone-workflow-run-updated', applyCanvasChatCommandsAsync: vi.fn() }));
const native = vi.hoisted(() => ({ executeProductionCanvasCommand: vi.fn(), bindProductionCanvasCandidate: vi.fn() }));
const store = {
  nodes: [{ id: 'plan', type: 'scriptNode', data: {} as Record<string, unknown> }] as Array<{ id: string; type: string; data: Record<string, unknown> }>,
  edges: [] as Array<{ source: string; target: string }>,
  addEdge: vi.fn((source: string, target: string) => { store.edges.push({ source, target }); return 'edge'; }),
  addNode: vi.fn(() => 'created-material'),
  deleteNodes: vi.fn(),
  findNodePosition: vi.fn(() => ({ x: 400, y: 100 })),
  setSelectedNode: vi.fn(),
  requestFocusNode: vi.fn(),
  updateNodeData: vi.fn((id: string, patch: Record<string, unknown>) => {
    const node = store.nodes.find((item) => item.id === id);
    if (node) node.data = { ...node.data, ...patch };
  }),
};
vi.mock('@/api/remix', () => api);
vi.mock('@/api/canvas', () => canvasApi);
vi.mock('@/features/canvas/remix/productionPlanV2AutoSave', () => save);
vi.mock('@/features/freezone/canvasChatCommands', () => commands);
vi.mock('@/features/canvas/remix/productionCanvasCommand', () => native);
vi.mock('@/stores/canvasStore', () => ({ useCanvasStore: { getState: () => store } }));
import { ProductionPlanV2Editor } from './ProductionPlanV2Editor';

const draft: ProductionPlanV2 = { schema_version: 2, goal: '作品', output: { width: 1080, height: 1920, fps_num: 25, fps_den: 1 },
  beats: [{ id: 'beat', purpose: '开场', duration_frames: 100, turns: [{ id: 'turn', speaker: 'A', text: '你好', start_frame: 10, end_frame: 30,
    timing_basis: 'aligned', words: [{ id: 'word', text: '你好', start_frame: 12, end_frame: 25 }] }] }],
  resources: [], placements: [{ id: 'visual', kind: 'visual', role: 'primary', track_id: 'visual:primary', description: '问候画面',
    start: { kind: 'program_frame', frame: 0 }, end: { kind: 'program_frame', frame: 30 } }], provenance: {} };

describe('production plan v2 editor', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    store.nodes = [{ id: 'plan', type: 'scriptNode', data: {} }];
    store.edges = [];
    store.nodes[0].data = { productionPlanV2Draft: structuredClone(draft), productionPlanV2Ref: { artifactId: 'plan-artifact', revision: 1 }, productionPlanV2Dirty: true };
    save.flushProductionPlanV2AutoSave.mockResolvedValue(null);
    native.executeProductionCanvasCommand.mockResolvedValue({ nodeId: 'plan', output: { plan_ref: { artifact_id: 'plan-artifact', revision: 2 }, changed_ids: [], invalidated: [], blockers: [] } });
    native.executeProductionCanvasCommand.mockImplementation(async (request) => {
      const next = structuredClone(store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2);
      if (request.command.kind === 'edit_turn') bindPlanSpeechSource(next, request.command.target_id,
        request.command.payload.speech_placement_id as string | null);
      if (request.command.kind === 'upsert_placement') {
        next.placements = next.placements.map((row) => row.id === request.command.target_id ? request.command.payload.placement : row);
      }
      store.updateNodeData('plan', { productionPlanV2Draft: next, productionPlanV2Ref: { artifactId: 'plan-artifact', revision: 2 } });
      return { nodeId: 'plan', output: { plan_ref: { artifact_id: 'plan-artifact', revision: 2 }, changed_ids: [request.command.target_id], invalidated: [], blockers: [] } };
    });
    commands.applyCanvasChatCommandsAsync.mockResolvedValue({ errors: [], commandResults: [] });
    canvasApi.listFreezoneWorkflowRuns.mockResolvedValue({ runs: [] });
    canvasApi.getFreezoneWorkflowRun.mockResolvedValue(null);
  });

  it('offers explicit arrangement for a saved editable outline without submitting on render', async () => {
    const outline = { ...structuredClone(draft), authoring_stage: 'outline' as const, placements: [] };
    store.nodes[0].data = { productionPlanV2Draft: outline, productionPlanV2Ref: { artifactId: 'plan-artifact', revision: 1 } };
    save.flushProductionPlanV2AutoSave.mockResolvedValue({ artifactId: 'plan-artifact', revision: 3 });
    render(<ProductionPlanV2Editor id="plan" project="demo" canvasId="canvas" data={store.nodes[0].data} onClose={() => {}} onCompose={() => {}} />);
    const button = await screen.findByRole('button', { name: 'AI 安排画面与声音' });
    await waitFor(() => expect(button).not.toBeDisabled());
    expect(commands.applyCanvasChatCommandsAsync).not.toHaveBeenCalled();
    fireEvent.click(button);
    await waitFor(() => expect(commands.applyCanvasChatCommandsAsync).toHaveBeenCalledWith(expect.arrayContaining([
      expect.objectContaining({ commands: [{ type: 'run_node_action', node_id: 'plan', action: 'generate_production_plan_arrangement',
        parameters: { plan_ref: { artifact_id: 'plan-artifact', revision: 3 } } }] }),
    ]), { projectId: 'demo', canvasId: 'canvas' }));
  });

  it('recovers the running arrangement instead of an older completed outline', async () => {
    store.nodes[0].data = { productionPlanV2Draft: { ...structuredClone(draft), authoring_stage: 'outline', placements: [] } };
    canvasApi.listFreezoneWorkflowRuns.mockResolvedValue({ runs: [
      { run_id: 'outline-run', created_at: '2026-10-15T01:00:00Z', status: 'completed',
        actions: [{ node_id: 'plan', action: 'generate_production_plan_draft', status: 'completed' }] },
      { run_id: 'arrange-run', created_at: '2026-10-15T02:00:00Z', status: 'running',
        actions: [{ node_id: 'plan', action: 'generate_production_plan_arrangement', status: 'running', task_key: 'arrange-task' }] },
    ] });
    render(<ProductionPlanV2Editor id="plan" project="demo" canvasId="canvas" data={store.nodes[0].data} onClose={() => {}} onCompose={() => {}} />);
    await waitFor(() => expect(screen.getByRole('button', { name: '正在安排画面与声音…' })).toBeDisabled());
    expect(commands.applyCanvasChatCommandsAsync).not.toHaveBeenCalled();
  });

  it('keeps edited dialogue visible after arrangement fails', async () => {
    const edited = { ...structuredClone(draft), authoring_stage: 'outline', placements: [] };
    edited.beats[0].turns[0].text = '人工修改后的台词';
    store.nodes[0].data = { productionPlanV2Draft: edited };
    canvasApi.listFreezoneWorkflowRuns.mockResolvedValue({ runs: [{ run_id: 'arrange-run', created_at: '2026-10-15T02:00:00Z', status: 'failed',
      actions: [{ node_id: 'plan', action: 'generate_production_plan_arrangement', status: 'failed', error: '编排超时' }] }] });
    render(<ProductionPlanV2Editor id="plan" project="demo" canvasId="canvas" data={store.nodes[0].data} onClose={() => {}} onCompose={() => {}} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('编排超时');
    expect(screen.getByDisplayValue('人工修改后的台词')).toBeVisible();
    expect(commands.applyCanvasChatCommandsAsync).not.toHaveBeenCalled();
  });

  it('offers goal based drafting for one empty editable plan', async () => {
    store.nodes[0].data = { productionPlanV2Draft: { schema_version: 2, goal: '黑咖啡访谈短片',
      output: { width: 1080, height: 1920, fps_num: 30, fps_den: 1 }, beats: [], resources: [], placements: [], provenance: {} },
      productionPlanV2Dirty: false };
    render(<ProductionPlanV2Editor id="plan" project="demo" canvasId="canvas" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'AI 起草作品方案' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'AI 起草作品方案' }));
    await waitFor(() => expect(commands.applyCanvasChatCommandsAsync).toHaveBeenCalledWith([
      expect.objectContaining({ commands: [{ type: 'run_node_action', node_id: 'plan', action: 'generate_production_plan_draft', parameters: { topic: '黑咖啡访谈短片' } }] }),
    ], expect.objectContaining({ projectId: 'demo', canvasId: 'canvas' })));
  });

  it('shows submission while the command is pending and reports an admission failure', async () => {
    store.nodes[0].data = { productionPlanV2Draft: { schema_version: 2, goal: '黑咖啡访谈短片',
      output: { width: 1080, height: 1920, fps_num: 30, fps_den: 1 }, beats: [], resources: [], placements: [], provenance: {} },
      productionPlanV2Dirty: false };
    let finish!: (result: unknown) => void;
    commands.applyCanvasChatCommandsAsync.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    render(<ProductionPlanV2Editor id="plan" project="demo" canvasId="canvas" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'AI 起草作品方案' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'AI 起草作品方案' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '正在提交…' })).toBeDisabled());
    await act(async () => finish({ errors: [], commandResults: [{ type: 'run_node_action', status: 'error',
      error: 'production automation stopped' }] }));
    expect(await screen.findByText('production automation stopped')).toBeVisible();
    expect(screen.getByRole('button', { name: 'AI 起草作品方案' })).toBeEnabled();
  });

  it('recognizes an already submitted draft when the editor reopens', async () => {
    store.nodes[0].data = { productionPlanV2Draft: { schema_version: 2, goal: '黑咖啡访谈短片',
      output: { width: 1080, height: 1920, fps_num: 30, fps_den: 1 }, beats: [], resources: [], placements: [], provenance: {} },
      productionPlanV2Dirty: false };
    canvasApi.listFreezoneWorkflowRuns.mockResolvedValue({ runs: [{ run_id: 'run-one', status: 'running', created_at: '2026-10-15T01:00:00Z',
      actions: [{ node_id: 'plan', action: 'generate_production_plan_draft', status: 'running', task_key: 'task-one',
        payload: { topic: '黑咖啡访谈短片' } }] }] });
    render(<ProductionPlanV2Editor id="plan" project="demo" canvasId="canvas" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
    expect(await screen.findByRole('button', { name: '正在起草…' })).toBeDisabled();
    expect(commands.applyCanvasChatCommandsAsync).not.toHaveBeenCalled();
  });

  it('does not resubmit an older interrupted run while its task is still active', async () => {
    store.nodes[0].data = { productionPlanV2Draft: { schema_version: 2, goal: '黑咖啡访谈短片',
      output: { width: 1080, height: 1920, fps_num: 30, fps_den: 1 }, beats: [], resources: [], placements: [], provenance: {} } };
    canvasApi.listFreezoneWorkflowRuns.mockResolvedValue({ runs: [{ run_id: 'old-browser-lease', status: 'interrupted',
      created_at: '2026-10-15T01:00:00Z', actions: [{ node_id: 'plan', action: 'generate_production_plan_draft',
        status: 'running', task_key: 'task-still-active' }] }] });
    render(<ProductionPlanV2Editor id="plan" project="demo" canvasId="canvas" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
    expect(await screen.findByRole('button', { name: '正在起草…' })).toBeDisabled();
    expect(commands.applyCanvasChatCommandsAsync).not.toHaveBeenCalled();
  });

  it('shows a blocked draft action reason after reopening', async () => {
    store.nodes[0].data = { productionPlanV2Draft: { schema_version: 2, goal: '黑咖啡访谈短片',
      output: { width: 1080, height: 1920, fps_num: 30, fps_den: 1 }, beats: [], resources: [], placements: [], provenance: {} } };
    canvasApi.listFreezoneWorkflowRuns.mockResolvedValue({ runs: [{ run_id: 'run-blocked', status: 'failed',
      created_at: '2026-10-15T01:00:00Z', actions: [{ node_id: 'plan', action: 'generate_production_plan_draft',
        status: 'blocked', error: '原节点已被后续编辑覆盖' }] }] });
    render(<ProductionPlanV2Editor id="plan" project="demo" canvasId="canvas" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('原节点已被后续编辑覆盖');
    expect(screen.getByRole('button', { name: 'AI 起草作品方案' })).toBeEnabled();
  });

  it('does not submit before checking an existing run on reopen', async () => {
    store.nodes[0].data = { productionPlanV2Draft: { schema_version: 2, goal: '黑咖啡访谈短片',
      output: { width: 1080, height: 1920, fps_num: 30, fps_den: 1 }, beats: [], resources: [], placements: [], provenance: {} },
      productionPlanV2Dirty: false };
    let finishLookup!: (result: unknown) => void;
    canvasApi.listFreezoneWorkflowRuns.mockImplementation(() => new Promise((resolve) => { finishLookup = resolve; }));
    render(<ProductionPlanV2Editor id="plan" project="demo" canvasId="canvas" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
    const checking = screen.getByRole('button', { name: '正在核对任务…' });
    expect(checking).toBeDisabled();
    fireEvent.click(checking);
    expect(commands.applyCanvasChatCommandsAsync).not.toHaveBeenCalled();
    await act(async () => finishLookup({ runs: [{ run_id: 'run-old', status: 'running', created_at: '2026-10-15T01:00:00Z',
      actions: [{ node_id: 'plan', action: 'generate_production_plan_draft', status: 'running', task_key: 'task-old' }] }] }));
    expect(screen.getByRole('button', { name: '正在起草…' })).toBeDisabled();
    expect(commands.applyCanvasChatCommandsAsync).not.toHaveBeenCalled();
  });

  it('keeps the same submitted run pending until the server reports a terminal result', async () => {
    store.nodes[0].data = { productionPlanV2Draft: { schema_version: 2, goal: '黑咖啡访谈短片',
      output: { width: 1080, height: 1920, fps_num: 30, fps_den: 1 }, beats: [], resources: [], placements: [], provenance: {} },
      productionPlanV2Dirty: false };
    commands.applyCanvasChatCommandsAsync.mockResolvedValue({ errors: [], commandResults: [{
      type: 'run_node_action', status: 'pending', output: { workflow_run_id: 'run-new' },
    }] });
    render(<ProductionPlanV2Editor id="plan" project="demo" canvasId="canvas" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'AI 起草作品方案' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'AI 起草作品方案' }));
    expect(await screen.findByRole('button', { name: '任务提交中…' })).toBeDisabled();
    expect(commands.applyCanvasChatCommandsAsync).toHaveBeenCalledTimes(1);
    canvasApi.listFreezoneWorkflowRuns.mockResolvedValue({ runs: [{ run_id: 'run-new', status: 'failed',
      created_at: '2026-10-15T01:00:00Z', actions: [{ node_id: 'plan', action: 'generate_production_plan_draft', status: 'failed' }] }] });
    act(() => window.dispatchEvent(new Event(commands.FREEZONE_WORKFLOW_RUN_UPDATED_EVENT)));
    expect(await screen.findByRole('button', { name: 'AI 起草作品方案' })).toBeEnabled();
    expect(screen.getByRole('alert')).toHaveTextContent('起草任务失败');
  });

  it('rechecks the accepted run until its durable task receipt appears', async () => {
    store.nodes[0].data = { productionPlanV2Draft: { schema_version: 2, goal: '黑咖啡访谈短片',
      output: { width: 1080, height: 1920, fps_num: 30, fps_den: 1 }, beats: [], resources: [], placements: [], provenance: {} } };
    commands.applyCanvasChatCommandsAsync.mockResolvedValue({ errors: [], commandResults: [{
      type: 'run_node_action', status: 'pending', output: { workflow_run_id: 'run-new' },
    }] });
    canvasApi.getFreezoneWorkflowRun.mockResolvedValueOnce({ run_id: 'run-new', status: 'running',
      actions: [{ node_id: 'plan', action: 'generate_production_plan_draft', status: 'pending' }] })
      .mockResolvedValue({ run_id: 'run-new', status: 'running',
        actions: [{ node_id: 'plan', action: 'generate_production_plan_draft', status: 'running', task_key: 'task-new' }] });
    render(<ProductionPlanV2Editor id="plan" project="demo" canvasId="canvas" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'AI 起草作品方案' })).toBeEnabled());
    vi.useFakeTimers();
    try {
      await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'AI 起草作品方案' })); });
      expect(screen.getByRole('button', { name: '任务提交中…' })).toBeDisabled();
      await act(async () => { await vi.advanceTimersByTimeAsync(11_000); });
      expect(screen.getByRole('button', { name: '正在起草…' })).toBeDisabled();
      expect(canvasApi.getFreezoneWorkflowRun).toHaveBeenCalledTimes(2);
      expect(commands.applyCanvasChatCommandsAsync).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('lets a silent beat follow its chosen media duration', () => {
    const current = structuredClone(draft);
    current.beats[0].turns = [];
    current.placements[0].start = { kind: 'beat', ref_id: 'beat', edge: 'start' };
    current.placements[0].end = { kind: 'beat', ref_id: 'beat', edge: 'end' };
    store.nodes[0].data.productionPlanV2Draft = current;
    render(<ProductionPlanV2Editor id="plan" project="demo" canvasId="canvas" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
    fireEvent.change(screen.getByRole('combobox', { name: '段落 1 时长依据' }), { target: { value: 'visual' } });
    expect((store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).beats[0].duration_source_placement_id).toBe('visual');
  });

  it('opens an existing dialogue subtitle in plan materials without creating a duplicate or media picker', () => {
    const current = structuredClone(draft);
    current.placements.push({ id: 'legacy-caption', kind: 'subtitle', role: 'dialogue', track_id: 'subtitles', turn_id: 'turn',
      start: { kind: 'turn', ref_id: 'turn', edge: 'start' }, end: { kind: 'turn', ref_id: 'turn', edge: 'end' } });
    store.nodes[0].data.productionPlanV2Draft = current;
    render(<ProductionPlanV2Editor id="plan" project="demo" data={store.nodes[0].data} onClose={() => {}} onCompose={() => {}} />);
    fireEvent.click(screen.getByRole('button', { name: '台词主线' }));
    fireEvent.click(screen.getByRole('button', { name: '编辑字幕' }));
    expect(screen.getByRole('button', { name: '台词主线' })).toBeVisible();
    expect(store.nodes[0].data.productionPlanV2FocusPlacementId).toBe('legacy-caption');
    expect((store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).placements.filter((item) => item.kind === 'subtitle')).toHaveLength(1);
    expect(screen.queryByRole('dialog', { name: /选择.*素材/ })).not.toBeInTheDocument();
  });

  it('opens the plan and materials first and can return there from speech timing', () => {
    render(<ProductionPlanV2Editor id="plan" project="demo" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);

    expect(screen.getByRole('button', { name: '选择画面 1 素材' })).toBeVisible();
    expect(screen.queryByRole('button', { name: '校准 段落 1 台词 1 时间' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '校准台词时间' }));
    expect(screen.getByRole('button', { name: '校准 段落 1 台词 1 时间' })).toBeVisible();

    fireEvent.click(screen.getByRole('button', { name: '收起校准' }));
    expect(screen.getByRole('button', { name: '选择画面 1 素材' })).toBeVisible();
  });

  it('keeps editable dialogue beside visuals after migrating to v2', () => {
    render(<ProductionPlanV2Editor id="plan" project="demo" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
    expect(screen.getByText('画面 0/1')).toBeVisible();
    expect(screen.getByLabelText('段落 1 台词 1')).toHaveValue('你好');
    fireEvent.change(screen.getByLabelText('段落 1 台词 1'), { target: { value: '早上好' } });
    expect((store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).beats[0].turns[0].text).toBe('早上好');
    expect(screen.getByRole('button', { name: '选择画面 1 素材' })).toBeVisible();
    expect(save.scheduleProductionPlanV2AutoSave).toHaveBeenCalledWith('demo', 'plan');
  });

  it('places selected author text on a new material without inventing media timing', async () => {
    render(<ProductionPlanV2Editor id="plan" project="demo" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
    const text = screen.getByText('你好').firstChild!;
    const range = document.createRange();
    range.setStart(text, 0); range.setEnd(text, 2);
    const selection = window.getSelection()!;
    selection.removeAllRanges(); selection.addRange(range);
    fireEvent.mouseUp(screen.getByLabelText('可选取台词范围'));
    fireEvent.click(screen.getByRole('button', { name: '选中台词添加画面' }));
    await waitFor(() => expect((store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).placements).toHaveLength(2));
    const placement = (store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).placements[1];
    expect(placement.resource_id).toBeUndefined();
    expect(placement.start).toMatchObject({ kind: 'text', ref_id: 'turn', char_offset: 0,
      text_hash: expect.stringMatching(/^[a-f0-9]{64}$/), affinity: 'after' });
    expect(placement.end).toMatchObject({ kind: 'text', ref_id: 'turn', char_offset: 2, affinity: 'before' });
    expect(save.scheduleProductionPlanV2AutoSave).toHaveBeenCalledWith('demo', 'plan');
    selection.removeAllRanges();
  });

  it('replaces only the selected picture use with a new dialogue range', async () => {
    const current = structuredClone(draft);
    current.placements[0].start = { kind: 'turn', ref_id: 'turn', edge: 'start' };
    current.placements[0].end = { kind: 'turn', ref_id: 'turn', edge: 'end' };
    current.placements.push({ ...structuredClone(current.placements[0]), id: 'visual-again', description: '再次问候' });
    store.nodes[0].data.productionPlanV2Draft = current;
    render(<ProductionPlanV2Editor id="plan" project="demo" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
    fireEvent.click(screen.getByRole('button', { name: /问候画面/ }));
    const text = screen.getByText('你好').firstChild!;
    const range = document.createRange();
    range.setStart(text, 1); range.setEnd(text, 2);
    const selection = window.getSelection()!;
    selection.removeAllRanges(); selection.addRange(range);
    fireEvent.mouseUp(screen.getByLabelText('可选取台词范围'));
    fireEvent.click(screen.getByRole('button', { name: '更新选中素材范围' }));
    await waitFor(() => expect((store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).placements[0].start.kind).toBe('text'));
    const updated = (store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).placements;
    expect(updated).toHaveLength(2);
    expect(updated[0].start).toMatchObject({ kind: 'text', ref_id: 'turn', char_offset: 1 });
    expect(updated[0].end).toMatchObject({ kind: 'text', ref_id: 'turn', char_offset: 2 });
    expect(updated[1]).toEqual(current.placements[1]);
    selection.removeAllRanges();
  });

  it('keeps the saved in-memory draft while its revision changes during a cue edit', () => {
    store.nodes[0].data.productionPlanV2Dirty = false;
    const { rerender } = render(<ProductionPlanV2Editor id="plan" project="demo" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
    store.nodes[0].data = { ...store.nodes[0].data, productionPlanV2Ref: { artifactId: 'plan-artifact', revision: 2 } };
    rerender(<ProductionPlanV2Editor id="plan" project="demo" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
    expect(api.fetchRemixArtifact).not.toHaveBeenCalled();
    expect(screen.getByLabelText('段落 1 台词 1')).toHaveValue('你好');
  });

  it('restores beat order and graphic creation in the main plan view', () => {
    const current = structuredClone(draft);
    current.beats.push({ id: 'second', purpose: '结尾', duration_frames: 50, turns: [] });
    store.nodes[0].data.productionPlanV2Draft = current;
    render(<ProductionPlanV2Editor id="plan" project="demo" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
    fireEvent.click(screen.getByRole('button', { name: '段落 2 上移' }));
    expect((store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).beats.map((beat) => beat.id)).toEqual(['second', 'beat']);
    fireEvent.click(screen.getAllByRole('button', { name: '添加图形效果' })[0]);
    expect((store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).placements).toEqual(expect.arrayContaining([
      expect.objectContaining({ kind: 'graphic', role: 'effect', graphic: expect.objectContaining({ template: 'question_card' }) }),
    ]));
  });

  it('removes a segment and its linked placement through the autosaved v2 draft', () => {
    const current = structuredClone(draft);
    current.placements[0].start = { kind: 'beat', ref_id: 'beat', edge: 'start' };
    current.placements[0].end = { kind: 'beat', ref_id: 'beat', edge: 'end' };
    current.beats.push({ id: 'second', purpose: '结尾', duration_frames: 50, turns: [] });
    store.nodes[0].data.productionPlanV2Draft = current;
    store.nodes[0].data.productionPlanV2PendingBindings = { visual: 'creating-node' };
    render(<ProductionPlanV2Editor id="plan" project="demo" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
    fireEvent.click(screen.getByRole('button', { name: '段落 1 删除' }));
    const changed = store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2;
    expect(changed.beats.map((beat) => beat.id)).toEqual(['second']);
    expect(changed.placements).toEqual([]);
    expect(store.nodes[0].data.productionPlanV2PendingBindings).toEqual({});
    expect(save.scheduleProductionPlanV2AutoSave).toHaveBeenCalledWith('demo', 'plan');
  });

  it('upgrades an already saved migrated cue through the existing autosave path', async () => {
    const current = structuredClone(draft);
    current.provenance.source_plan_ref = { artifact_id: 'old-plan', revision: 1 };
    current.placements[0].id = 'visual:beat:picture';
    current.placements[0].end = { kind: 'program_frame', frame: 100 };
    store.nodes[0].data.productionPlanV2Draft = current;
    render(<ProductionPlanV2Editor id="plan" project="demo" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);

    await waitFor(() => expect((store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).placements[0].end)
      .toMatchObject({ kind: 'beat', ref_id: 'beat', edge: 'end' }));
    expect(save.scheduleProductionPlanV2AutoSave).toHaveBeenCalledWith('demo', 'plan');
  });

  it('binds a canvas material from the restored plan view into the v2 plan', async () => {
    store.nodes.push({ id: 'clip', type: 'videoNode', data: { displayName: '开场视频', videoUrl: '/static/projects/demo/freezone/clip.mp4' } });
    native.bindProductionCanvasCandidate.mockImplementation(async () => {
      const next = structuredClone(store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2);
      next.resources.push({ id: `resource:clip:${'a'.repeat(12)}`, node_id: 'clip', media_kind: 'video', media_path: 'freezone/clip.mp4', media_hash: 'a'.repeat(64), duration_us: 4_000_000, has_audio: true });
      next.placements[0].resource_id = `resource:clip:${'a'.repeat(12)}`;
      store.updateNodeData('plan', { productionPlanV2Draft: next, productionPlanV2Ref: { artifactId: 'plan-artifact', revision: 3 } });
      return { nodeId: 'plan', output: { plan_ref: { artifact_id: 'plan-artifact', revision: 3 }, changed_ids: ['visual'], invalidated: [], blockers: [] } };
    });
    render(<ProductionPlanV2Editor id="plan" project="demo" canvasId="canvas" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);

    fireEvent.click(screen.getByRole('button', { name: '选择画面 1 素材' }));
    fireEvent.click(screen.getByRole('button', { name: '绑定 开场视频' }));

    await waitFor(() => expect((store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).placements[0].resource_id).toBe(`resource:clip:${'a'.repeat(12)}`));
    expect(native.bindProductionCanvasCandidate).toHaveBeenCalledWith('demo', 'canvas', 'plan', 'visual', 'clip');
    expect(native.executeProductionCanvasCommand).not.toHaveBeenCalled();
  });

  it('plans an unbound picture as one dialogue source through the saved command', async () => {
    const current = structuredClone(draft);
    current.beats[0].turns.push({ id: 'second', speaker: 'A', text: '再见' });
    current.placements[0].end = { kind: 'program_frame', frame: 100 };
    store.nodes[0].data.productionPlanV2Draft = current;
    save.flushProductionPlanV2AutoSave.mockResolvedValue({ artifactId: 'plan-artifact', revision: 2 });
    native.executeProductionCanvasCommand.mockImplementation(async (request) => {
      const next = structuredClone(store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2);
      next.beats[0].turns[0].speech_placement_id = request.command.payload.speech_placement_id;
      store.updateNodeData('plan', { productionPlanV2Draft: next, productionPlanV2Ref: { artifactId: 'plan-artifact', revision: 3 } });
      return { nodeId: 'plan', output: { plan_ref: { artifact_id: 'plan-artifact', revision: 3 }, changed_ids: ['turn'], invalidated: [], blockers: [] } };
    });
    render(<ProductionPlanV2Editor id="plan" project="demo" canvasId="canvas" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
    fireEvent.click(screen.getByRole('button', { name: '台词主线' }));
    fireEvent.change(screen.getByLabelText('段落 1 台词 1 发声素材'), { target: { value: 'visual' } });
    await waitFor(() => expect(native.executeProductionCanvasCommand).toHaveBeenCalledWith(expect.objectContaining({ command: expect.objectContaining({
      kind: 'edit_turn', target_id: 'turn', payload: { speech_placement_id: 'visual', invalidate_speech_timing: true }, plan_ref: { artifact_id: 'plan-artifact', revision: 2 },
    }) }), 'demo', 'canvas'));
    expect((store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).beats[0].turns[0].speech_placement_id).toBe('visual');
    expect(screen.getByLabelText('段落 1 台词 1 发声素材')).toHaveValue('visual');
    expect(screen.getByRole('button', { name: '校准 段落 1 台词 1 时间' })).toBeDisabled();
    expect(screen.getByText('独立声音 0/0')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '先准备发声素材' }));
    expect(screen.getByRole('complementary', { name: '选择画面素材' })).toBeInTheDocument();
  });

  it('shows one shared video voice and counts only extra audio requests', () => {
    const current = structuredClone(draft);
    current.beats[0].turns = [{ id: 'first', speaker: 'A', text: '你好', speech_placement_id: 'visual' },
      { id: 'second', speaker: 'B', text: '再见', speech_placement_id: 'visual' }];
    current.placements[0].end = { kind: 'program_frame', frame: 100 };
    store.nodes[0].data.productionPlanV2Draft = current;
    render(<ProductionPlanV2Editor id="plan" project="demo" canvasId="canvas" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
    expect(screen.getAllByText('由画面 1 发声')).toHaveLength(2);
    expect(screen.getByLabelText('段落 1 台词 1 声音来源')).toBeInTheDocument();
    expect(screen.queryByLabelText('段落 1 台词 2 声音来源')).not.toBeInTheDocument();
    expect(screen.getByText('独立声音 0/0')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '校准同源 2 句台词' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '添加声音' }));
    expect(screen.getByText('独立声音 0/1')).toBeInTheDocument();
  });

  it('changes every turn sharing an uncalibrated source in one saved command', async () => {
    const current = structuredClone(draft);
    current.beats[0].turns = [{ id: 'first', speaker: 'A', text: '你好', speech_placement_id: 'visual' },
      { id: 'second', speaker: 'B', text: '再见', speech_placement_id: 'visual' }];
    current.placements[0].end = { kind: 'program_frame', frame: 100 };
    store.nodes[0].data.productionPlanV2Draft = current;
    native.executeProductionCanvasCommand.mockImplementation(async (request) => {
      const next = structuredClone(store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2);
      for (const edit of request.command.payload.commands) {
        next.beats[0].turns.find((turn) => turn.id === edit.target_id)!.speech_placement_id = edit.payload.speech_placement_id;
      }
      store.updateNodeData('plan', { productionPlanV2Draft: next, productionPlanV2Ref: { artifactId: 'plan-artifact', revision: 2 } });
      return { nodeId: 'plan', output: { plan_ref: { artifact_id: 'plan-artifact', revision: 2 }, changed_ids: ['first', 'second'], invalidated: [], blockers: [] } };
    });
    render(<ProductionPlanV2Editor id="plan" project="demo" canvasId="canvas" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
    fireEvent.change(screen.getByLabelText('段落 1 台词 1 声音来源'), { target: { value: '__none__' } });
    await waitFor(() => expect((store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).beats[0].turns.map((turn) => turn.speech_placement_id)).toEqual([null, null]));
    expect(native.executeProductionCanvasCommand).toHaveBeenCalledWith(expect.objectContaining({ command: expect.objectContaining({
      kind: 'batch', payload: { commands: [
        { kind: 'edit_turn', target_id: 'first', payload: { speech_placement_id: null } },
        { kind: 'edit_turn', target_id: 'second', payload: { speech_placement_id: null } },
      ] },
    }) }), 'demo', 'canvas');
  });

  it('reassigns both turns of one draft shared take through one native command', async () => {
    const current = structuredClone(draft);
    current.beats[0].turns = [{ id: 'first', speaker: 'A', text: '你好', speech_placement_id: 'voice' },
      { id: 'second', speaker: 'B', text: '再见', speech_placement_id: 'voice' }];
    current.placements[0].end = { kind: 'program_frame', frame: 100 };
    current.placements.push({ id: 'voice', kind: 'audio', role: 'speech', track_id: 'audio:speech', resource_id: 'audio',
      start: { kind: 'program_frame', frame: 0 }, end: { kind: 'program_frame', frame: 100 } });
    current.resources.push({ id: 'audio', node_id: 'audio-node', media_kind: 'audio', media_path: 'media/voice.wav',
      media_hash: 'a'.repeat(64), duration_us: 4_000_000, has_audio: true });
    current.speech_takes = [{ id: 'take', beat_id: 'beat', resource_id: 'audio', turn_ids: ['first', 'second'],
      origin_placement_id: 'voice', source_in_us: 0, source_out_us: null, speed_num: 1, speed_den: 1, lead_in_frames: 0 }];
    current.speech_cues = (current.beats[0].turns.map((turn) => ({ id: `cue:${turn.id}`, turn_id: turn.id, take_id: 'take',
      text: turn.text, timing_source: 'asr' as const, timing_status: 'draft' as const, word_ids: [] }))) as unknown as NonNullable<ProductionPlanV2['speech_cues']>;
    store.nodes[0].data.productionPlanV2Draft = current;
    native.executeProductionCanvasCommand.mockImplementation(async (request) => {
      const next = structuredClone(store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2);
      next.beats[0].turns.forEach((turn) => { turn.speech_placement_id = request.command.payload.placement_id; });
      next.speech_takes = []; next.speech_cues = [];
      store.updateNodeData('plan', { productionPlanV2Draft: next, productionPlanV2Ref: { artifactId: 'plan-artifact', revision: 2 } });
      return { nodeId: 'plan', output: { plan_ref: { artifact_id: 'plan-artifact', revision: 2 }, changed_ids: ['beat'], invalidated: [], blockers: [] } };
    });
    render(<ProductionPlanV2Editor id="plan" project="demo" canvasId="canvas" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
    expect(screen.getByLabelText('段落 1 台词 1 声音来源')).toBeEnabled();
    fireEvent.change(screen.getByLabelText('段落 1 台词 1 声音来源'), { target: { value: 'visual' } });
    await waitFor(() => expect((store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).beats[0].turns.map((turn) => turn.speech_placement_id)).toEqual(['visual', 'visual']));
    expect(native.executeProductionCanvasCommand).toHaveBeenCalledWith(expect.objectContaining({ command: expect.objectContaining({
      kind: 'reassign_speech_source', target_id: 'beat', payload: { placement_id: 'visual', turn_ids: ['first', 'second'] },
    }) }), 'demo', 'canvas');
  });

  it('keeps an accepted shared take source read-only with an evidence explanation', () => {
    const current = structuredClone(draft);
    current.beats[0].turns = [{ id: 'first', speaker: 'A', text: '你好', speech_placement_id: 'visual' },
      { id: 'second', speaker: 'B', text: '再见', speech_placement_id: 'visual' }];
    current.placements[0].end = { kind: 'program_frame', frame: 100 };
    current.speech_takes = [{ id: 'take', beat_id: 'beat', resource_id: 'clip', turn_ids: ['first', 'second'], origin_placement_id: 'visual',
      source_in_us: 0, source_out_us: null, speed_num: 1, speed_den: 1, lead_in_frames: 0 }];
    current.speech_cues = [{ id: 'cue:first', turn_id: 'first', take_id: 'take', text: '你好', timing_source: 'manual',
      timing_status: 'ready', source_start_us: 0, source_end_us: 1_000_000, word_ids: [],
      evidence_ref: { artifact_id: 'accepted', revision: 1 } }];
    store.nodes[0].data.productionPlanV2Draft = current;
    render(<ProductionPlanV2Editor id="plan" project="demo" canvasId="canvas" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
    expect(screen.getByLabelText('段落 1 台词 1 声音来源')).toBeDisabled();
    expect(screen.getByText(/已确认语音时间或词锚点/)).toBeInTheDocument();
    expect(native.executeProductionCanvasCommand).not.toHaveBeenCalled();
  });

  it('saves ordinary video sound ownership with an upsert placement command', async () => {
    const current = structuredClone(draft);
    current.resources.push({ id: 'clip', node_id: 'clip-node', media_kind: 'video', media_path: 'media/clip.mp4',
      media_hash: 'a'.repeat(64), duration_us: 4_000_000, has_audio: true });
    current.placements[0].resource_id = 'clip';
    store.nodes[0].data.productionPlanV2Draft = current;
    render(<ProductionPlanV2Editor id="plan" project="demo" canvasId="canvas" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
    fireEvent.click(screen.getByRole('checkbox', { name: '画面 1 使用视频声音' }));
    await waitFor(() => expect(native.executeProductionCanvasCommand).toHaveBeenCalledWith(expect.objectContaining({ command: expect.objectContaining({
      kind: 'upsert_placement', target_id: 'visual', payload: { placement: expect.objectContaining({ id: 'visual', source_audio: true }) },
    }) }), 'demo', 'canvas'));
    expect((store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).placements[0].source_audio).toBe(true);
  });

  it('creates a material node for an unbound v2 placement and remembers the target', () => {
    const onClose = vi.fn();
    save.flushProductionPlanV2AutoSave.mockResolvedValue({ artifactId: 'plan-artifact', revision: 1 });
    render(<ProductionPlanV2Editor id="plan" project="demo" data={store.nodes[0].data} onClose={onClose} onCompose={() => undefined} />);

    fireEvent.click(screen.getByRole('button', { name: '选择画面 1 素材' }));
    fireEvent.click(screen.getByRole('button', { name: '生成视频' }));

    expect(store.nodes[0].data.productionPlanV2PendingBindings).toEqual({ visual: 'created-material' });
    expect(store.edges).toContainEqual({ source: 'created-material', target: 'plan' });
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('creates a word-anchored range in the same autosaved plan', () => {
    render(<ProductionPlanV2Editor id="plan" project="demo" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
    fireEvent.click(screen.getByRole('button', { name: '添加命名范围' }));
    fireEvent.change(screen.getByLabelText('范围 1 名称'), { target: { value: '问候' } });
    const wordStart = screen.getAllByRole('option', { name: '词「你好」开始' })[0] as HTMLOptionElement;
    const wordEnd = screen.getAllByRole('option', { name: '词「你好」结束' })[0] as HTMLOptionElement;
    fireEvent.change(screen.getByLabelText('范围 1 开始'), { target: { value: wordStart.value } });
    fireEvent.change(screen.getByLabelText('范围 1 结束'), { target: { value: wordEnd.value } });
    expect((store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).selections).toMatchObject([{
      label: '问候', start: { kind: 'word', ref_id: 'word', edge: 'start' }, end: { kind: 'word', ref_id: 'word', edge: 'end' },
    }]);
    expect(save.scheduleProductionPlanV2AutoSave).toHaveBeenCalledWith('demo', 'plan');
  });

  it('enters compose directly when the plan has no bound spoken media', () => {
    const onCompose = vi.fn();
    render(<ProductionPlanV2Editor id="plan" project="demo" data={store.nodes[0].data} onClose={() => undefined} onCompose={onCompose} />);

    fireEvent.click(screen.getByRole('button', { name: '进入视频合成' }));

    expect(onCompose).toHaveBeenCalledOnce();
  });

  it('edits a migrated graphic template and emphasis in the saved plan', () => {
    const current = structuredClone(draft);
    current.placements = [{ id: 'card', kind: 'graphic', role: 'effect', track_id: 'graphic:effect',
      graphic: { template: 'question_card', text: '为什么少一杯？', emphasis: '少一杯' },
      start: { kind: 'program_frame', frame: 0 }, end: { kind: 'program_frame', frame: 30 } }];
    store.nodes[0].data.productionPlanV2Draft = current;
    render(<ProductionPlanV2Editor id="plan" project="demo" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
    fireEvent.change(screen.getByLabelText('条目 1 图形模板'), { target: { value: 'keyword' } });
    fireEvent.change(screen.getByLabelText('条目 1 重点词'), { target: { value: '为什么' } });
    expect((store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).placements[0].graphic).toMatchObject({
      template: 'keyword', emphasis: '为什么',
    });
    expect(save.scheduleProductionPlanV2AutoSave).toHaveBeenCalledWith('demo', 'plan');
  });

  it('stays in the plan and focuses a spoken turn before opening compose', () => {
    const current = structuredClone(draft);
    current.beats[0].turns[0].speech_placement_id = 'speech';
    current.resources.push({ id: 'voice', node_id: 'voice-node', media_kind: 'audio', media_path: 'media/voice.wav', media_hash: 'a'.repeat(64), duration_us: 4_000_000, has_audio: true });
    current.placements.push({ id: 'speech', kind: 'audio', role: 'speech', track_id: 'audio:speech', resource_id: 'voice', start: { kind: 'program_frame', frame: 0 }, end: { kind: 'program_frame', frame: 100 } });
    store.nodes[0].data.productionPlanV2Draft = current;
    const onCompose = vi.fn();
    render(<ProductionPlanV2Editor id="plan" project="demo" data={store.nodes[0].data} onClose={() => undefined} onCompose={onCompose} />);

    expect(screen.getByText(/1 句台词待校准/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '先校准台词时间' }));

    expect(onCompose).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: '校准 段落 1 台词 1 时间' })).toHaveFocus();
  });

  it('guides an unassigned spoken turn to choose its source first', () => {
    const current = structuredClone(draft);
    current.resources.push({ id: 'voice', node_id: 'voice-node', media_kind: 'audio', media_path: 'media/voice.wav', media_hash: 'a'.repeat(64), duration_us: 4_000_000, has_audio: true });
    current.placements.push({ id: 'speech', kind: 'audio', role: 'speech', track_id: 'audio:speech', resource_id: 'voice', start: { kind: 'program_frame', frame: 0 }, end: { kind: 'program_frame', frame: 100 } });
    store.nodes[0].data.productionPlanV2Draft = current;
    const onCompose = vi.fn();
    render(<ProductionPlanV2Editor id="plan" project="demo" data={store.nodes[0].data} onClose={() => undefined} onCompose={onCompose} />);

    fireEvent.click(screen.getByRole('button', { name: '先校准台词时间' }));

    expect(screen.getByLabelText('段落 1 台词 1 发声素材')).toHaveFocus();
    expect(onCompose).not.toHaveBeenCalled();
  });

  it('offers only audible material active in the current beat and flags an old choice from another beat', async () => {
    const current = structuredClone(draft);
    current.beats.push({ id: 'answer', purpose: '回答', duration_frames: 100,
      turns: [{ id: 'answer-turn', speaker: 'B', text: '黑咖啡', speech_placement_id: 'intro-video' }] });
    current.resources.push(
      { id: 'intro', node_id: 'intro-node', media_kind: 'video', media_path: 'media/intro.mp4', media_hash: 'a'.repeat(64), duration_us: 4_000_000, has_audio: true },
      { id: 'answer-video', node_id: 'answer-node', media_kind: 'video', media_path: 'media/answer.mp4', media_hash: 'b'.repeat(64), duration_us: 4_000_000, has_audio: true },
      { id: 'crossing', node_id: 'crossing-node', media_kind: 'video', media_path: 'media/crossing.mp4', media_hash: 'c'.repeat(64), duration_us: 4_000_000, has_audio: true },
    );
    current.placements.push(
      { id: 'intro-video', kind: 'visual', role: 'primary', track_id: 'visual:primary', resource_id: 'intro', description: '开场视频', start: { kind: 'program_frame', frame: 0 }, end: { kind: 'program_frame', frame: 100 } },
      { id: 'answer-video', kind: 'visual', role: 'primary', track_id: 'visual:primary', resource_id: 'answer-video', description: '回答视频', start: { kind: 'program_frame', frame: 100 }, end: { kind: 'program_frame', frame: 200 } },
      { id: 'crossing-video', kind: 'visual', role: 'overlay', track_id: 'visual:overlay', resource_id: 'crossing', description: '跨段素材', start: { kind: 'program_frame', frame: 90 }, end: { kind: 'program_frame', frame: 110 } },
    );
    store.nodes[0].data.productionPlanV2Draft = current;
    render(<ProductionPlanV2Editor id="plan" project="demo" canvasId="canvas" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
    fireEvent.click(screen.getByRole('button', { name: '台词主线' }));

    const source = screen.getByLabelText('段落 2 台词 1 发声素材') as HTMLSelectElement;
    expect([...source.options].map((option) => option.value)).toEqual(['', '__none__', 'answer-video', 'crossing-video']);
    expect(source.value).toBe('');
    expect(screen.getByText('原选素材不在本段，请重新选择本段发声素材。')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '校准 段落 2 台词 1 时间' })).toBeDisabled();
    fireEvent.change(source, { target: { value: 'answer-video' } });
    await waitFor(() => expect((store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).beats[1].turns[0].speech_placement_id).toBe('answer-video'));
  });

  it('explains when the current beat has only silent visuals and lets the user clear a wrong source', async () => {
    const current = structuredClone(draft);
    current.beats.push({ id: 'answer', purpose: '回答', duration_frames: 100,
      turns: [{ id: 'answer-turn', speaker: 'B', text: '黑咖啡', speech_placement_id: 'intro-video' }] });
    current.resources.push(
      { id: 'intro', node_id: 'intro-node', media_kind: 'video', media_path: 'media/intro.mp4', media_hash: 'a'.repeat(64), duration_us: 4_000_000, has_audio: true },
      { id: 'silent', node_id: 'silent-node', media_kind: 'video', media_path: 'media/silent.mp4', media_hash: 'b'.repeat(64), duration_us: 4_000_000, has_audio: false },
    );
    current.placements.push(
      { id: 'intro-video', kind: 'visual', role: 'primary', track_id: 'visual:primary', resource_id: 'intro', start: { kind: 'program_frame', frame: 0 }, end: { kind: 'program_frame', frame: 100 } },
      { id: 'silent-video', kind: 'visual', role: 'primary', track_id: 'visual:primary', resource_id: 'silent', start: { kind: 'program_frame', frame: 100 }, end: { kind: 'program_frame', frame: 200 } },
    );
    store.nodes[0].data.productionPlanV2Draft = current;
    render(<ProductionPlanV2Editor id="plan" project="demo" canvasId="canvas" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
    fireEvent.click(screen.getByRole('button', { name: '台词主线' }));

    const source = screen.getByLabelText('段落 2 台词 1 发声素材') as HTMLSelectElement;
    expect([...source.options].map((option) => option.value)).toEqual(['', '__none__']);
    expect(screen.getByText(/本段画面没有音轨/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '清除 段落 2 台词 1 跨段绑定' }));
    await waitFor(() => expect((store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).beats[1].turns[0].speech_placement_id).toBeNull());
  });

  it('opens compose to repair a source range that cannot be calibrated in the plan', () => {
    const current = structuredClone(draft);
    current.beats[0].turns[0].speech_placement_id = 'speech';
    current.resources.push({ id: 'voice', node_id: 'voice-node', media_kind: 'audio', media_path: 'media/short.wav', media_hash: 'a'.repeat(64), duration_us: 1_000_000, has_audio: true });
    current.placements.push({ id: 'speech', kind: 'audio', role: 'speech', track_id: 'audio:speech', resource_id: 'voice', start: { kind: 'program_frame', frame: 0 }, end: { kind: 'program_frame', frame: 100 } });
    store.nodes[0].data.productionPlanV2Draft = current;
    const onCompose = vi.fn();
    render(<ProductionPlanV2Editor id="plan" project="demo" data={store.nodes[0].data} onClose={() => undefined} onCompose={onCompose} />);

    fireEvent.click(screen.getByRole('button', { name: '进入视频合成调整发声范围' }));

    expect(onCompose).toHaveBeenCalledOnce();
  });

  it('waits for an in-flight speech calibration before opening compose', () => {
    const current = structuredClone(draft);
    current.beats[0].turns[0].speech_placement_id = 'speech';
    current.resources.push({ id: 'voice', node_id: 'voice-node', media_kind: 'audio', media_path: 'media/voice.wav', media_hash: 'a'.repeat(64), duration_us: 4_000_000, has_audio: true });
    current.placements.push({ id: 'speech', kind: 'audio', role: 'speech', track_id: 'audio:speech', resource_id: 'voice', start: { kind: 'program_frame', frame: 0 }, end: { kind: 'program_frame', frame: 100 } });
    store.nodes[0].data.productionPlanV2Draft = current;
    store.nodes[0].data.productionSpeechOperations = { turn: { placementId: 'speech', mediaHash: 'a'.repeat(64), text: '你好', sourceInUs: 0, sourceOutUs: 4_000_000 } };
    api.fetchRemixOperation.mockReturnValue(new Promise(() => undefined));
    const onCompose = vi.fn();
    render(<ProductionPlanV2Editor id="plan" project="demo" data={store.nodes[0].data} onClose={() => undefined} onCompose={onCompose} />);

    expect(screen.getByRole('button', { name: '校准中，请稍候' })).toBeDisabled();
    expect(onCompose).not.toHaveBeenCalled();
  });

  it('clears old word timing when a spoken source is unbound', async () => {
    const current = structuredClone(draft);
    current.beats[0].turns[0].speech_placement_id = 'speech';
    current.beats[0].turns[0].speech_timing = { placement_id: 'speech', media_hash: 'a'.repeat(64), text: '你好', text_hash: 'b'.repeat(64),
      source_in_us: 0, source_out_us: 4_000_000, evidence_ref: { artifact_id: 'words', revision: 1 },
      words: [{ id: 'timed-word', text: '你好', start_char: 0, end_char: 2, start_us: 100_000, end_us: 500_000 }] };
    current.resources.push({ id: 'voice', node_id: 'voice-node', media_kind: 'audio', media_path: 'media/voice.wav', media_hash: 'a'.repeat(64), duration_us: 4_000_000, has_audio: true });
    current.placements.push({ id: 'speech', kind: 'audio', role: 'speech', track_id: 'audio:speech', resource_id: 'voice', start: { kind: 'program_frame', frame: 0 }, end: { kind: 'program_frame', frame: 100 } });
    store.nodes[0].data.productionPlanV2Draft = current;
    render(<ProductionPlanV2Editor id="plan" project="demo" canvasId="canvas" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
    fireEvent.click(screen.getByRole('button', { name: '台词主线' }));

    fireEvent.change(screen.getByLabelText('段落 1 台词 1 发声素材'), { target: { value: '__none__' } });

    await waitFor(() => expect(screen.getByLabelText('段落 1 台词 1 发声素材')).toHaveValue('__none__'));
    expect([...((screen.getByLabelText('段落 1 台词 1 发声素材') as HTMLSelectElement).options)].map((option) => option.textContent)).toContain('无发声');
    expect(native.executeProductionCanvasCommand).toHaveBeenCalledWith(expect.objectContaining({ command: expect.objectContaining({
      kind: 'edit_turn', payload: { speech_placement_id: null, invalidate_speech_timing: true },
    }) }), 'demo', 'canvas');
    expect((store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).beats[0].turns[0].speech_timing).toBeNull();
  });

  it('keeps a saved speech review while a source change is pending or fails', async () => {
    const current = structuredClone(draft);
    current.beats[0].turns[0].speech_placement_id = 'visual';
    current.resources.push({ id: 'video', node_id: 'video-node', media_kind: 'video', media_path: 'media/video.mp4',
      media_hash: 'a'.repeat(64), duration_us: 4_000_000, has_audio: true });
    current.placements[0].resource_id = 'video';
    current.placements[0].end = { kind: 'program_frame', frame: 100 };
    const review = { status: 'script_mismatch', artifactRef: { artifactId: 'review', revision: 1 }, placementId: 'visual',
      mediaHash: 'a'.repeat(64), text: '你好', sourceInUs: 0, sourceOutUs: 4_000_000, message: '识别结果需要复核' };
    store.nodes[0].data = { productionPlanV2Draft: current, productionPlanV2Ref: { artifactId: 'plan-artifact', revision: 1 },
      productionSpeechReviews: { turn: review } };
    let rejectEdit: (reason: Error) => void = () => undefined;
    native.executeProductionCanvasCommand.mockReturnValue(new Promise((_, reject) => { rejectEdit = reject; }));
    render(<ProductionPlanV2Editor id="plan" project="demo" canvasId="canvas" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
    fireEvent.click(screen.getByRole('button', { name: '台词主线' }));
    expect(screen.getByText('识别结果需要复核')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('段落 1 台词 1 发声素材'), { target: { value: '__none__' } });
    expect(store.nodes[0].data.productionSpeechReviews).toEqual({ turn: review });
    await act(async () => { rejectEdit(new Error('保存失败')); });
    await waitFor(() => expect(screen.getByText('保存失败')).toBeInTheDocument());
    expect(store.nodes[0].data.productionSpeechReviews).toEqual({ turn: review });
    native.executeProductionCanvasCommand.mockResolvedValue({ nodeId: 'plan', output: { plan_ref: { artifact_id: 'plan-artifact', revision: 2 }, changed_ids: [], invalidated: [], blockers: [] } });
    fireEvent.change(screen.getByLabelText('段落 1 台词 1 发声素材'), { target: { value: '__none__' } });
    await waitFor(() => expect(native.executeProductionCanvasCommand).toHaveBeenCalledTimes(2));
    expect(store.nodes[0].data.productionSpeechReviews).toEqual({ turn: review });
    native.executeProductionCanvasCommand.mockImplementation(async () => {
      const next = structuredClone(store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2);
      next.beats[0].turns[0].speech_placement_id = null;
      store.updateNodeData('plan', { productionPlanV2Draft: next, productionPlanV2Ref: { artifactId: 'plan-artifact', revision: 2 } });
      return { nodeId: 'plan', output: { plan_ref: { artifact_id: 'plan-artifact', revision: 2 }, changed_ids: ['turn'], invalidated: [], blockers: [] } };
    });
    fireEvent.change(screen.getByLabelText('段落 1 台词 1 发声素材'), { target: { value: '__none__' } });
    await waitFor(() => expect((store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).beats[0].turns[0].speech_placement_id).toBeNull());
    expect(store.nodes[0].data.productionSpeechReviews).toEqual({});
  });

  it('aligns a turn against bound speech media and autosaves source word evidence', async () => {
    const current = structuredClone(draft);
    current.resources.push({ id: 'voice', node_id: 'voice-node', media_kind: 'audio', media_path: 'media/voice.wav', media_hash: 'a'.repeat(64), duration_us: 4_000_000, has_audio: true });
    current.placements.push({ id: 'speech', kind: 'audio', role: 'speech', track_id: 'audio:speech', resource_id: 'voice', start: { kind: 'program_frame', frame: 0 }, end: { kind: 'program_frame', frame: 100 } });
    store.nodes[0].data = { productionPlanV2Draft: current, productionPlanV2Ref: { artifactId: 'plan-artifact', revision: 1 }, productionPlanV2Dirty: true };
    save.flushProductionPlanV2AutoSave.mockResolvedValue({ artifactId: 'plan-artifact', revision: 2 });
    api.submitRemixProcessAction.mockResolvedValue({ operation_id: 'operation' });
    api.fetchRemixOperation.mockResolvedValue({ status: 'completed', output_refs: [{ artifact_id: 'words', revision: 1 }], input_refs: [{ artifact_id: 'plan-artifact', revision: 2 }] });
    api.fetchRemixArtifact.mockResolvedValue({ schema: 'word_timeline.v1', id: 'words', revision: 1, payload: { status: 'completed', plan_ref: { artifact_id: 'plan-artifact', revision: 2 }, turn_id: 'turn', placement_id: 'speech', media_hash: `sha256:${'a'.repeat(64)}`, text_hash: 'b'.repeat(64), source_in_us: 0, source_out_us: 4_000_000, words: [{ text: '你好', start_char: 0, end_char: 2, start_us: 100_000, end_us: 500_000 }] }, upstream_refs: [{ artifact_id: 'plan-artifact', revision: 2 }] });
    const onCompose = vi.fn();
    render(<ProductionPlanV2Editor id="plan" project="demo" canvasId="canvas" data={store.nodes[0].data} onClose={() => undefined} onCompose={onCompose} />);
    fireEvent.click(screen.getByRole('button', { name: '台词主线' }));
    fireEvent.change(screen.getByLabelText('段落 1 台词 1 发声素材'), { target: { value: 'speech' } });
    await waitFor(() => expect((store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).beats[0].turns[0].speech_placement_id).toBe('speech'));
    fireEvent.click(screen.getByRole('button', { name: '校准 段落 1 台词 1 时间' }));
    await waitFor(() => expect(api.submitRemixProcessAction).toHaveBeenCalledWith('demo', expect.objectContaining({ action: 'align_production_speech', mediaPath: 'media/voice.wav', turnId: 'turn', takeId: 'take:beat:speech' })));
    await waitFor(() => expect((store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).beats[0].turns[0].speech_timing?.words[0]).toMatchObject({ text: '你好', start_char: 0, end_char: 2, start_us: 100_000, end_us: 500_000 }));
    expect(screen.getByText(/源片 0\.100–0\.500 秒/)).toBeInTheDocument();
    fireEvent.click(screen.getByText(/查看逐词时间/));
    expect(screen.getAllByText('你好').length).toBeGreaterThan(0);
    expect(screen.getByText('0.100–0.500 秒')).toBeInTheDocument();
    expect(save.scheduleProductionPlanV2AutoSave).toHaveBeenCalledWith('demo', 'plan');
    expect(screen.getByRole('button', { name: '进入视频合成' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: '进入视频合成' }));
    expect(onCompose).toHaveBeenCalledOnce();
  });

  it('keeps an accepted erhua variant visible for human review', async () => {
    const current = structuredClone(draft);
    current.beats[0].turns[0].text = '问你个事儿';
    current.beats[0].turns[0].speech_placement_id = 'speech';
    current.resources.push({ id: 'voice', node_id: 'voice-node', media_kind: 'audio', media_path: 'media/voice.wav', media_hash: 'a'.repeat(64), duration_us: 4_000_000, has_audio: true });
    current.placements.push({ id: 'speech', kind: 'audio', role: 'speech', track_id: 'audio:speech', resource_id: 'voice', start: { kind: 'program_frame', frame: 0 }, end: { kind: 'program_frame', frame: 100 } });
    store.nodes[0].data = { productionPlanV2Draft: current, productionPlanV2Ref: { artifactId: 'plan-artifact', revision: 1 }, productionPlanV2Dirty: true };
    save.flushProductionPlanV2AutoSave.mockResolvedValue({ artifactId: 'plan-artifact', revision: 2 });
    api.submitRemixProcessAction.mockResolvedValue({ operation_id: 'erhua-operation' });
    api.fetchRemixOperation.mockResolvedValue({ status: 'completed', output_refs: [{ artifact_id: 'erhua-words', revision: 1 }] });
    api.fetchRemixArtifact.mockResolvedValue({ schema: 'word_timeline.v1', id: 'erhua-words', revision: 1,
      payload: { status: 'completed', turn_id: 'turn', placement_id: 'speech', media_hash: `sha256:${'a'.repeat(64)}`,
        text_hash: 'b'.repeat(64), source_in_us: 0, source_out_us: 4_000_000,
        words: [{ text: '问你个事儿', start_char: 0, end_char: 5, start_us: 100_000, end_us: 800_000 }],
        match_report: { omissions: [{ script_range: { text: '儿' } }] } },
      upstream_refs: [{ artifact_id: 'plan-artifact', revision: 2 }] });
    render(<ProductionPlanV2Editor id="plan" project="demo" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
    fireEvent.click(screen.getByRole('button', { name: '台词主线' }));

    fireEvent.click(screen.getByRole('button', { name: '校准 段落 1 台词 1 时间' }));

    await waitFor(() => expect((store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).beats[0].turns[0].speech_timing?.words[0].text).toBe('问你个事儿'));
    expect(screen.getByText(/儿化.*请试听确认/)).toBeInTheDocument();
  });

  it('keeps a no-speech result reviewable without requiring word timing', async () => {
    const current = structuredClone(draft);
    current.beats[0].turns[0].speech_placement_id = 'speech';
    current.resources.push({ id: 'voice', node_id: 'voice-node', media_kind: 'audio', media_path: 'media/quiet.wav', media_hash: 'a'.repeat(64), duration_us: 4_000_000, has_audio: true });
    current.placements.push({ id: 'speech', kind: 'audio', role: 'speech', track_id: 'audio:speech', resource_id: 'voice', start: { kind: 'program_frame', frame: 0 }, end: { kind: 'program_frame', frame: 100 } });
    store.nodes[0].data = { productionPlanV2Draft: current, productionPlanV2Ref: { artifactId: 'plan-artifact', revision: 1 }, productionPlanV2Dirty: true };
    save.flushProductionPlanV2AutoSave.mockResolvedValue({ artifactId: 'plan-artifact', revision: 2 });
    api.submitRemixProcessAction.mockResolvedValue({ operation_id: 'quiet-operation' });
    api.fetchRemixOperation.mockResolvedValue({ status: 'completed', output_refs: [{ artifact_id: 'quiet-words', revision: 1 }] });
    api.fetchRemixArtifact.mockResolvedValue({ schema: 'word_timeline.v1', id: 'quiet-words', revision: 1, payload: { status: 'no_speech', turn_id: 'turn', placement_id: 'speech', media_hash: `sha256:${'a'.repeat(64)}`, source_in_us: 0, source_out_us: 4_000_000, words: [] }, upstream_refs: [{ artifact_id: 'plan-artifact', revision: 2 }] });
    const editor = render(<ProductionPlanV2Editor id="plan" project="demo" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
    fireEvent.click(screen.getByRole('button', { name: '台词主线' }));
    fireEvent.click(screen.getByRole('button', { name: '校准 段落 1 台词 1 时间' }));
    await waitFor(() => expect(screen.getByText(/没有识别到语音/)).toBeInTheDocument());
    expect((store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).beats[0].turns[0].speech_timing).toBeNull();
    expect(screen.getByRole('button', { name: '进入视频合成' })).toBeEnabled();
    editor.unmount();
    render(<ProductionPlanV2Editor id="plan" project="demo" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
    fireEvent.click(screen.getByRole('button', { name: '台词主线' }));
    expect(screen.getByText(/没有识别到语音/)).toBeInTheDocument();
  });

  it('shows the missing spoken words and unused source tail for a clipped alignment', async () => {
    const current = structuredClone(draft);
    current.beats[0].turns[0].text = '你好，再见';
    current.beats[0].turns[0].speech_placement_id = 'speech';
    current.resources.push({ id: 'voice', node_id: 'voice-node', media_kind: 'audio', media_path: 'media/voice.wav', media_hash: 'a'.repeat(64), duration_us: 5_000_000, has_audio: true });
    current.placements.push({ id: 'speech', kind: 'audio', role: 'speech', track_id: 'audio:speech', resource_id: 'voice', start: { kind: 'program_frame', frame: 0 }, end: { kind: 'program_frame', frame: 100 } });
    store.nodes[0].data = { productionPlanV2Draft: current, productionPlanV2Ref: { artifactId: 'plan-artifact', revision: 1 }, productionPlanV2Dirty: true };
    save.flushProductionPlanV2AutoSave.mockResolvedValue({ artifactId: 'plan-artifact', revision: 2 });
    api.submitRemixProcessAction.mockResolvedValue({ operation_id: 'clipped-operation' });
    api.fetchRemixOperation.mockResolvedValue({ status: 'completed', output_refs: [{ artifact_id: 'clipped-words', revision: 1 }] });
    api.fetchRemixArtifact.mockResolvedValue({ schema: 'word_timeline.v1', id: 'clipped-words', revision: 1,
      payload: { status: 'script_mismatch', turn_id: 'turn', placement_id: 'speech', media_hash: `sha256:${'a'.repeat(64)}`,
        source_in_us: 0, source_out_us: 5_000_000, words: [], mismatch: { report: { normalized_asr: '你好',
          omissions: [{ script_range: { text: '再见' } }] } } }, upstream_refs: [{ artifact_id: 'plan-artifact', revision: 2 }] });
    render(<ProductionPlanV2Editor id="plan" project="demo" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
    fireEvent.click(screen.getByRole('button', { name: '台词主线' }));

    fireEvent.click(screen.getByRole('button', { name: '校准 段落 1 台词 1 时间' }));

    await waitFor(() => expect(screen.getByText(/识别缺少：再见/)).toBeInTheDocument());
    expect((store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).beats[0].turns[0].speech_timing).toBeNull();
  });

  it('explains when the complete recording matches but speech exceeds the planned segment', async () => {
    const current = structuredClone(draft);
    current.beats[0].turns[0].text = '黑咖啡，没有之一。';
    current.beats[0].turns[0].speech_placement_id = 'speech';
    current.resources.push({ id: 'voice', node_id: 'voice-node', media_kind: 'video', media_path: 'media/answer.mp4', media_hash: 'a'.repeat(64), duration_us: 5_000_000, has_audio: true });
    current.placements.push({ id: 'speech', kind: 'visual', role: 'primary', track_id: 'visual:primary', resource_id: 'voice', start: { kind: 'program_frame', frame: 0 }, end: { kind: 'program_frame', frame: 100 } });
    store.nodes[0].data = { productionPlanV2Draft: current, productionPlanV2Ref: { artifactId: 'plan-artifact', revision: 1 }, productionPlanV2Dirty: true };
    save.flushProductionPlanV2AutoSave.mockResolvedValue({ artifactId: 'plan-artifact', revision: 2 });
    api.submitRemixProcessAction.mockResolvedValue({ operation_id: 'full-operation' });
    api.fetchRemixOperation.mockResolvedValue({ status: 'completed', output_refs: [{ artifact_id: 'full-words', revision: 1 }] });
    api.fetchRemixArtifact.mockResolvedValue({ schema: 'word_timeline.v1', id: 'full-words', revision: 1,
      payload: { status: 'completed', turn_id: 'turn', placement_id: 'speech', media_hash: `sha256:${'a'.repeat(64)}`,
        text_hash: 'b'.repeat(64), source_in_us: 0, source_out_us: 5_000_000,
        words: [{ text: '黑咖啡', start_char: 0, end_char: 3, start_us: 1_000_000, end_us: 2_000_000 },
          { text: '没有之一', start_char: 4, end_char: 8, start_us: 4_000_000, end_us: 4_500_000 }] },
      upstream_refs: [{ artifact_id: 'plan-artifact', revision: 2 }] });
    render(<ProductionPlanV2Editor id="plan" project="demo" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
    fireEvent.click(screen.getByRole('button', { name: '台词主线' }));
    fireEvent.click(screen.getByRole('button', { name: '校准 段落 1 台词 1 时间' }));
    await waitFor(() => expect((store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).speech_cues?.[0].source_end_us).toBe(4_500_000));
    expect((store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).beats[0].turns[0].speech_timing?.words).toHaveLength(2);
    expect(screen.queryByText(/录音和台词不一致/)).not.toBeInTheDocument();
  });

  it('calibrates two speakers in one main video with one task and separate word ranges', async () => {
    const current = structuredClone(draft);
    current.beats[0].duration_frames = 150;
    current.beats[0].turns = [
      { id: 'guest', speaker: '嘉宾', text: '我一天三杯。', speech_placement_id: 'picture' },
      { id: 'host', speaker: '主持人', text: '行，下一题。', speech_placement_id: 'picture' },
    ];
    current.resources.push({ id: 'video', node_id: 'video-node', media_kind: 'video', media_path: 'media/interview.mp4', media_hash: 'a'.repeat(64), duration_us: 6_000_000, has_audio: true });
    current.placements = [{ id: 'picture', kind: 'visual', role: 'primary', track_id: 'visual:primary', resource_id: 'video',
      start: { kind: 'beat', ref_id: 'beat', edge: 'start' }, end: { kind: 'beat', ref_id: 'beat', edge: 'end' } }];
    store.nodes[0].data = { productionPlanV2Draft: current, productionPlanV2Ref: { artifactId: 'plan-artifact', revision: 1 }, productionPlanV2Dirty: true };
    save.flushProductionPlanV2AutoSave.mockResolvedValue({ artifactId: 'plan-artifact', revision: 2 });
    api.submitRemixProcessAction.mockResolvedValue({ operation_id: 'shared-operation' });
    api.fetchRemixOperation.mockResolvedValue({ status: 'completed', output_refs: [{ artifact_id: 'guest-words', revision: 1 }] });
    const linked = [{ artifact_id: 'plan-artifact', revision: 2 }];
    api.fetchRemixArtifact.mockImplementation(async (_project: string, ref: { artifactId: string }) => ref.artifactId === 'guest-words'
      ? { schema: 'word_timeline.v1', id: 'guest-words', revision: 1, upstream_refs: linked,
          payload: { status: 'completed', turn_id: 'guest', group_turn_ids: ['guest', 'host'],
            group_refs: [{ turn_id: 'host', artifact_id: 'host-words', revision: 1 }], placement_id: 'picture',
            media_hash: `sha256:${'a'.repeat(64)}`, text_hash: 'b'.repeat(64), transcript: '我一天三杯。',
            source_in_us: 0, source_out_us: 6_000_000,
            words: [{ text: '我一天三杯', start_char: 0, end_char: 5, start_us: 500_000, end_us: 2_400_000 }] } }
      : { schema: 'word_timeline.v1', id: 'host-words', revision: 1, upstream_refs: linked,
          payload: { status: 'completed', turn_id: 'host', placement_id: 'picture',
            media_hash: `sha256:${'a'.repeat(64)}`, text_hash: 'c'.repeat(64), transcript: '行，下一题。',
            source_in_us: 0, source_out_us: 6_000_000,
            words: [{ text: '行，下一题', start_char: 0, end_char: 5, start_us: 3_000_000, end_us: 4_500_000 }] } });
    render(<ProductionPlanV2Editor id="plan" project="demo" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
    fireEvent.click(screen.getByRole('button', { name: '台词主线' }));

    fireEvent.click(screen.getByRole('button', { name: '校准 段落 1 同源 2 句时间' }));

    await waitFor(() => expect((store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).beats[0].turns
      .map((turn) => turn.speech_timing?.words[0]?.start_us)).toEqual([500_000, 3_000_000]));
    expect(api.submitRemixProcessAction).toHaveBeenCalledTimes(1);
    expect(api.submitRemixProcessAction).toHaveBeenCalledWith('demo', expect.objectContaining({ turnId: 'guest', placementId: 'picture' }));
    expect(screen.queryByRole('button', { name: '校准 段落 1 台词 2 时间' })).not.toBeInTheDocument();
  });
});

it('replaces a full ASR cue with imported split rows after evidence is saved', async () => {
 const current = structuredClone(draft); current.beats[0].turns[0].text = '你好世界';
 current.resources = [{ id: 'r', node_id: 'v', media_kind: 'video', media_path: 'v.mp4', media_hash: 'hash', duration_us: 4000000, has_audio: true }];
 current.speech_takes = [{ id: 'take', beat_id: 'beat', resource_id: 'r', turn_ids: ['turn'], origin_placement_id: 'visual', source_in_us: 0, source_out_us: null, speed_num: 1, speed_den: 1, lead_in_frames: 0 }];
 current.speech_cues = [{ id: 'old', turn_id: 'turn', take_id: 'take', text: '你好世界', source_start_us: 0, source_end_us: 2000000, timing_source: 'asr', timing_status: 'ready', word_ids: [], evidence_ref: { artifact_id: 'e', revision: 1 } }];
 store.nodes[0].data = { productionPlanV2Draft: current, productionPlanV2Ref: { artifactId: 'plan-artifact', revision: 1 }, productionPlanV2Dirty: true };
 save.flushProductionPlanV2AutoSave.mockResolvedValue({ artifactId: 'plan-artifact', revision: 1 }); api.createRemixArtifact.mockResolvedValue({ id: 'import', revision: 1 });
 render(<ProductionPlanV2Editor id="plan" project="demo" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
 fireEvent.click(screen.getByRole('button', { name: '台词主线' })); fireEvent.click(screen.getByRole('button', { name: '导入素材 SRT' }));
 fireEvent.change(screen.getByLabelText('素材 SRT 内容'), { target: { value: '1\n00:00:00,000 --> 00:00:01,000\n你好\n\n2\n00:00:01,000 --> 00:00:02,000\n世界' } });
 fireEvent.click(screen.getByRole('button', { name: '预览并匹配台词' }));
 for (const i of [1, 2]) fireEvent.change(screen.getByLabelText(`SRT ${i} 对应台词`), { target: { value: 'turn' } });
 fireEvent.click(screen.getByRole('button', { name: '确认导入并保存证据' }));
 await waitFor(() => expect((store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).speech_cues?.map((cue) => cue.text)).toEqual(['你好', '世界']));
 expect(api.createRemixArtifact).toHaveBeenCalled();
});

it('checks edited recognition against the plan in one click without manual SRT mapping', async () => {
 vi.clearAllMocks();
 const current = structuredClone(draft);
 current.beats[0].turns[0].speech_placement_id = 'speech-visual';
 current.resources = [{ id: 'r', node_id: 'v', media_kind: 'video', media_path: 'v.mp4', media_hash: 'a'.repeat(64), duration_us: 4_000_000, has_audio: true }];
 current.placements.push({ id: 'speech-visual', kind: 'visual', role: 'primary', track_id: 'visual:primary', resource_id: 'r', start: { kind: 'beat', ref_id: 'beat', edge: 'start' }, end: { kind: 'beat', ref_id: 'beat', edge: 'end' } });
 current.speech_takes = [{ id: 'take', beat_id: 'beat', resource_id: 'r', turn_ids: ['turn'], origin_placement_id: 'speech-visual', source_in_us: 0, source_out_us: null, speed_num: 1, speed_den: 1, lead_in_frames: 0 }];
 const recognitionRef = { artifactId: 'raw-asr', revision: 1 };
 store.nodes[0].data = { productionPlanV2Draft: current, productionPlanV2Ref: { artifactId: 'plan-artifact', revision: 1 },
  productionSpeechReviews: { turn: { status: 'script_mismatch', artifactRef: recognitionRef, placementId: 'speech-visual', mediaHash: 'a'.repeat(64), text: '你好', sourceInUs: 0, sourceOutUs: 4_000_000, message: '识别到：你号。' } } };
 api.fetchRemixArtifact.mockResolvedValue({ schema: 'word_timeline.v1', id: 'raw-asr', revision: 1, upstream_refs: [{ artifact_id: 'plan-artifact', revision: 1 }],
  payload: { status: 'script_mismatch', media_hash: `sha256:${'a'.repeat(64)}`, placement_id: 'speech-visual', source_in_us: 0, source_out_us: 4_000_000,
   recognized_transcript: '你号', recognized_words: [{ text: '你号', start_us: 500_000, end_us: 1_500_000 }] } });
 save.flushProductionPlanV2AutoSave.mockResolvedValue({ artifactId: 'plan-artifact', revision: 1 });
 api.createRemixArtifact.mockResolvedValue({ id: 'corrected', revision: 1 });
 render(<ProductionPlanV2Editor id="plan" project="demo" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
 fireEvent.click(screen.getByRole('button', { name: '台词主线' }));
 fireEvent.click(screen.getByRole('button', { name: '校正识别词' }));
 await waitFor(() => expect(screen.getByLabelText('识别词 1 文字')).toHaveValue('你号'));
 expect(screen.queryByLabelText('素材 SRT 内容')).toBeNull();
 expect(screen.queryByRole('button', { name: '预览并匹配台词' })).toBeNull();
 fireEvent.change(screen.getByLabelText('识别词 1 文字'), { target: { value: '你' } });
 fireEvent.click(screen.getByRole('button', { name: '核对并保存台词时间' }));
 await waitFor(() => expect(screen.getByText(/还缺少台词文字/)).toBeInTheDocument());
 expect(api.createRemixArtifact).not.toHaveBeenCalled();
 fireEvent.change(screen.getByLabelText('识别词 1 文字'), { target: { value: '你好' } });
 fireEvent.click(screen.getByRole('button', { name: '核对并保存台词时间' }));
 await waitFor(() => expect((store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).speech_cues?.[0]).toMatchObject({ text: '你好', timing_status: 'ready' }));
 expect(api.createRemixArtifact).toHaveBeenCalledWith('demo', 'speech_cue_evidence.v1', expect.objectContaining({ recognition_ref: { artifact_id: 'raw-asr', revision: 1 }, corrected_words: [{ text: '你好', start_us: 500_000, end_us: 1_500_000 }] }), expect.arrayContaining([{ artifactId: 'raw-asr', revision: 1 }]));
 const accepted = store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2;
 expect(accepted.speech_cues?.[0].word_ids).toEqual(accepted.beats[0].turns[0].speech_timing?.words.map((word) => word.id));
 expect(accepted.beats[0].turns[0].speech_timing?.words[0]).toMatchObject({ text: '你好', start_us: 500_000, end_us: 1_500_000 });
});

it('preflights sentence-only SRT imports before creating evidence for referenced word anchors', async () => {
 vi.clearAllMocks();
 const current = structuredClone(draft);
 current.resources = [{ id: 'r', node_id: 'v', media_kind: 'video', media_path: 'v.mp4', media_hash: 'a'.repeat(64), duration_us: 4_000_000, has_audio: true }];
 current.speech_takes = [{ id: 'take', beat_id: 'beat', resource_id: 'r', turn_ids: ['turn'], origin_placement_id: 'visual', source_in_us: 0, source_out_us: null, speed_num: 1, speed_den: 1, lead_in_frames: 0 }];
 current.speech_cues = [{ id: 'old', turn_id: 'turn', take_id: 'take', text: '你好', source_start_us: 500_000, source_end_us: 1_500_000, timing_source: 'asr', timing_status: 'ready', word_ids: ['keyword'], evidence_ref: { artifact_id: 'e', revision: 1 } }];
 current.beats[0].turns[0].speech_timing = { placement_id: 'visual', media_hash: 'a'.repeat(64), text: '你好', text_hash: 'hash', evidence_ref: { artifact_id: 'e', revision: 1 }, words: [{ id: 'keyword', text: '你好', start_us: 500_000, end_us: 1_500_000 }] };
 current.placements.push({ id: 'word-caption', kind: 'subtitle', role: 'dialogue', track_id: 'subtitle', text: '你好', start: { kind: 'word', ref_id: 'keyword', edge: 'start' }, end: { kind: 'word', ref_id: 'keyword', edge: 'end' } });
 store.nodes[0].data = { productionPlanV2Draft: current, productionPlanV2Ref: { artifactId: 'plan-artifact', revision: 1 }, productionPlanV2Dirty: false };
 save.flushProductionPlanV2AutoSave.mockResolvedValue({ artifactId: 'plan-artifact', revision: 1 });
 render(<ProductionPlanV2Editor id="plan" project="demo" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
 fireEvent.click(screen.getByRole('button', { name: '台词主线' }));
 fireEvent.click(screen.getByRole('button', { name: '导入素材 SRT' }));
 fireEvent.change(screen.getByLabelText('素材 SRT 内容'), { target: { value: '1\n00:00:00,500 --> 00:00:01,500\n你好' } });
 fireEvent.click(screen.getByRole('button', { name: '预览并匹配台词' }));
 fireEvent.change(screen.getByLabelText('SRT 1 对应台词'), { target: { value: 'turn' } });
 fireEvent.click(screen.getByRole('button', { name: '确认导入并保存证据' }));
 await waitFor(() => expect(screen.getByText(/已有词锚点需要先调整/)).toBeInTheDocument());
 expect(api.createRemixArtifact).not.toHaveBeenCalled();
 expect((store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).placements.find((row) => row.id === 'word-caption')?.start.kind).toBe('word');
});

it('edits recognized word text and time before saving sentence SRT with word evidence', async () => {
 vi.clearAllMocks();
 const current = structuredClone(draft);
 current.beats[0].turns[0].speech_placement_id = 'speech-visual';
 current.resources = [{ id: 'r', node_id: 'v', media_kind: 'video', media_path: 'v.mp4', media_hash: 'a'.repeat(64), duration_us: 4_000_000, has_audio: true }];
 current.placements.push({ id: 'speech-visual', kind: 'visual', role: 'primary', track_id: 'visual:primary', resource_id: 'r', start: { kind: 'beat', ref_id: 'beat', edge: 'start' }, end: { kind: 'beat', ref_id: 'beat', edge: 'end' } });
 current.speech_takes = [{ id: 'take', beat_id: 'beat', resource_id: 'r', turn_ids: ['turn'], origin_placement_id: 'speech-visual', source_in_us: 0, source_out_us: null, speed_num: 1, speed_den: 1, lead_in_frames: 0 }];
 store.nodes[0].data = { productionPlanV2Draft: current, productionPlanV2Ref: { artifactId: 'plan-artifact', revision: 1 },
  productionSpeechReviews: { turn: { status: 'script_mismatch', artifactRef: { artifactId: 'raw-asr', revision: 1 }, placementId: 'speech-visual', mediaHash: 'a'.repeat(64), text: '你好', sourceInUs: 0, sourceOutUs: 4_000_000, message: '识别到：你号。' } } };
 api.fetchRemixArtifact.mockResolvedValue({ schema: 'word_timeline.v1', id: 'raw-asr', revision: 1, payload: { status: 'script_mismatch', media_hash: `sha256:${'a'.repeat(64)}`, placement_id: 'speech-visual', source_in_us: 0, source_out_us: 4_000_000,
  recognized_transcript: '你号', recognized_words: [{ text: '你', start_us: 500_000, end_us: 900_000, start_char: 0, end_char: 1 }, { text: '号', start_us: 900_000, end_us: 1_500_000, start_char: 1, end_char: 2 }] } });
 save.flushProductionPlanV2AutoSave.mockResolvedValue({ artifactId: 'plan-artifact', revision: 1 });
 api.createRemixArtifact.mockResolvedValue({ id: 'corrected', revision: 1 });
 render(<ProductionPlanV2Editor id="plan" project="demo" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
 fireEvent.click(screen.getByRole('button', { name: '台词主线' }));
 fireEvent.click(screen.getByRole('button', { name: '校正识别词' }));
 await waitFor(() => expect(screen.getByLabelText('识别词 2 文字')).toHaveValue('号'));
 fireEvent.change(screen.getByLabelText('识别词 2 文字'), { target: { value: '好' } });
 fireEvent.change(screen.getByLabelText('识别词 2 结束秒'), { target: { value: '1.2' } });
 expect(screen.queryByLabelText('素材 SRT 内容')).toBeNull();
 fireEvent.click(screen.getByRole('button', { name: '核对并保存台词时间' }));
 await waitFor(() => expect(api.createRemixArtifact).toHaveBeenCalledWith('demo', 'speech_cue_evidence.v1', expect.objectContaining({ corrected_words: expect.arrayContaining([{ text: '好', start_us: 900_000, end_us: 1_200_000 }]) }), expect.any(Array)));
 expect((store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).speech_cues?.[0]).toMatchObject({ source_start_us: 500_000, source_end_us: 1_200_000 });
});

it('offers source word anchors after a take display is removed', () => {
 const current = structuredClone(draft); current.beats[0].turns[0].words = [];
 current.beats[0].turns[0].speech_placement_id = 'removed';
 current.beats[0].turns[0].speech_timing = { placement_id: 'removed', media_hash: 'hash', text: '你好', text_hash: 'text', evidence_ref: { artifact_id: 'e', revision: 1 }, words: [{ id: 'real', text: '真实词', start_us: 100, end_us: 200 }] };
 current.resources = [{ id: 'r', node_id: 'v', media_kind: 'video', media_path: 'v.mp4', media_hash: 'hash', duration_us: 4000000, has_audio: true }];
 current.speech_takes = [{ id: 'take', beat_id: 'beat', resource_id: 'r', turn_ids: ['turn'], origin_placement_id: 'removed', source_in_us: 0, source_out_us: null, speed_num: 1, speed_den: 1, lead_in_frames: 0 }];
 store.nodes[0].data = { productionPlanV2Draft: current, productionPlanV2Ref: { artifactId: 'plan-artifact', revision: 1 }, productionPlanV2Dirty: true };
 render(<ProductionPlanV2Editor id="plan" project="demo" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
 fireEvent.click(screen.getByRole('button', { name: '添加命名范围' }));
 expect(screen.getAllByRole('option', { name: '词「真实词」开始' }).length).toBeGreaterThan(0);
});

it('autosaves legacy speech migration once and labels unverifiable words as old evidence', async () => {
 const current = structuredClone(draft); current.beats[0].turns[0].speech_placement_id = 'legacy-voice';
 current.resources = [{ id: 'r', node_id: 'v', media_kind: 'video', media_path: 'v.mp4', media_hash: 'hash', duration_us: 4096000, has_audio: true }];
 current.placements.push({ id: 'legacy-voice', kind: 'visual', role: 'primary', track_id: 'visual:primary', resource_id: 'r', start: { kind: 'beat', ref_id: 'beat', edge: 'start' }, end: { kind: 'beat', ref_id: 'beat', edge: 'end' } });
 current.beats[0].turns[0].speech_timing = { placement_id: 'legacy-voice', media_hash: 'hash', text: '你好', text_hash: 'old', source_in_us: 0, source_out_us: 4000000, evidence_ref: { artifact_id: 'e', revision: 1 }, words: [{ id: 'w', text: '你好', start_us: 100000, end_us: 1000000 }] };
 store.nodes[0].data = { productionPlanV2Draft: current, productionPlanV2Ref: { artifactId: 'plan-artifact', revision: 1 }, productionPlanV2Dirty: true };
 save.scheduleProductionPlanV2AutoSave.mockClear();
 const editor = render(<ProductionPlanV2Editor id="plan" project="demo" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
 await waitFor(() => expect((store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).speech_cues?.[0].timing_status).toBe('draft'));
 fireEvent.click(screen.getByRole('button', { name: '台词主线' }));
 expect(screen.getByText('旧识别 1 词 · 需完整校准')).toBeInTheDocument();
 expect(save.scheduleProductionPlanV2AutoSave).toHaveBeenCalledTimes(1);
 editor.unmount();
 render(<ProductionPlanV2Editor id="plan" project="demo" data={store.nodes[0].data} onClose={() => undefined} onCompose={() => undefined} />);
 await Promise.resolve();
 expect(save.scheduleProductionPlanV2AutoSave).toHaveBeenCalledTimes(1);
 expect((store.nodes[0].data.productionPlanV2Draft as ProductionPlanV2).speech_takes).toHaveLength(1);
});
