import { beforeEach, describe, expect, it, vi } from 'vitest';

const addNode = vi.fn((_type?: unknown, _position?: unknown, _data?: unknown) => 'script-new');
const addEdge = vi.fn(() => 'edge-new');
const updateNodeData = vi.fn();
const setSelectedNode = vi.fn();
const requestFocusNode = vi.fn();
const deleteNodes = vi.fn();
const findNodePosition = vi.fn(() => ({ x: 480, y: 20 }));
const state = { nodes: [] as unknown[], edges: [] as unknown[], addNode, addEdge, updateNodeData, setSelectedNode, requestFocusNode, deleteNodes, findNodePosition };
vi.mock('@/stores/canvasStore', () => ({ useCanvasStore: { getState: () => state } }));

import { createScriptFromBlueprint } from './createScriptFromBlueprint';

describe('createScriptFromBlueprint', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.nodes = [{ id: 'story-1', type: 'videoStoryNode', data: {
      blueprintNarrativeRole: '先提问再展示产品',
      blueprintPreserveRule: '保留问答节奏',
      blueprintReplaceRule: '替换产品画面',
      referenceEvidenceRef: { artifactId: 'evidence-1', revision: 2 },
      referenceBlueprintRef: { artifactId: 'story-blueprint', revision: 3 },
      sourceMediaPath: 'reference.mp4',
    } }];
    state.edges = [];
  });

  it('creates and opens a production plan for a new adaptation', () => {
    expect(createScriptFromBlueprint('story-1')).toBe('script-new');
    expect(addNode).toHaveBeenCalledWith('scriptNode', { x: 480, y: 20 }, expect.objectContaining({
      remixRole: 'production_plan',
      productionPlanOpenOnCreate: true,
      productionPlanBlueprintRef: { artifactId: 'story-blueprint', revision: 3 },
    }));
    expect(addEdge).toHaveBeenCalledWith('story-1', 'script-new');
    expect(setSelectedNode).toHaveBeenCalledWith('script-new');
    expect(requestFocusNode).toHaveBeenCalledWith('script-new');
  });

  it('creates a new plan without opening an existing legacy script', () => {
    state.nodes.push({ id: 'script-existing', type: 'scriptNode', data: { remixRole: 'adaptation_script', remixScriptRef: { artifactId: 'saved-script', revision: 2 } } });
    state.edges = [{ source: 'story-1', target: 'script-existing' }];
    expect(createScriptFromBlueprint('story-1')).toBe('script-new');
    expect(addNode).toHaveBeenCalledOnce();
    expect(addEdge).toHaveBeenCalledWith('story-1', 'script-new');
    expect(updateNodeData).not.toHaveBeenCalledWith('script-existing', expect.anything());
  });

  it('creates another plan when one is already connected', () => {
    state.nodes.push({ id: 'plan-existing', type: 'scriptNode', data: { remixRole: 'production_plan', productionPlanRef: { artifactId: 'plan', revision: 1 } } });
    state.edges = [{ source: 'story-1', target: 'plan-existing' }];
    expect(createScriptFromBlueprint('story-1')).toBe('script-new');
    expect(addNode).toHaveBeenCalledOnce();
    expect(addEdge).toHaveBeenCalledWith('story-1', 'script-new');
    expect(updateNodeData).not.toHaveBeenCalledWith('plan-existing', expect.anything());
  });

  it('creates separate drafts for repeated clicks', () => {
    addNode.mockReturnValueOnce('plan-first').mockReturnValueOnce('plan-second');
    expect(createScriptFromBlueprint('story-1')).toBe('plan-first');
    expect(createScriptFromBlueprint('story-1')).toBe('plan-second');
    expect(addEdge).toHaveBeenNthCalledWith(1, 'story-1', 'plan-first');
    expect(addEdge).toHaveBeenNthCalledWith(2, 'story-1', 'plan-second');
    const first = addNode.mock.calls[0][2] as { productionPlanV2Draft: object };
    const second = addNode.mock.calls[1][2] as { productionPlanV2Draft: object };
    expect(first.productionPlanV2Draft).not.toBe(second.productionPlanV2Draft);
  });

  it('keeps the analyzed story as context for a new production plan', () => {
    state.nodes = [{ id: 'story-1', type: 'videoStoryNode', data: {
      rawResult: { video_story: { summary: '双人访谈中展示产品并以反转收尾' } },
      referenceEvents: [
        { narrative_inference: '开场提问' },
        { narrative_inference: '展示产品' },
        { narrative_inference: '结尾反转' },
      ],
      referenceEvidenceRef: { artifactId: 'evidence-1', revision: 2 },
    } }];

    createScriptFromBlueprint('story-1');

    expect(addNode).toHaveBeenCalledWith('scriptNode', expect.anything(), expect.objectContaining({
      remixRole: 'production_plan', productionPlanSourceStoryId: 'story-1', productionPlanBlueprintRef: null,
    }));
  });

  it('uses the exact saved blueprint reference for the new plan', () => {
    state.nodes = [{ id: 'story-1', type: 'videoStoryNode', data: {
      rawResult: { video_story: { summary: '原片的 AI 摘要' } },
      referenceBlueprintRef: { artifactId: 'saved-blueprint', revision: 2 },
    } }];

    createScriptFromBlueprint('story-1');

    expect(addNode).toHaveBeenCalledWith('scriptNode', expect.anything(), expect.objectContaining({
      productionPlanBlueprintRef: { artifactId: 'saved-blueprint', revision: 2 },
    }));
  });

  it('does not copy legacy inferred rules into the plan', () => {
    state.nodes = [{ id: 'story-1', type: 'videoStoryNode', data: {
      rawResult: { video_story: { summary: '旧剧情摘要' } },
      referenceEvents: [{ narrative_inference: '开场提问' }, { narrative_inference: '结尾反转' }],
      blueprintNarrativeRole: '旧剧情摘要',
      blueprintPreserveRule: '保留原片的叙事顺序与关键转折：开场提问 → 结尾反转。',
      blueprintReplaceRule: '将原片特有的人物、产品、场景和具体说法替换为新主题的真实信息；重新制作对应画面与台词。',
      referenceBlueprintRef: { artifactId: 'saved-blueprint', revision: 2 },
    } }];

    createScriptFromBlueprint('story-1');
    const data = addNode.mock.calls[0]?.[2] as Record<string, unknown>;
    expect(data.productionPlanBlueprintRef).toEqual({ artifactId: 'saved-blueprint', revision: 2 });
    expect(data).not.toHaveProperty('blueprintPreserveRule');
  });

  it('removes an unconnected new plan and reports a failed connection', () => {
    addEdge.mockReturnValueOnce(null as unknown as string);
    expect(() => createScriptFromBlueprint('story-1')).toThrow('作品方案连线失败');
    expect(deleteNodes).toHaveBeenCalledWith(['script-new']);
  });
});
