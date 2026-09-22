import { beforeEach, describe, expect, it, vi } from 'vitest';

const addNode = vi.fn(() => 'script-new');
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

  it('removes an unconnected new script and reports a failed connection', () => {
    addEdge.mockReturnValueOnce(null as unknown as string);
    expect(() => createScriptFromBlueprint('story-1')).toThrow('改编脚本连线失败');
    expect(deleteNodes).toHaveBeenCalledWith(['script-new']);
  });
});
