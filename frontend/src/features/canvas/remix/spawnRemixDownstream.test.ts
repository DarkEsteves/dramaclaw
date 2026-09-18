import { describe, expect, it, vi } from 'vitest';
import type { CanvasEdge, CanvasNode } from '../domain/canvasNodes';
import { createComposeFromTimeline, createTimelineFromScript, planComposeFromTimeline, planTimelineFromScript } from './spawnRemixDownstream';

const graphStore = vi.hoisted(() => ({ getState: vi.fn() }));
vi.mock('@/stores/canvasStore', () => ({ useCanvasStore: graphStore }));

const node = (id: string, type: string, data: Record<string, unknown> = {}) => ({ id, type, data, position: { x: 0, y: 0 } }) as CanvasNode;
const edge = (source: string, target: string) => ({ id: `${source}-${target}`, source, target }) as CanvasEdge;
const ref = (artifactId: string) => ({ artifactId, revision: 1 });

describe('source-first remix graph plans', () => {
  it('connects the saved script and its directly aligned transcript to a new timeline', () => {
    const nodes = [node('script', 'scriptNode', { remixScriptRef: ref('s') }), node('words', 'transcriptNode', { artifactRef: ref('w'), alignedScriptRef: ref('s'), status: 'completed' })];
    expect(planTimelineFromScript(nodes, [edge('script', 'words')], 'script')).toEqual({ sourceIds: ['script', 'words'] });
  });

  it('rejects missing or stale alignment before creating a timeline', () => {
    const nodes = [node('script', 'scriptNode', { remixScriptRef: ref('s') }), node('words', 'transcriptNode', { artifactRef: ref('w'), alignedScriptRef: ref('old'), status: 'completed' })];
    expect(() => planTimelineFromScript(nodes, [edge('script', 'words')], 'script')).toThrow('按当前脚本对齐');
  });

  it('requires the complete reference and narration chain before creating compose', () => {
    const nodes = [
      node('video', 'videoNode', { remixRole: 'reference', remixMedia: { media_hash: 'v', has_audio: true } }),
      node('story', 'videoStoryNode', { referenceBlueprintRef: ref('b') }),
      node('script', 'scriptNode', { remixScriptRef: ref('s') }),
      node('words', 'transcriptNode', { artifactRef: ref('w'), alignedScriptRef: ref('s'), status: 'completed' }),
      node('timeline', 'semanticTimelineNode', { artifactRef: ref('t') }),
    ];
    const edges = [edge('video', 'story'), edge('story', 'script'), edge('video', 'words'), edge('script', 'words'), edge('script', 'timeline'), edge('words', 'timeline')];
    expect(planComposeFromTimeline(nodes, edges, 'timeline')).toEqual({ sourceIds: ['timeline'] });
    expect(() => planComposeFromTimeline(nodes, edges.filter((item) => item.source !== 'video'), 'timeline')).toThrow('参考视频');
    expect(() => planComposeFromTimeline(nodes, edges.filter((item) => item.target !== 'story'), 'timeline')).toThrow('参考视频');
  });

  it('creates and connects one timeline with an automatic compile request', () => {
    const addNode = vi.fn(() => 'timeline');
    const addEdge = vi.fn(() => 'edge');
    graphStore.getState.mockReturnValue({
      nodes: [node('script', 'scriptNode', { remixScriptRef: ref('s') }), node('words', 'transcriptNode', { artifactRef: ref('w'), alignedScriptRef: ref('s'), status: 'completed' })],
      edges: [edge('script', 'words')], addNode, addEdge,
      findNodePosition: vi.fn(() => ({ x: 100, y: 0 })), setSelectedNode: vi.fn(), requestFocusNode: vi.fn(),
    });
    expect(createTimelineFromScript('script')).toBe('timeline');
    expect(addNode).toHaveBeenCalledWith('semanticTimelineNode', { x: 100, y: 0 }, { autoCompileRequested: true });
    expect(addEdge.mock.calls).toEqual([['script', 'timeline'], ['words', 'timeline']]);
  });

  it('creates a compose node with an automatic manifest request when its graph is complete', () => {
    const addNode = vi.fn(() => 'compose');
    const addEdge = vi.fn(() => 'edge');
    graphStore.getState.mockReturnValue({
      nodes: [
        node('video', 'videoNode', { remixRole: 'reference', remixMedia: { media_hash: 'v', has_audio: true } }),
        node('story', 'videoStoryNode', { referenceBlueprintRef: ref('b') }),
        node('script', 'scriptNode', { remixScriptRef: ref('s') }),
        node('words', 'transcriptNode', { artifactRef: ref('w'), alignedScriptRef: ref('s'), status: 'completed' }),
        node('timeline', 'semanticTimelineNode', { artifactRef: ref('t') }),
      ],
      edges: [edge('video', 'story'), edge('story', 'script'), edge('video', 'words'), edge('script', 'words'), edge('script', 'timeline'), edge('words', 'timeline')],
      addNode, addEdge, findNodePosition: vi.fn(() => ({ x: 500, y: 0 })), setSelectedNode: vi.fn(), requestFocusNode: vi.fn(),
    });
    expect(createComposeFromTimeline('timeline')).toBe('compose');
    expect(addNode).toHaveBeenCalledWith('videoComposeNode', { x: 500, y: 0 }, { autoPrepareRequested: true });
    expect(addEdge).toHaveBeenCalledWith('timeline', 'compose');
  });

  it('does not create another timeline when a valid connected child already exists', () => {
    const addNode = vi.fn();
    const updateNodeData = vi.fn();
    graphStore.getState.mockReturnValue({
      nodes: [node('script', 'scriptNode', { remixScriptRef: ref('s') }), node('words', 'transcriptNode', { artifactRef: ref('w'), alignedScriptRef: ref('s'), status: 'completed' }), node('timeline', 'semanticTimelineNode')],
      edges: [edge('script', 'words'), edge('script', 'timeline'), edge('words', 'timeline')],
      addNode, updateNodeData, setSelectedNode: vi.fn(), requestFocusNode: vi.fn(),
    });
    expect(createTimelineFromScript('script')).toBe('timeline');
    expect(addNode).not.toHaveBeenCalled();
    expect(updateNodeData).toHaveBeenCalledWith('timeline', { autoCompileRequested: true });
  });

  it('removes a new child when a required edge cannot be added', () => {
    const deleteNodes = vi.fn();
    graphStore.getState.mockReturnValue({
      nodes: [node('script', 'scriptNode', { remixScriptRef: ref('s') }), node('words', 'transcriptNode', { artifactRef: ref('w'), alignedScriptRef: ref('s'), status: 'completed' })],
      edges: [edge('script', 'words')], addNode: vi.fn(() => 'timeline'), addEdge: vi.fn(() => null), deleteNodes,
      findNodePosition: vi.fn(() => ({ x: 100, y: 0 })), setSelectedNode: vi.fn(), requestFocusNode: vi.fn(),
    });
    expect(() => createTimelineFromScript('script')).toThrow('连线失败');
    expect(deleteNodes).toHaveBeenCalledWith(['timeline']);
  });
});
