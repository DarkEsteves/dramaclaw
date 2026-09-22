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
    state.nodes = [];
    state.edges = [];
  });

  it('creates and connects a script, then requests its editor', () => {
    expect(createScriptFromBlueprint('story-1')).toBe('script-new');
    expect(addNode).toHaveBeenCalledWith('scriptNode', { x: 480, y: 20 });
    expect(addEdge).toHaveBeenCalledWith('story-1', 'script-new');
    expect(setSelectedNode).toHaveBeenCalledWith('script-new');
    expect(requestFocusNode).toHaveBeenCalledWith('script-new');
    expect(consumeRemixScriptEditorRequest('script-new')).toBe(true);
  });

  it('creates a fresh connected script when an older script already exists', () => {
    state.nodes = [{ id: 'script-existing', type: 'scriptNode', data: {} }];
    state.edges = [{ source: 'story-1', target: 'script-existing' }];
    expect(createScriptFromBlueprint('story-1')).toBe('script-new');
    expect(addNode).toHaveBeenCalledWith('scriptNode', { x: 480, y: 20 });
    expect(addEdge).toHaveBeenCalledWith('story-1', 'script-new');
    expect(consumeRemixScriptEditorRequest('script-new')).toBe(true);
  });

  it('removes an unconnected new script and reports a failed connection', () => {
    addEdge.mockReturnValueOnce(null as unknown as string);
    expect(() => createScriptFromBlueprint('story-1')).toThrow('改编脚本连线失败');
    expect(deleteNodes).toHaveBeenCalledWith(['script-new']);
  });
});
