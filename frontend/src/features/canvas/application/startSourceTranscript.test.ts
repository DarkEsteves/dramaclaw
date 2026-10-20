import { beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({
  nodes: [{ id: 'audio-1', type: 'audioNode', data: { audioUrl: '/static/projects/demo/freezone/_uploads/voice.wav' } }] as Array<{ id: string; type: string; data: Record<string, unknown> }>,
  edges: [] as Array<{ source: string; target: string }>,
  addNode: vi.fn((_type: string, _position: unknown, data: Record<string, unknown> = {}) => { state.nodes.push({ id: 'transcript-1', type: 'transcriptNode', data }); return 'transcript-1'; }),
  addEdge: vi.fn((source: string, target: string): string | null => { state.edges.push({ source, target }); return 'edge-1'; }),
  deleteNode: vi.fn((id: string) => { state.nodes.splice(state.nodes.findIndex((item) => item.id === id), 1); }),
  findNodePosition: vi.fn(() => ({ x: 480, y: 220 })),
  updateNodeData: vi.fn((id: string, patch: Record<string, unknown>) => { const node = state.nodes.find((item) => item.id === id); if (node) node.data = { ...node.data, ...patch }; }),
}));
vi.mock('@/stores/canvasStore', () => ({ useCanvasStore: { getState: () => state } }));
vi.mock('@/lib/url-params', () => ({ readUrl: () => ({ project: 'demo' }) }));
const attachRemixMedia = vi.hoisted(() => vi.fn());
vi.mock('@/api/remix', () => ({ attachRemixMedia }));

import { isSourceTranscriptPreparing, startSourceTranscript } from './startSourceTranscript';

