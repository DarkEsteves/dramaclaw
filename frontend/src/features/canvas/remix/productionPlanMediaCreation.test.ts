import { describe, expect, it, vi } from 'vitest';
import { emptyProductionPlan } from './productionPlanModel';
import { createPlanMaterialNode } from './productionPlanMediaCreation';
import { CANVAS_NODE_TYPES } from '@/features/canvas/domain/canvasNodes';
import { isUpstreamConnectionAllowed } from '@/features/canvas/domain/nodeRegistry';
import { planCueKey } from './productionPlanReadiness';

function fixture() {
  const plan = emptyProductionPlan();
  plan.beats[0].purpose = '提出问题';
  plan.beats[0].visuals[0].description = '采访者看向镜头';
  const planNode = { id: 'plan-1', data: {} as Record<string, unknown> };
  const store = {
    nodes: [planNode], addNode: vi.fn(() => 'material-1'), addEdge: vi.fn(() => 'edge-1'),
    deleteNodes: vi.fn(), updateNodeData: vi.fn((_id: string, patch: Record<string, unknown>) => {
      planNode.data = { ...planNode.data, ...patch };
    }),
    findNodePosition: vi.fn(() => ({ x: 400, y: 100 })),
    setSelectedNode: vi.fn(), requestFocusNode: vi.fn(),
  };
  return { plan, planNode, store };
}

describe('plan material creation', () => {
  it('permits an audio material to connect to a production plan script node', () => {
    expect(isUpstreamConnectionAllowed(CANVAS_NODE_TYPES.audio, CANVAS_NODE_TYPES.script)).toBe(true);
  });
  it('creates and tracks an image node for the exact visual cue', () => {
    const { plan, planNode, store } = fixture();
    const beat = plan.beats[0];
    expect(createPlanMaterialNode(store, 'plan-1', beat, beat.visuals[0], 'visuals', 'image', 30)).toBe('material-1');
    expect(store.addNode).toHaveBeenCalledWith('imageGenNode', { x: 400, y: 100 }, expect.objectContaining({
      prompt: expect.stringContaining('采访者看向镜头'),
    }));
    expect(store.addEdge).toHaveBeenCalledWith('material-1', 'plan-1');
    expect(planNode.data.productionPlanPendingBindings).toEqual({
      [planCueKey(beat.id, 'visuals', beat.visuals[0].id)]: 'material-1',
    });
    expect(store.requestFocusNode).toHaveBeenCalledWith('material-1');
  });

  it('uses the beat duration and prompt for a video node', () => {
    const { plan, store } = fixture();
    const beat = plan.beats[0];
    beat.duration_frames = 150;
    createPlanMaterialNode(store, 'plan-1', beat, beat.visuals[0], 'visuals', 'video', 30);
    expect(store.addNode).toHaveBeenCalledWith('videoNode', expect.anything(), expect.objectContaining({
      durationSec: 5, aspectRatio: '9:16', generateAudio: false, prompt: expect.stringContaining('采访者看向镜头'),
    }));
  });

  it('prefills a narration audio node from the beat dialogue', () => {
    const { plan, store } = fixture();
    const beat = plan.beats[0];
    beat.turns.push({ speaker: '采访者', text: '你每天喝咖啡吗？' });
    const cue = { id: 'voice', role: 'narration' as const, description: '旁白', media: null };
    createPlanMaterialNode(store, 'plan-1', beat, cue, 'audio', 'audio', 30);
    expect(store.addNode).toHaveBeenCalledWith('audioNode', expect.anything(), expect.objectContaining({
      audioKind: 'speech', text: '你每天喝咖啡吗？',
    }));
  });

  it('does not retain a disconnected node or pending assignment', () => {
    const { plan, planNode, store } = fixture();
    store.addEdge.mockReturnValueOnce(null as never);
    expect(() => createPlanMaterialNode(store, 'plan-1', plan.beats[0], plan.beats[0].visuals[0], 'visuals', 'image', 30))
      .toThrow('无法连接素材节点');
    expect(store.deleteNodes).toHaveBeenCalledWith(['material-1']);
    expect(planNode.data.productionPlanPendingBindings).toBeUndefined();
  });
});
