import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const save = vi.hoisted(() => ({ flushProductionPlanV2AutoSave: vi.fn(), scheduleProductionPlanV2AutoSave: vi.fn() }));
const native = vi.hoisted(() => ({ bindProductionCanvasCandidate: vi.fn() }));
const store = {
  nodes: [] as Array<{ id: string; type: string; data: Record<string, unknown> }>,
  edges: [] as Array<{ source: string; target: string }>,
  updateNodeData: vi.fn((id: string, patch: Record<string, unknown>) => {
    const node = store.nodes.find((item) => item.id === id);
    if (node) node.data = { ...node.data, ...patch };
  }),
  setSelectedNode: vi.fn(), requestFocusNode: vi.fn(), deleteNodes: vi.fn(),
  addNode: vi.fn(() => 'compose'), addEdge: vi.fn(() => 'edge'),
  findNodePosition: vi.fn(() => ({ x: 500, y: 0 })),
};
vi.mock('@/stores/canvasStore', () => ({ useCanvasStore: Object.assign((selector: (state: typeof store) => unknown) => selector(store), { getState: () => store }) }));
vi.mock('@/features/canvas/remix/productionPlanV2AutoSave', () => save);
vi.mock('@/features/canvas/remix/productionCanvasCommand', () => native);
vi.mock('@/features/canvas/nodes/ProductionPlanV2Editor', () => ({ ProductionPlanV2Editor: ({ data, onClose, onCompose }: { data: Record<string, unknown>; onClose: () => void; onCompose: () => void }) => <div role="dialog" aria-label="编辑作品方案"><span>{String((data.productionPlanV2Draft as { goal?: string } | undefined)?.goal ?? '')}</span><button onClick={onClose}>关闭</button><button onClick={onCompose}>进入视频合成</button></div> }));
vi.mock('@xyflow/react', () => ({ Handle: ({ type, id }: { type: string; id: string }) => <span data-testid={`${type}-handle`} data-handle-id={id} />, Position: { Left: 'left', Right: 'right' } }));
vi.mock('@/features/canvas/ui/NodeHeader', () => ({ NodeHeader: () => null, NODE_HEADER_FLOATING_POSITION_CLASS: '' }));
vi.mock('@/lib/url-params', () => ({ readUrl: () => ({ project: 'demo', canvas: 'canvas' }) }));

import { ProductionPlanNode } from './ProductionPlanNode';

const draft = { schema_version: 2, goal: '黑咖啡访谈短片', output: { width: 1080, height: 1920, fps_num: 30, fps_den: 1 },
  beats: [], resources: [], placements: [], provenance: {} };
const props = { id: 'plan', type: 'scriptNode', zIndex: 0, draggable: true, dragging: false,
  selected: false, selectable: true, deletable: true, isConnectable: true, positionAbsoluteX: 0, positionAbsoluteY: 0,
  data: { remixRole: 'production_plan', productionPlanV2Draft: draft } } as const;

describe('ProductionPlanNode', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    store.nodes = [{ id: 'plan', type: 'scriptNode', data: { remixRole: 'production_plan', productionPlanV2Draft: draft } }];
    store.edges = [];
    save.flushProductionPlanV2AutoSave.mockResolvedValue({ artifactId: 'saved-plan', revision: 1 });
  });

  it('opens the one editable plan from the canvas node', () => {
    render(<ProductionPlanNode {...props} />);
    expect(screen.getByTestId('target-handle')).toHaveAttribute('data-handle-id', 'target');
    expect(screen.getByTestId('source-handle')).toHaveAttribute('data-handle-id', 'source');
    fireEvent.click(screen.getByRole('button', { name: '编辑作品方案' }));
    expect(screen.getByRole('dialog', { name: '编辑作品方案' })).toHaveTextContent('黑咖啡访谈短片');
  });

  it('creates a linked composition node from the saved editable plan', async () => {
    render(<ProductionPlanNode {...props} />);
    fireEvent.click(screen.getByRole('button', { name: '编辑作品方案' }));
    fireEvent.click(screen.getByRole('button', { name: '进入视频合成' }));
    await waitFor(() => expect(store.addNode).toHaveBeenCalledWith('videoComposeNode', { x: 500, y: 0 }, expect.objectContaining({
      productionPlanNodeId: 'plan', productionPlanV2Ref: { artifactId: 'saved-plan', revision: 1 },
    })));
    expect(store.addEdge).toHaveBeenCalledWith('plan', 'compose');
  });

  it('binds a generated material through the native command and clears only its pending request', async () => {
    const pendingDraft = { ...draft, placements: [{ id: 'visual', kind: 'visual' }] };
    const planData = { remixRole: 'production_plan', productionPlanV2Draft: pendingDraft,
      productionPlanV2PendingBindings: { visual: 'clip' } };
    store.nodes = [{ id: 'plan', type: 'scriptNode', data: planData },
      { id: 'clip', type: 'videoNode', data: { videoUrl: '/static/projects/demo/media/clip.mp4', remixNodeRevision: 2 } }];
    native.bindProductionCanvasCandidate.mockResolvedValue({ nodeId: 'plan', output: { plan_ref: { artifact_id: 'plan', revision: 2 } } });
    render(<ProductionPlanNode {...props} data={planData as unknown as typeof props.data} />);
    await waitFor(() => expect(native.bindProductionCanvasCandidate).toHaveBeenCalledWith('demo', 'canvas', 'plan', 'visual', 'clip', expect.any(Function)));
    await waitFor(() => expect(store.nodes[0].data.productionPlanV2PendingBindings).toEqual({}));
    expect(store.updateNodeData).not.toHaveBeenCalledWith('plan', expect.objectContaining({ productionPlanV2Dirty: true }));
    expect(store.addEdge).not.toHaveBeenCalled();
  });
});