describe('startSourceTranscript', () => {
  beforeEach(() => {
    state.nodes.splice(0, state.nodes.length, { id: 'audio-1', type: 'audioNode', data: { audioUrl: '/static/projects/demo/freezone/_uploads/voice.wav' } });
    state.edges.splice(0);
    vi.clearAllMocks();
    state.addEdge.mockImplementation((source: string, target: string) => { state.edges.push({ source, target }); return 'edge-1'; });
    attachRemixMedia.mockResolvedValue({ media_path: 'freezone/_uploads/voice.wav', media_hash: 'hash', media_kind: 'audio', has_audio: true, url: '/voice.wav' });
  });

  it('attaches the current audio, creates a connected transcript and requests automatic transcription', async () => {
    await expect(startSourceTranscript('audio-1')).resolves.toBe('transcript-1');
    expect(attachRemixMedia).toHaveBeenCalledWith('demo', 'freezone/_uploads/voice.wav');
    expect(state.nodes[0].data.remixMedia).toMatchObject({ media_path: 'freezone/_uploads/voice.wav' });
    expect(state.addNode).toHaveBeenCalledWith('transcriptNode', { x: 480, y: 220 });
    expect(state.edges).toContainEqual({ source: 'audio-1', target: 'transcript-1' });
    expect(state.nodes[1].data.autoStartMode).toBe('transcribe');
    expect(state.nodes[1].data.transcriptLanguage).toBe('auto');
  });

  it('can create the transcript without starting a second job when analysis coordinates it', async () => {
    await startSourceTranscript('audio-1', { autoStart: false });
    expect(state.nodes[1].data.autoStartMode).toBeNull();
  });

  it('creates a fresh transcript when the toolbar is used after a completed result', async () => {
    state.nodes[0].data.remixMedia = { media_path: 'freezone/_uploads/voice.wav', media_hash: 'hash', media_kind: 'audio', has_audio: true };
    state.nodes.push({ id: 'transcript-old', type: 'transcriptNode', data: { words: [{ text: '完成' }], status: 'completed', sourceMediaPath: 'freezone/_uploads/voice.wav', sourceMediaHash: 'hash' } });
    state.edges.push({ source: 'audio-1', target: 'transcript-old' });
    state.addNode.mockImplementationOnce((_type: string, _position: unknown, data: Record<string, unknown> = {}) => { state.nodes.push({ id: 'transcript-new', type: 'transcriptNode', data }); return 'transcript-new'; });
    await expect(startSourceTranscript('audio-1')).resolves.toBe('transcript-new');
    expect(state.edges).toContainEqual({ source: 'audio-1', target: 'transcript-new' });
    expect(attachRemixMedia).not.toHaveBeenCalled();
  });

  it('reuses a matching transcript when reference analysis explicitly requests reuse', async () => {
    state.nodes[0].data.remixMedia = { media_path: 'freezone/_uploads/voice.wav', media_hash: 'hash', media_kind: 'audio', has_audio: true };
    state.nodes.push({ id: 'transcript-1', type: 'transcriptNode', data: { words: [{ text: '完成' }], status: 'completed', sourceMediaPath: 'freezone/_uploads/voice.wav', sourceMediaHash: 'hash' } });
    state.edges.push({ source: 'audio-1', target: 'transcript-1' });
    await expect(startSourceTranscript('audio-1', { autoStart: false, reuseExisting: true })).resolves.toBe('transcript-1');
    expect(state.addNode).not.toHaveBeenCalled();
  });

  it('focuses an already running transcript instead of creating a duplicate task', async () => {
    state.nodes[0].data.remixMedia = { media_path: 'freezone/_uploads/voice.wav', media_hash: 'hash', media_kind: 'audio', has_audio: true };
    state.nodes.push({ id: 'transcript-1', type: 'transcriptNode', data: { words: [], autoStartMode: 'transcribe', sourceMediaPath: 'freezone/_uploads/voice.wav', sourceMediaHash: 'hash' } });
    state.edges.push({ source: 'audio-1', target: 'transcript-1' });
    await expect(startSourceTranscript('audio-1')).resolves.toBe('transcript-1');
    expect(state.addNode).not.toHaveBeenCalled();
  });

  it('rejects silent video before creating a child', async () => {
    state.nodes[0] = { id: 'audio-1', type: 'videoNode', data: { videoUrl: '/static/projects/demo/freezone/_uploads/clip.mp4' } };
    attachRemixMedia.mockResolvedValue({ media_path: 'freezone/_uploads/clip.mp4', media_kind: 'video', has_audio: false, url: '/clip.mp4' });
    await expect(startSourceTranscript('audio-1')).rejects.toThrow('没有声音');
    expect(state.addNode).not.toHaveBeenCalled();
  });

  it('refreshes the media reference when the source file has changed', async () => {
    state.nodes[0].data.remixMedia = { media_path: 'freezone/_uploads/old.wav', media_kind: 'audio', has_audio: true };
    await startSourceTranscript('audio-1');
    expect(attachRemixMedia).toHaveBeenCalledWith('demo', 'freezone/_uploads/voice.wav');
    expect(state.nodes[0].data.remixMedia).toMatchObject({ media_path: 'freezone/_uploads/voice.wav' });
  });

  it('makes a new transcript when the source changed after an earlier result', async () => {
    state.nodes.push({ id: 'transcript-old', type: 'transcriptNode', data: { words: [{ text: '旧' }], sourceMediaPath: 'freezone/_uploads/old.wav' } });
    state.edges.push({ source: 'audio-1', target: 'transcript-old' });
    state.addNode.mockImplementationOnce((_type: string, _position: unknown, data: Record<string, unknown> = {}) => { state.nodes.push({ id: 'transcript-new', type: 'transcriptNode', data }); return 'transcript-new'; });
    await expect(startSourceTranscript('audio-1')).resolves.toBe('transcript-new');
    expect(state.edges).toContainEqual({ source: 'audio-1', target: 'transcript-new' });
    expect(state.nodes[2].data.sourceMediaPath).toBe('freezone/_uploads/voice.wav');
  });

  it('rejects an unrelated source URL instead of using stale attached media', async () => {
    state.nodes[0].data.audioUrl = 'https://example.com/other.wav';
    state.nodes[0].data.remixMedia = { media_path: 'freezone/_uploads/voice.wav', media_hash: 'old', media_kind: 'audio', has_audio: true, url: '/voice.wav' };
    await expect(startSourceTranscript('audio-1')).rejects.toThrow('尚未保存到项目');
    expect(state.addNode).not.toHaveBeenCalled();
  });

  it('makes a new transcript when the same path has new media content', async () => {
    state.nodes[0].data.remixMedia = { media_path: 'freezone/_uploads/voice.wav', media_hash: 'new', media_kind: 'audio', has_audio: true };
    state.nodes.push({ id: 'transcript-old', type: 'transcriptNode', data: { words: [{ text: '旧' }], sourceMediaPath: 'freezone/_uploads/voice.wav', sourceMediaHash: 'old' } });
    state.edges.push({ source: 'audio-1', target: 'transcript-old' });
    state.addNode.mockImplementationOnce((_type: string, _position: unknown, data: Record<string, unknown> = {}) => { state.nodes.push({ id: 'transcript-new', type: 'transcriptNode', data }); return 'transcript-new'; });
    await expect(startSourceTranscript('audio-1')).resolves.toBe('transcript-new');
    expect(state.nodes[2].data.sourceMediaHash).toBe('new');
  });

  it('does not start transcription when the graph rejects the edge', async () => {
    state.addEdge.mockReturnValueOnce(null);
    await expect(startSourceTranscript('audio-1')).rejects.toThrow('连接转写节点失败');
    expect(state.nodes).toHaveLength(1);
  });
  it.each([{ autoStart: true, reuseExisting: false }, { autoStart: false, reuseExisting: true }, { autoStart: true, reuseExisting: true }, { autoStart: false, reuseExisting: false }])('exposes exact source preparation and releases it for %o', async options => {
    let release!: (value: unknown) => void;
    attachRemixMedia.mockReturnValueOnce(new Promise(resolve => { release = resolve; }));
    state.nodes[0].id = 'audio:child';
    const pending = startSourceTranscript('audio:child', options);
    expect(isSourceTranscriptPreparing('audio:child')).toBe(true);
    expect(isSourceTranscriptPreparing('audio')).toBe(false);
    release({ media_path: 'freezone/_uploads/voice.wav', media_hash: 'hash', media_kind: 'audio', has_audio: true });
    await pending;
    expect(isSourceTranscriptPreparing('audio:child')).toBe(false);
  });
  it('reuses a synchronously submitting transcript without adding an auto-start behind it', async () => {
    state.nodes[0].data.remixMedia = { media_path: 'freezone/_uploads/voice.wav', media_hash: 'hash', media_kind: 'audio', has_audio: true };
    state.nodes.push({ id: 'transcript-1', type: 'transcriptNode', data: { words: [], sourceMediaPath: 'freezone/_uploads/voice.wav', sourceMediaHash: 'hash', transcriptSubmissionPending: 'actual-submission', autoStartMode: null } });
    state.edges.push({ source: 'audio-1', target: 'transcript-1' });
    await expect(startSourceTranscript('audio-1')).resolves.toBe('transcript-1');
    expect(state.addNode).not.toHaveBeenCalled();
    expect(state.nodes[1].data.autoStartMode).toBeNull();
    expect(state.nodes[1].data.transcriptSubmissionPending).toBe('actual-submission');
  });
});
