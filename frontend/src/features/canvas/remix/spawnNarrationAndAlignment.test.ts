import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { CanvasEdge, CanvasNode } from '../domain/canvasNodes';
import { createAlignedTranscriptFromAudio, createNarrationFromScript } from './spawnNarrationAndAlignment';

const graphStore = vi.hoisted(() => ({ getState: vi.fn() }));
const attachRemixMedia = vi.hoisted(() => vi.fn());
vi.mock('@/stores/canvasStore', () => ({ useCanvasStore: graphStore }));
vi.mock('@/api/remix', () => ({ attachRemixMedia }));
vi.mock('@/lib/url-params', () => ({ readUrl: () => ({ project: 'demo' }) }));

const node = (id: string, type: string, data: Record<string, unknown> = {}) => ({ id, type, data, position: { x: 0, y: 0 } }) as CanvasNode;
const edge = (source: string, target: string) => ({ id: `${source}-${target}`, source, target }) as CanvasEdge;
const scriptRef = { artifactId: 'script-a', revision: 3 };

beforeEach(() => { vi.clearAllMocks(); });

describe('remix narration and alignment continuation', () => {
  it('creates one dialogue-mode audio node without putting role labels in its speakable text', () => {
    const addNode = vi.fn(() => 'audio');
    const addEdge = vi.fn(() => 'edge');
    graphStore.getState.mockReturnValue({
      nodes: [node('script', 'scriptNode', { remixScriptRef: scriptRef, remixScriptDraft: { dialogue: '男：你好？\n女：你好。' } })],
      edges: [], addNode, addEdge, setSelectedNode: vi.fn(), requestFocusNode: vi.fn(),
      findNodePosition: vi.fn(() => ({ x: 300, y: 0 })),
    });
    expect(createNarrationFromScript('script')).toBe('audio');
    expect(addNode).toHaveBeenCalledWith('audioNode', { x: 300, y: 0 }, expect.objectContaining({
      remixDialogueTurns: [{ speaker: '男', text: '你好？' }, { speaker: '女', text: '你好。' }],
      text: '男：你好？\n女：你好。',
    }));
  });

  it('creates one connected narration node prefilled with the exact saved dialogue', () => {
    const addNode = vi.fn(() => 'audio');
    const addEdge = vi.fn(() => 'edge');
    const setSelectedNode = vi.fn();
    const requestFocusNode = vi.fn();
    graphStore.getState.mockReturnValue({
      nodes: [node('script', 'scriptNode', { remixScriptRef: scriptRef, remixScriptDraft: { dialogue: '新的旁白。', selections: [] } })],
      edges: [], addNode, addEdge, setSelectedNode, requestFocusNode,
      findNodePosition: vi.fn(() => ({ x: 300, y: 0 })),
    });
    expect(createNarrationFromScript('script')).toBe('audio');
    expect(addNode).toHaveBeenCalledWith('audioNode', { x: 300, y: 0 }, expect.objectContaining({
      remixRole: 'narration', audioKind: 'speech', text: '新的旁白。', remixScriptRef: scriptRef,
    }));
    expect(addEdge).toHaveBeenCalledWith('script', 'audio');
    expect(setSelectedNode).toHaveBeenCalledWith('audio');
    expect(requestFocusNode).toHaveBeenCalledWith('audio');
  });

  it('reuses a narration for the same script revision', () => {
    const addNode = vi.fn();
    graphStore.getState.mockReturnValue({
      nodes: [node('script', 'scriptNode', { remixScriptRef: scriptRef, remixScriptDraft: { dialogue: '新的旁白。' } }), node('audio', 'audioNode', { remixRole: 'narration', remixScriptRef: scriptRef })],
      edges: [edge('script', 'audio')], addNode, setSelectedNode: vi.fn(), requestFocusNode: vi.fn(),
    });
    expect(createNarrationFromScript('script')).toBe('audio');
    expect(addNode).not.toHaveBeenCalled();
  });

  it('upgrades an empty pre-existing narration node to speaker mode for the same saved script', () => {
    const updateNodeData = vi.fn();
    graphStore.getState.mockReturnValue({
      nodes: [
        node('script', 'scriptNode', { remixScriptRef: scriptRef, remixScriptDraft: { dialogue: '男：你好？\n女：你好。' } }),
        node('audio', 'audioNode', { remixRole: 'narration', remixScriptRef: scriptRef, text: '男：你好？\n女：你好。' }),
      ],
      edges: [edge('script', 'audio')], addNode: vi.fn(), updateNodeData,
      setSelectedNode: vi.fn(), requestFocusNode: vi.fn(),
    });
    expect(createNarrationFromScript('script')).toBe('audio');
    expect(updateNodeData).toHaveBeenCalledWith('audio', expect.objectContaining({
      remixDialogueTurns: [{ speaker: '男', text: '你好？' }, { speaker: '女', text: '你好。' }],
    }));
  });

  it('rejects an unsaved script before creating narration', () => {
    const addNode = vi.fn();
    graphStore.getState.mockReturnValue({ nodes: [node('script', 'scriptNode', { remixScriptDraft: { dialogue: '草稿' } })], edges: [], addNode });
    expect(() => createNarrationFromScript('script')).toThrow('保存改编脚本');
    expect(addNode).not.toHaveBeenCalled();
  });

  it('attaches real project-local audio, creates two edges, and requests alignment against the exact version', async () => {
    const media = { media_path: 'freezone/_uploads/narration.wav', media_hash: 'hash', media_kind: 'audio', has_audio: true, url: '/static/projects/demo/freezone/_uploads/narration.wav' };
    attachRemixMedia.mockResolvedValue(media);
    const addNode = vi.fn(() => 'words');
    const addEdge = vi.fn(() => 'edge');
    const updateNodeData = vi.fn();
    const setSelectedNode = vi.fn();
    const requestFocusNode = vi.fn();
    const audioUrl = '/static/projects/demo/freezone/_uploads/narration.wav';
    const nodes = [node('script', 'scriptNode', { remixScriptRef: scriptRef }), node('audio', 'audioNode', { remixRole: 'narration', remixScriptRef: scriptRef, audioUrl })];
    graphStore.getState.mockReturnValue({ nodes, edges: [edge('script', 'audio')], addNode, addEdge, updateNodeData, setSelectedNode, requestFocusNode, findNodePosition: vi.fn(() => ({ x: 700, y: 0 })) });
    expect(await createAlignedTranscriptFromAudio('audio')).toBe('words');
    expect(attachRemixMedia).toHaveBeenCalledWith('demo', 'freezone/_uploads/narration.wav');
    expect(updateNodeData).toHaveBeenCalledWith('audio', { remixMedia: media });
    expect(addEdge.mock.calls).toEqual([['audio', 'words'], ['script', 'words']]);
    expect(updateNodeData).toHaveBeenCalledWith('words', expect.objectContaining({ autoStartMode: 'align', pendingScriptRef: scriptRef, sourceMediaPath: media.media_path }));
    expect(setSelectedNode).toHaveBeenCalledWith('words');
    expect(requestFocusNode).toHaveBeenCalledWith('words');
  });

  it('refuses to align an audio node from an older script revision', async () => {
    const addNode = vi.fn();
    graphStore.getState.mockReturnValue({
      nodes: [node('script', 'scriptNode', { remixScriptRef: scriptRef }), node('audio', 'audioNode', { remixRole: 'narration', remixScriptRef: { ...scriptRef, revision: 2 }, audioUrl: '/voice.wav' })],
      edges: [edge('script', 'audio')], addNode,
    });
    await expect(createAlignedTranscriptFromAudio('audio')).rejects.toThrow('当前脚本版本');
    expect(addNode).not.toHaveBeenCalled();
  });

  it('never aligns the reference audio without a saved script connection', async () => {
    const addNode = vi.fn();
    graphStore.getState.mockReturnValue({ nodes: [node('reference-audio', 'audioNode', { remixRole: 'narration', audioUrl: '/static/projects/demo/freezone/_uploads/reference.wav' })], edges: [], addNode });
    await expect(createAlignedTranscriptFromAudio('reference-audio')).rejects.toThrow('连接一份改编脚本');
    expect(addNode).not.toHaveBeenCalled();
    expect(attachRemixMedia).not.toHaveBeenCalled();
  });

  it('coalesces rapid alignment clicks while attaching the same audio', async () => {
    let release!: (media: Record<string, unknown>) => void;
    attachRemixMedia.mockReturnValue(new Promise((resolve) => { release = resolve; }));
    const addNode = vi.fn(() => 'words');
    const addEdge = vi.fn(() => 'edge');
    graphStore.getState.mockReturnValue({
      nodes: [node('script', 'scriptNode', { remixScriptRef: scriptRef }), node('audio', 'audioNode', { remixRole: 'narration', remixScriptRef: scriptRef, audioUrl: '/static/projects/demo/freezone/_uploads/narration.wav' })],
      edges: [edge('script', 'audio')], addNode, addEdge, updateNodeData: vi.fn(), setSelectedNode: vi.fn(), requestFocusNode: vi.fn(), findNodePosition: vi.fn(() => ({ x: 700, y: 0 })),
    });
    const first = createAlignedTranscriptFromAudio('audio');
    const second = createAlignedTranscriptFromAudio('audio');
    release({ media_path: 'freezone/_uploads/narration.wav', media_hash: 'hash', media_kind: 'audio', has_audio: true });
    expect(await Promise.all([first, second])).toEqual(['words', 'words']);
    expect(addNode).toHaveBeenCalledTimes(1);
  });
});
