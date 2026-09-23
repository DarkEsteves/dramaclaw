import { describe, expect, it, vi } from 'vitest';
import type { CanvasEdge, CanvasNode } from '../domain/canvasNodes';
import { applyRemixInvalidation, createComposeFromTimeline, createTimelineFromScript, mutateVisualGenerationRequest, planComposeFromTimeline, planRemixInvalidation, planTimelineFromScript } from './spawnRemixDownstream';

const graphStore = vi.hoisted(() => ({ getState: vi.fn() }));
vi.mock('@/stores/canvasStore', () => ({ useCanvasStore: graphStore }));

const node = (id: string, type: string, data: Record<string, unknown> = {}) => ({ id, type, data, position: { x: 0, y: 0 } }) as CanvasNode;
const edge = (source: string, target: string) => ({ id: `${source}-${target}`, source, target }) as CanvasEdge;
const ref = (artifactId: string) => ({ artifactId, revision: 1 });

describe('source-first remix graph plans', () => {
  it.each([
    ['style', ['render']],
    ['media_binding', ['render']],
    ['visual_prompt', ['generate_visual', 'render']],
    ['script', ['align_script', 'compile_timeline', 'render']],
    ['voice', ['align_script', 'compile_timeline', 'render']],
  ] as const)('invalidates only affected work for %s edits', (change, expected) => {
    expect(planRemixInvalidation(change)).toEqual(expected);
  });
  it('applies script invalidation through the graph without dispatching paid calls', () => {
    const updateNodeData = vi.fn();
    const paidCall = vi.fn();
    graphStore.getState.mockReturnValue({
      nodes: [node('script', 'scriptNode'), node('words', 'transcriptNode', { artifactRef: ref('w') }), node('timeline', 'semanticTimelineNode', { artifactRef: ref('t') }), node('compose', 'videoComposeNode', { resultVideoUrl: '/old.mp4' })],
      edges: [edge('script', 'words'), edge('words', 'timeline'), edge('timeline', 'compose')], updateNodeData, paidCall,
    });
    expect(applyRemixInvalidation('script', 'script')).toEqual(['align_script', 'compile_timeline', 'render']);
    expect(updateNodeData).toHaveBeenCalledWith('words', expect.objectContaining({ artifactRef: null, alignmentStale: true }));
    expect(updateNodeData).toHaveBeenCalledWith('timeline', expect.objectContaining({ artifactRef: null }));
    expect(updateNodeData).toHaveBeenCalledWith('compose', expect.objectContaining({ resultVideoUrl: null }));
    expect(paidCall).not.toHaveBeenCalled();
  });
  it('durably marks the edited visual output stale until a new generation completes', () => {
    const updateNodeData = vi.fn();
    graphStore.getState.mockReturnValue({
      nodes: [node('visual', 'imageGenNode', { prompt: 'new prompt', remixVisualOutputCurrent: true }), node('compose', 'videoComposeNode', { remixInputsCurrent: true })],
      edges: [edge('visual', 'compose')], updateNodeData,
    });
    expect(applyRemixInvalidation('visual', 'visual_prompt')).toEqual(['generate_visual', 'render']);
    expect(updateNodeData).not.toHaveBeenCalledWith('visual', expect.objectContaining({ remixVisualRequestRevision: expect.anything() }));
    expect(updateNodeData).toHaveBeenCalledWith('compose', expect.objectContaining({ remixInputsCurrent: false }));
  });
  it.each([
    [{ prompt: 'translated' }], [{ model: 'new-model', modelParams: {} }],
    [{ styleTemplateId: 'cinematic' }], [{ aspectRatio: '9:16', size: '4K', quality: 'high' }],
    [{ modelParams: { seed: 2 } }], [{ cameraSelection: { lensId: '50mm' } }],
    [{ referenceImageUrl: '/new.png' }],
  ])('centralizes every visual request mutation and clears the obsolete task %#', (patch) => {
    const updateNodeData = vi.fn();
    graphStore.getState.mockReturnValue({
      nodes: [node('visual', 'imageGenNode', { remixVisualRequestRevision: 4, remixVisualOutputCurrent: true }), node('compose', 'videoComposeNode', { remixInputsCurrent: true })],
      edges: [edge('visual', 'compose')], updateNodeData,
    });
    expect(mutateVisualGenerationRequest('visual', patch)).toBe(5);
    expect(updateNodeData).toHaveBeenCalledWith('visual', expect.objectContaining({
      ...patch, remixVisualRequestRevision: 5, remixVisualOutputCurrent: false,
      isGenerating: false, generationStartedAt: null, generationTaskKey: null,
    }));
    expect(updateNodeData).toHaveBeenCalledWith('compose', expect.objectContaining({ remixInputsCurrent: false }));
  });
  it('connects the saved script and its directly aligned transcript to a new timeline', () => {
    const nodes = [node('script', 'scriptNode', { remixScriptRef: ref('s') }), node('words', 'transcriptNode', { artifactRef: ref('w'), alignedScriptRef: ref('s'), status: 'completed' })];
    expect(planTimelineFromScript(nodes, [edge('script', 'words')], 'script')).toEqual({ sourceIds: ['script', 'words'] });
  });

  it('uses each accepted speaking take as a timeline input without a global narration', () => {
    const digest = 'a'.repeat(64);
    const nodes = [node('script', 'scriptNode', { remixScriptRef: ref('s'), remixScriptDraft: { dialogue: '男：你好', segments: [{ id: 'segment-1', source: 'speaking_video', takeNodeId: 'take', takeMediaRevision: digest }] } }),
      node('take', 'videoNode', { videoUrl: '/static/projects/demo/freezone/take.mp4', remixRole: 'performance', remixMedia: { url: '/static/projects/demo/freezone/take.mp4', media_path: 'freezone/take.mp4', media_kind: 'video', media_hash: digest, has_audio: true } })];
    expect(planTimelineFromScript(nodes, [edge('take', 'script')], 'script')).toEqual({ sourceIds: ['script', 'take'] });
  });

  it('refuses a silent speaking take before compiling', () => {
    const nodes = [node('script', 'scriptNode', { remixScriptRef: ref('s'), remixScriptDraft: { segments: [{ id: 'segment-1', source: 'speaking_video', takeNodeId: 'take', takeMediaRevision: 'a'.repeat(64) }] } }),
      node('take', 'videoNode', { videoUrl: '/take.mp4', remixMedia: { url: '/take.mp4', media_kind: 'video', media_hash: 'a'.repeat(64), has_audio: false } })];
    expect(() => planTimelineFromScript(nodes, [edge('take', 'script')], 'script')).toThrow(/声音/);
  });

  it('rejects missing or stale alignment before creating a timeline', () => {
    const nodes = [node('script', 'scriptNode', { remixScriptRef: ref('s') }), node('words', 'transcriptNode', { artifactRef: ref('w'), alignedScriptRef: ref('old'), status: 'completed' })];
    expect(() => planTimelineFromScript(nodes, [edge('script', 'words')], 'script')).toThrow('按当前脚本对齐');
  });

  it('rejects completed words from an older narration file', () => {
    const nodes = [
      node('script', 'scriptNode', { remixScriptRef: ref('s') }),
      node('audio', 'audioNode', { remixRole: 'narration', remixScriptRef: ref('s'), audioUrl: '/new.wav', remixMedia: { media_path: 'new.wav', media_hash: 'new' } }),
      node('words', 'transcriptNode', { artifactRef: ref('w'), alignedScriptRef: ref('s'), status: 'completed', sourceMediaPath: 'old.wav', sourceMediaHash: 'old' }),
    ];
    expect(() => planTimelineFromScript(nodes, [edge('script', 'audio'), edge('audio', 'words'), edge('script', 'words')], 'script')).toThrow('当前配音');
  });

  it('rejects old alignment when audioUrl changes before media is reattached', () => {
    const nodes = [
      node('script', 'scriptNode', { remixScriptRef: ref('s') }),
      node('audio', 'audioNode', { remixRole: 'narration', remixScriptRef: ref('s'), audioUrl: '/new.wav', remixMedia: { url: '/old.wav', media_path: 'old.wav', media_hash: 'old' } }),
      node('words', 'transcriptNode', { artifactRef: ref('w'), alignedScriptRef: ref('s'), status: 'completed', sourceMediaPath: 'old.wav', sourceMediaHash: 'old' }),
    ];
    expect(() => planTimelineFromScript(nodes, [edge('script', 'audio'), edge('audio', 'words'), edge('script', 'words')], 'script')).toThrow('当前配音');
  });

  it('does not reuse completed words while a new narration is awaiting media attachment', () => {
    const nodes = [
      node('script', 'scriptNode', { remixScriptRef: ref('s') }),
      node('audio', 'audioNode', { remixRole: 'narration', remixScriptRef: ref('s'), audioUrl: '/new.wav' }),
      node('words', 'transcriptNode', { artifactRef: ref('w'), alignedScriptRef: ref('s'), status: 'completed', sourceMediaPath: 'old.wav', sourceMediaHash: 'old' }),
    ];
    expect(() => planTimelineFromScript(nodes, [edge('script', 'audio'), edge('audio', 'words'), edge('script', 'words')], 'script')).toThrow('当前配音');
  });

  it('does not fall back to old words when a connected narration has no audio', () => {
    const nodes = [
      node('script', 'scriptNode', { remixScriptRef: ref('s') }),
      node('audio', 'audioNode', { remixRole: 'narration', remixScriptRef: ref('s'), audioUrl: null }),
      node('words', 'transcriptNode', { artifactRef: ref('w'), alignedScriptRef: ref('s'), status: 'completed' }),
    ];
    expect(() => planTimelineFromScript(nodes, [edge('script', 'audio'), edge('audio', 'words'), edge('script', 'words')], 'script')).toThrow('当前配音');
  });

  it('uses the current narration alignment when an older transcript remains connected', () => {
    const nodes = [
      node('script', 'scriptNode', { remixScriptRef: ref('s') }),
      node('audio', 'audioNode', { remixRole: 'narration', remixScriptRef: ref('s'), audioUrl: '/new.wav', remixMedia: { media_path: 'new.wav', media_hash: 'new' } }),
      node('old', 'transcriptNode', { artifactRef: ref('old'), alignedScriptRef: ref('s'), status: 'completed', sourceMediaPath: 'old.wav', sourceMediaHash: 'old' }),
      node('current', 'transcriptNode', { artifactRef: ref('current'), alignedScriptRef: ref('s'), status: 'completed', sourceMediaPath: 'new.wav', sourceMediaHash: 'new' }),
    ];
    const edges = [edge('script', 'audio'), edge('script', 'old'), edge('audio', 'old'), edge('script', 'current'), edge('audio', 'current')];
    expect(planTimelineFromScript(nodes, edges, 'script')).toEqual({ sourceIds: ['script', 'current'] });
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

  it('blocks compose when required timing is unresolved or uses an obsolete narration take', () => {
    const base = [
      node('video', 'videoNode', { remixRole: 'reference', remixMedia: { media_hash: 'v', has_audio: true } }),
      node('story', 'videoStoryNode', { referenceBlueprintRef: ref('b') }),
      node('script', 'scriptNode', { remixScriptRef: ref('s') }),
      node('audio', 'audioNode', { remixRole: 'narration', remixMedia: { media_hash: 'voice-new' }, audioUrl: '/new.wav' }),
      node('words', 'transcriptNode', { artifactRef: ref('w'), alignedScriptRef: ref('s'), status: 'completed', sourceMediaHash: 'voice-new' }),
      node('timeline', 'semanticTimelineNode', { artifactRef: ref('t'), compiledMediaHash: 'voice-old', events: [{ selectionId: 'required', label: 'required', invalidReason: 'selection invalid' }] }),
    ];
    const edges = [edge('video', 'story'), edge('story', 'script'), edge('script', 'audio'), edge('audio', 'words'), edge('script', 'words'), edge('script', 'timeline'), edge('words', 'timeline')];
    expect(() => planComposeFromTimeline(base, edges, 'timeline')).toThrow('未解决');
    const resolved = base.map((item) => item.id === 'timeline' ? node('timeline', 'semanticTimelineNode', { artifactRef: ref('t'), compiledMediaHash: 'voice-old', events: [] }) : item);
    expect(() => planComposeFromTimeline(resolved, edges, 'timeline')).toThrow('配音版本');
  });

  it('requires legacy timing without a narration hash to be recompiled before export', () => {
    const nodes = [
      node('video', 'videoNode', { remixRole: 'reference', remixMedia: { media_hash: 'v', has_audio: true } }),
      node('story', 'videoStoryNode', { referenceBlueprintRef: ref('b') }),
      node('script', 'scriptNode', { remixScriptRef: ref('s') }),
      node('audio', 'audioNode', { remixRole: 'narration', remixMedia: { media_hash: 'voice' }, audioUrl: '/voice.wav' }),
      node('words', 'transcriptNode', { artifactRef: ref('w'), alignedScriptRef: ref('s'), status: 'completed' }),
      node('timeline', 'semanticTimelineNode', { artifactRef: ref('t'), events: [] }),
    ];
    const edges = [edge('video', 'story'), edge('story', 'script'), edge('script', 'audio'), edge('audio', 'words'), edge('script', 'words'), edge('script', 'timeline'), edge('words', 'timeline')];
    expect(() => planComposeFromTimeline(nodes, edges, 'timeline')).toThrow('重新编译');
  });

  it('accepts the same production narration digest with or without the sha256 prefix', () => {
    const digest = 'a'.repeat(64);
    const nodes = [
      node('video', 'videoNode', { remixRole: 'reference', remixMedia: { media_hash: 'v', has_audio: true } }),
      node('story', 'videoStoryNode', { referenceBlueprintRef: ref('b') }),
      node('script', 'scriptNode', { remixScriptRef: ref('s') }),
      node('audio', 'audioNode', { remixRole: 'narration', remixMedia: { media_hash: digest }, audioUrl: '/voice.wav' }),
      node('words', 'transcriptNode', { artifactRef: ref('w'), alignedScriptRef: ref('s'), status: 'completed' }),
      node('timeline', 'semanticTimelineNode', { artifactRef: ref('t'), compiledMediaHash: `sha256:${digest}`, events: [] }),
    ];
    const edges = [edge('video', 'story'), edge('story', 'script'), edge('script', 'audio'), edge('audio', 'words'), edge('script', 'words'), edge('script', 'timeline'), edge('words', 'timeline')];
    expect(planComposeFromTimeline(nodes, edges, 'timeline')).toEqual({ sourceIds: ['timeline'] });
  });

  it('rejects a cleared narration even when its old media and timing remain', () => {
    const digest = 'a'.repeat(64);
    const nodes = [
      node('video', 'videoNode', { remixRole: 'reference', remixMedia: { media_hash: 'v', has_audio: true } }),
      node('story', 'videoStoryNode', { referenceBlueprintRef: ref('b') }),
      node('script', 'scriptNode', { remixScriptRef: ref('s') }),
      node('audio', 'audioNode', { remixRole: 'narration', remixMedia: { media_hash: digest }, audioUrl: null }),
      node('words', 'transcriptNode', { artifactRef: ref('w'), alignedScriptRef: ref('s'), status: 'completed' }),
      node('timeline', 'semanticTimelineNode', { artifactRef: ref('t'), compiledMediaHash: `sha256:${digest}`, events: [] }),
    ];
    const edges = [edge('video', 'story'), edge('story', 'script'), edge('script', 'audio'), edge('video', 'words'), edge('audio', 'words'), edge('script', 'words'), edge('script', 'timeline'), edge('words', 'timeline')];
    expect(() => planComposeFromTimeline(nodes, edges, 'timeline')).toThrow('当前配音没有音频');
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
