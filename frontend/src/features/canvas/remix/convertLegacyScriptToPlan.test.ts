import { beforeEach, describe, expect, it, vi } from 'vitest';

const addNode = vi.fn((_type?: unknown, _position?: unknown, _data?: unknown) => 'plan-new');
const addEdge = vi.fn(() => 'edge-new');
const updateNodeData = vi.fn();
const deleteNodes = vi.fn();
const state = {
  nodes: [] as Array<{ id: string; type: string; data: Record<string, unknown> }>,
  edges: [] as Array<{ source: string; target: string }>,
  addNode, addEdge, updateNodeData, deleteNodes,
  findNodePosition: vi.fn(() => ({ x: 520, y: 380 })),
  setSelectedNode: vi.fn(), requestFocusNode: vi.fn(),
};
vi.mock('@/stores/canvasStore', () => ({ useCanvasStore: { getState: () => state } }));
import { convertLegacyScriptToPlan } from './createProductionPlan';

describe('convertLegacyScriptToPlan', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.nodes = [
      { id: 'story-1', type: 'videoStoryNode', data: { referenceBlueprintRef: { artifactId: 'blueprint', revision: 2 } } },
      { id: 'script-1', type: 'scriptNode', data: { remixRole: 'adaptation_script', remixTopic: '黑咖啡', remixScriptDraft: { dialogue: '女：喝什么？\n男：黑咖啡。' } } },
    ];
    state.edges = [{ source: 'story-1', target: 'script-1' }];
  });

  it('creates a separate plan with exact legacy turns and leaves the script untouched', () => {
    expect(convertLegacyScriptToPlan('script-1')).toBe('plan-new');
    const data = addNode.mock.calls[0][2] as Record<string, unknown>;
    expect(data.remixRole).toBe('production_plan');
    expect(data.productionPlanV2Draft).toMatchObject({ schema_version: 2, goal: '黑咖啡', beats: [{ turns: [
      { speaker: '女', text: '喝什么？' }, { speaker: '男', text: '黑咖啡。' },
    ] }] });
    expect(data.productionPlanLockedTurns).toEqual([
      { speaker: '女', text: '喝什么？' }, { speaker: '男', text: '黑咖啡。' },
    ]);
    expect(addEdge).toHaveBeenCalledWith('story-1', 'plan-new');
    expect(deleteNodes).not.toHaveBeenCalled();
    expect(updateNodeData).not.toHaveBeenCalledWith('script-1', expect.anything());
  });

  it('converts a legacy script into another independent plan', () => {
    state.nodes.push({ id: 'plan-existing', type: 'scriptNode', data: { remixRole: 'production_plan' } });
    state.edges.push({ source: 'story-1', target: 'plan-existing' });
    expect(convertLegacyScriptToPlan('script-1')).toBe('plan-new');
    expect(addNode).toHaveBeenCalledOnce();
    expect(addEdge).toHaveBeenCalledWith('story-1', 'plan-new');
    expect(updateNodeData).not.toHaveBeenCalledWith('plan-existing', expect.anything());
  });
});
