import { beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({
  nodes: [{ id: 'video', type: 'videoNode', data: { videoUrl: '/static/projects/demo/freezone/_uploads/clip.mp4' } }] as Array<{ id: string; type: string; data: Record<string, unknown> }>,
  edges: [] as Array<{ source: string; target: string }>,
  addNode: vi.fn((type: string, _position: unknown, data: Record<string, unknown> = {}) => { const id = type === 'videoStoryNode' ? 'story' : 'transcript'; state.nodes.push({ id, type, data }); return id; }),
  addEdge: vi.fn((source: string, target: string) => { state.edges.push({ source, target }); return `${source}-${target}`; }),
  deleteNode: vi.fn(),
  findNodePosition: vi.fn(() => ({ x: 480, y: 220 })),
  updateNodeData: vi.fn((id: string, patch: Record<string, unknown>) => { const node = state.nodes.find((item) => item.id === id); if (node) node.data = { ...node.data, ...patch }; }),
}));
vi.mock('@/stores/canvasStore', () => ({ useCanvasStore: { getState: () => state } }));
vi.mock('@/lib/url-params', () => ({ readUrl: () => ({ project: 'demo' }) }));
const api = vi.hoisted(() => ({
  attachRemixMedia: vi.fn(), submitRemixProcessAction: vi.fn(), fetchRemixOperation: vi.fn(), fetchRemixArtifact: vi.fn(), fetchRemixTaskStatus: vi.fn(),
}));
vi.mock('@/api/remix', () => api);
const startSourceTranscript = vi.hoisted(() => vi.fn());
vi.mock('./startSourceTranscript', () => ({ startSourceTranscript }));
const reanalyzeVideoStory = vi.hoisted(() => vi.fn());
vi.mock('./videoAnalyzeStory', () => ({ reanalyzeVideoStory }));

import { analyzeReferenceVideo } from './analyzeReferenceVideo';

