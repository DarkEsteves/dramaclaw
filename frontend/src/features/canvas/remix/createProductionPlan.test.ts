import { beforeEach, describe, expect, it, vi } from 'vitest';

const addNode = vi.fn(() => 'plan-1');
const addEdge = vi.fn(() => 'edge-1');
const state = {
  nodes: [{ id: 'story-1', data: { referenceBlueprintRef: { artifactId: 'blueprint', revision: 2 } } }],
  addNode, addEdge, deleteNodes: vi.fn(), findNodePosition: vi.fn(() => ({ x: 400, y: 0 })),
  setSelectedNode: vi.fn(), requestFocusNode: vi.fn(),
};
vi.mock('@/stores/canvasStore', () => ({ useCanvasStore: { getState: () => state } }));
import { createProductionPlanFromStory } from './createProductionPlan';

describe('createProductionPlanFromStory', () => {
  beforeEach(() => vi.clearAllMocks());
  it('creates an independent editable plan linked to reference evidence', () => {
    expect(createProductionPlanFromStory('story-1')).toBe('plan-1');
    expect(addNode).toHaveBeenCalledWith('scriptNode', { x: 400, y: 0 }, expect.objectContaining({
      remixRole: 'production_plan', productionPlanBlueprintRef: { artifactId: 'blueprint', revision: 2 },
      productionPlanV2Draft: expect.objectContaining({ schema_version: 2, goal: '', beats: [], resources: [], placements: [] }),
    }));
    expect(addEdge).toHaveBeenCalledWith('story-1', 'plan-1');
  });
});
