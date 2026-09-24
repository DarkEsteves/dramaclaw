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
import { consumeRemixScriptEditorRequest } from './remixScriptEditorRequests';

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

  it('creates and connects a script, then requests its editor', () => {
    expect(createScriptFromBlueprint('story-1')).toBe('script-new');
    expect(addNode).toHaveBeenCalledWith('scriptNode', { x: 480, y: 20 }, expect.objectContaining({
      blueprintNarrativeRole: '先提问再展示产品',
      blueprintPreserveRule: '保留问答节奏',
      blueprintReplaceRule: '替换产品画面',
      referenceEvidenceRef: { artifactId: 'evidence-1', revision: 2 },
      remixBlueprintSeedRef: { artifactId: 'story-blueprint', revision: 3 },
      referenceBlueprintRef: null,
      remixBlueprintRef: null,
    }));
    expect(addEdge).toHaveBeenCalledWith('story-1', 'script-new');
    expect(setSelectedNode).toHaveBeenCalledWith('script-new');
    expect(requestFocusNode).toHaveBeenCalledWith('script-new');
    expect(consumeRemixScriptEditorRequest('script-new')).toBe(true);
  });

  it('creates a fresh connected script when an older script already exists', () => {
    state.nodes.push({ id: 'script-existing', type: 'scriptNode', data: {} });
    state.edges = [{ source: 'story-1', target: 'script-existing' }];
    expect(createScriptFromBlueprint('story-1')).toBe('script-new');
    expect(addNode).toHaveBeenCalledWith('scriptNode', { x: 480, y: 20 }, expect.any(Object));
    expect(addEdge).toHaveBeenCalledWith('story-1', 'script-new');
    expect(consumeRemixScriptEditorRequest('script-new')).toBe(true);
  });

  it('copies branch settings into independent child data', () => {
    addNode.mockReturnValueOnce('script-a').mockReturnValueOnce('script-b');
    createScriptFromBlueprint('story-1');
    createScriptFromBlueprint('story-1');
    const first = addNode.mock.calls[0][2] as Record<string, unknown>;
    const second = addNode.mock.calls[1][2] as Record<string, unknown>;

    expect(first).not.toBe(second);
    first.blueprintPreserveRule = '只改第一条分支';
    expect(second.blueprintPreserveRule).toBe('保留问答节奏');
    expect((state.nodes[0] as { data: Record<string, unknown> }).data.blueprintPreserveRule).toBe('保留问答节奏');
  });

  it('prefills a new branch from completed AI analysis when no rules were saved', () => {
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
      blueprintNarrativeRole: '通过提问、揭晓、反转推进故事。',
      blueprintPreserveRule: '保留“提问 → 揭晓 → 反转”的叙事节奏。',
      blueprintReplaceRule: '',
    }));
  });

  it('lets a saved blueprint restore its exact rules before using analysis suggestions', () => {
    state.nodes = [{ id: 'story-1', type: 'videoStoryNode', data: {
      rawResult: { video_story: { summary: '原片的 AI 摘要' } },
      referenceBlueprintRef: { artifactId: 'saved-blueprint', revision: 2 },
    } }];

    createScriptFromBlueprint('story-1');

    expect(addNode).toHaveBeenCalledWith('scriptNode', expect.anything(), expect.objectContaining({
      blueprintNarrativeRole: '',
      remixBlueprintSeedRef: { artifactId: 'saved-blueprint', revision: 2 },
    }));
  });

  it('upgrades exact legacy defaults when branching from an automatically saved blueprint', () => {
    state.nodes = [{ id: 'story-1', type: 'videoStoryNode', data: {
      rawResult: { video_story: { summary: '旧剧情摘要' } },
      referenceEvents: [{ narrative_inference: '开场提问' }, { narrative_inference: '结尾反转' }],
      blueprintNarrativeRole: '旧剧情摘要',
      blueprintPreserveRule: '保留原片的叙事顺序与关键转折：开场提问 → 结尾反转。',
      blueprintReplaceRule: '将原片特有的人物、产品、场景和具体说法替换为新主题的真实信息；重新制作对应画面与台词。',
      referenceBlueprintRef: { artifactId: 'saved-blueprint', revision: 2 },
    } }];

    createScriptFromBlueprint('story-1');
    expect(addNode).toHaveBeenCalledWith('scriptNode', expect.anything(), expect.objectContaining({
      blueprintNarrativeRole: '通过提问、反转推进故事。',
      blueprintPreserveRule: '保留“提问 → 反转”的叙事节奏。',
      blueprintReplaceRule: '',
      remixBlueprintSeedRef: { artifactId: 'saved-blueprint', revision: 2 },
    }));
  });

  it('removes an unconnected new script and reports a failed connection', () => {
    addEdge.mockReturnValueOnce(null as unknown as string);
    expect(() => createScriptFromBlueprint('story-1')).toThrow('改编脚本连线失败');
    expect(deleteNodes).toHaveBeenCalledWith(['script-new']);
  });
});