describe('analyzeReferenceVideo', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.nodes.splice(0, state.nodes.length, { id: 'video', type: 'videoNode', data: { videoUrl: '/static/projects/demo/freezone/_uploads/clip.mp4' } });
    state.edges.splice(0);
    api.attachRemixMedia.mockResolvedValue({ media_path: 'freezone/_uploads/clip.mp4', media_hash: 'hash', media_kind: 'video', duration_us: 2_000_000, has_audio: true, url: '/static/projects/demo/freezone/_uploads/clip.mp4' });
    startSourceTranscript.mockImplementation(async () => {
      state.nodes.push({ id: 'transcript', type: 'transcriptNode', data: { words: [] } });
      state.edges.push({ source: 'video', target: 'transcript' });
      return 'transcript';
    });
    api.submitRemixProcessAction.mockImplementation(async (_project, request) => ({ operation_id: request.action }));
    api.fetchRemixOperation.mockImplementation(async (_project, operationId) => ({ status: 'needs_confirmation', output_refs: [{ artifact_id: operationId === 'transcribe_media' ? 'words' : 'evidence', revision: 1 }] }));
    api.fetchRemixArtifact.mockImplementation(async (_project, ref) => ref.artifactId === 'words'
      ? { id: 'words', revision: 1, schema: 'word_timeline.v1', payload: { status: 'completed', transcript: 'hello', words: [{ text: 'hello', start_us: 0, end_us: 500_000 }] } }
      : { id: 'evidence', revision: 1, schema: 'reference_evidence.v1', payload: { media_hash: 'sha256:hash', duration_us: 2_000_000, cuts: [], representative_frames: [] } });
    reanalyzeVideoStory.mockImplementation(() => ({ nodeId: 'story', completion: Promise.resolve() }));
  });

  it('creates connected transcript and story nodes before joint analysis', async () => {
    const result = analyzeReferenceVideo('video', { videoUrl: '/static/projects/demo/freezone/_uploads/clip.mp4' });
    expect(result.nodeId).toBe('story');
    expect(state.findNodePosition).toHaveBeenCalledWith('video', 960, 900);
    await result.completion;

    expect(state.edges).toEqual(expect.arrayContaining([
      { source: 'video', target: 'story' }, { source: 'video', target: 'transcript' }, { source: 'transcript', target: 'story' },
    ]));
    expect(startSourceTranscript).toHaveBeenCalledWith('video', { autoStart: false, reuseExisting: true });
    expect(api.submitRemixProcessAction).toHaveBeenCalledWith('demo', expect.objectContaining({ action: 'analyze_reference_video' }));
    expect(api.submitRemixProcessAction).toHaveBeenCalledWith('demo', expect.objectContaining({ action: 'transcribe_media' }));
    expect(reanalyzeVideoStory).toHaveBeenCalledWith('story', expect.objectContaining({ evidenceRef: { artifactId: 'evidence', revision: 1 }, transcriptRef: { artifactId: 'words', revision: 1 } }));
    expect(state.nodes.find((node) => node.id === 'story')?.data.remixAnalysisStage).toBe('completed');
  });

  it('analyzes a silent video without creating a transcript node', async () => {
    api.attachRemixMedia.mockResolvedValue({ media_path: 'freezone/_uploads/clip.mp4', media_hash: 'hash', media_kind: 'video', duration_us: 2_000_000, has_audio: false, url: '/static/projects/demo/freezone/_uploads/clip.mp4' });
    const result = analyzeReferenceVideo('video', { videoUrl: '/static/projects/demo/freezone/_uploads/clip.mp4' });
    await result.completion;
    expect(startSourceTranscript).not.toHaveBeenCalled();
    expect(reanalyzeVideoStory).toHaveBeenCalledWith('story', expect.objectContaining({ transcriptRef: null }));
  });

  it('creates a fresh story node when a completed analysis already exists', async () => {
    state.nodes.push({
      id: 'story-existing',
      type: 'videoStoryNode',
      data: { sourceVideoUrl: '/static/projects/demo/freezone/_uploads/clip.mp4', remixAnalysisStage: 'completed' },
    });
    state.edges.push({ source: 'video', target: 'story-existing' });

    const result = analyzeReferenceVideo('video', { videoUrl: '/static/projects/demo/freezone/_uploads/clip.mp4' });
    expect(result.nodeId).toBe('story');
    expect(state.edges).toContainEqual({ source: 'video', target: 'story' });
    await result.completion;
    expect(reanalyzeVideoStory).toHaveBeenCalledWith('story', expect.any(Object));
  });

  it('reanalyzes an explicit story target in place', async () => {
    state.nodes.push({
      id: 'story-existing',
      type: 'videoStoryNode',
      data: { sourceVideoUrl: '/static/projects/demo/freezone/_uploads/clip.mp4', remixAnalysisStage: 'completed' },
    });
    state.edges.push({ source: 'video', target: 'story-existing' });

    const result = analyzeReferenceVideo('video', {
      videoUrl: '/static/projects/demo/freezone/_uploads/clip.mp4',
      targetStoryId: 'story-existing',
    });
    expect(result.nodeId).toBe('story-existing');
    expect(state.addNode).not.toHaveBeenCalledWith('videoStoryNode', expect.anything(), expect.anything());
    await result.completion;
    expect(reanalyzeVideoStory).toHaveBeenCalledWith('story-existing', expect.any(Object));
  });

  it('returns the running story when the toolbar is clicked again before completion', async () => {
    let releaseMedia!: (media: Record<string, unknown>) => void;
    api.attachRemixMedia.mockReturnValueOnce(new Promise((resolve) => { releaseMedia = resolve; }));

    const first = analyzeReferenceVideo('video', { videoUrl: '/static/projects/demo/freezone/_uploads/clip.mp4' });
    const second = analyzeReferenceVideo('video', { videoUrl: '/static/projects/demo/freezone/_uploads/clip.mp4' });

    expect(second.nodeId).toBe(first.nodeId);
    expect(second.completion).toBe(first.completion);
    expect(state.addNode).toHaveBeenCalledTimes(1);

    releaseMedia({ media_path: 'freezone/_uploads/clip.mp4', media_hash: 'hash', media_kind: 'video', duration_us: 2_000_000, has_audio: true, url: '/static/projects/demo/freezone/_uploads/clip.mp4' });
    await first.completion;
  });

  it('reuses an existing manual transcription operation instead of submitting twice', async () => {
    startSourceTranscript.mockImplementation(async () => {
      state.nodes.push({ id: 'transcript', type: 'transcriptNode', data: { words: [], operationId: 'manual-op', sourceMediaHash: 'hash' } });
      state.edges.push({ source: 'video', target: 'transcript' });
      return 'transcript';
    });
    api.fetchRemixOperation.mockImplementation(async (_project, operationId) => ({ status: 'needs_confirmation', output_refs: [{ artifact_id: operationId === 'analyze_reference_video' ? 'evidence' : 'words', revision: 1 }] }));

    await analyzeReferenceVideo('video', { videoUrl: '/static/projects/demo/freezone/_uploads/clip.mp4' }).completion;

    expect(api.submitRemixProcessAction).not.toHaveBeenCalledWith('demo', expect.objectContaining({ action: 'transcribe_media' }));
    expect(api.fetchRemixOperation).toHaveBeenCalledWith('demo', 'manual-op');
  });

  it('drops old analysis results if the source video changes while processing', async () => {
    reanalyzeVideoStory.mockImplementation(() => ({ nodeId: 'story', completion: Promise.resolve().then(() => {
      state.nodes.find((node) => node.id === 'video')!.data.videoUrl = '/new.mp4';
      state.nodes.find((node) => node.id === 'story')!.data.referenceEvents = [{ spoken_text: '旧视频' }];
    }) }));

    await analyzeReferenceVideo('video', { videoUrl: '/static/projects/demo/freezone/_uploads/clip.mp4' }).completion;

    expect(state.nodes.find((node) => node.id === 'story')?.data.referenceEvents).toEqual([]);
    expect(state.nodes.find((node) => node.id === 'story')?.data.analysisError).toMatch(/原片已变化/);
  });
});
