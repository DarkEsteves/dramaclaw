import { beforeEach, describe, expect, it, vi } from 'vitest';
const state = vi.hoisted(() => ({
  nodes: [] as Array<{ id: string; type: string; data: Record<string, unknown> }>, edges: [] as Array<{ source: string; target: string }>,
  listeners: new Set<() => void>(), autoCompleteTranscript: false,
  subscribe: vi.fn((listener: () => void) => { state.listeners.add(listener); return () => { state.listeners.delete(listener); }; }),
  addNode: vi.fn((type: string, _position: unknown, data: Record<string, unknown> = {}) => { const id = type === 'transcriptNode' ? 'words-node' : 'story'; state.nodes.push({ id, type, data }); return id; }),
  addEdge: vi.fn((source: string, target: string) => { state.edges.push({ source, target }); return `${source}-${target}`; }),
  deleteNode: vi.fn(), findNodePosition: vi.fn(() => ({ x: 480, y: 220 })),
  updateNodeData: vi.fn((id: string, patch: Record<string, unknown>) => {
    const node = state.nodes.find((item) => item.id === id); if (node) node.data = { ...node.data, ...patch };
    for (const listener of state.listeners) listener();
    if (patch.autoStartMode === 'transcribe' && state.autoCompleteTranscript) queueMicrotask(() => state.updateNodeData(id, {
      autoStartMode: null, operationId: null, status: 'completed', artifactRef: { artifactId: 'words', revision: 1 },
      words: [{ text: '原话', startUs: 123456, endUs: 765432 }], transcriptText: '原话',
    }));
  }),
}));
const api = vi.hoisted(() => ({ apiCall: vi.fn(), attachRemixMedia: vi.fn(), fetchRemixArtifact: vi.fn() }));
vi.mock('@/stores/canvasStore', () => ({ useCanvasStore: { getState: () => state, subscribe: state.subscribe } }));
vi.mock('@/lib/url-params', () => ({ readUrl: () => ({ project: 'demo', canvas: 'canvas' }) }));
vi.mock('@/api/client', () => ({ apiCall: api.apiCall }));
vi.mock('@/api/remix', () => ({ attachRemixMedia: api.attachRemixMedia, fetchRemixArtifact: api.fetchRemixArtifact }));
vi.mock('@/features/freezone/canvasSyncRuntime', () => ({ flushFreezoneCanvasRuntime: vi.fn(async () => true) }));
import { analyzeReferenceVideo, waitForReferenceVideoAnalysis } from './analyzeReferenceVideo';
import { startSourceTranscript } from './startSourceTranscript';

const url = '/static/projects/demo/freezone/_uploads/clip.mp4';
const ref = { artifact_id: 'analysis', revision: 1 };
function fakeCompletedTranscriptAndAnalysis() {
  state.listeners.clear(); state.autoCompleteTranscript = true;
  api.fetchRemixArtifact.mockReset().mockImplementation(async (_project, input) => input.artifactId === 'words'
    ? { schema: 'word_timeline.v1', payload: { media_hash: 'sha256:hash', status: 'completed', language: 'zh', transcript: '原话', words: [{ text: '原话', start_char: 0, end_char: 2, start_us: 123456, end_us: 765432 }] } }
    : { schema: 'reference_analysis.v1', payload: { media_hash: 'sha256:hash', transcript_status: (state.nodes[0].data.remixMedia as { has_audio?: boolean })?.has_audio === false ? 'no_audio' : 'completed',
      evidence_ref: { artifact_id: 'evidence', revision: 1 }, transcript_ref: (state.nodes[0].data.remixMedia as { has_audio?: boolean })?.has_audio === false ? null : { artifact_id: 'words', revision: 1 },
      coverage: { visual_inspection: 'sampled_frames' }, story: { video_story: { summary: 'Visual structure' }, reference_events: [], analyses: [] }, limitations: [] } });
}
describe('server reference analysis', () => {
  beforeEach(() => {
    vi.clearAllMocks(); state.nodes.splice(0, state.nodes.length, { id: 'video', type: 'videoNode', data: { videoUrl: url } }); state.edges.splice(0);
    api.attachRemixMedia.mockResolvedValue({ media_path: 'freezone/_uploads/clip.mp4', media_hash: 'hash', media_kind: 'video', duration_us: 2000000, has_audio: true, url });
    api.apiCall.mockResolvedValue({ operation_id: 'op', status: 'completed', analysis_ref: ref });
    fakeCompletedTranscriptAndAnalysis();
  });
  it('uses one shared server request and reads its completed artifact', async () => {
    const result = analyzeReferenceVideo('video', { videoUrl: url }); await result.completion;
    expect(api.apiCall).toHaveBeenCalledWith('projects/demo/freezone/canvases/canvas/reference-analysis', expect.objectContaining({ method: 'POST', json: expect.objectContaining({ source_node_id: 'video' }) }));
    expect(api.fetchRemixArtifact).toHaveBeenCalledWith('demo', { artifactId: 'analysis', revision: 1 });
    expect(state.nodes.find(n => n.id === result.nodeId)?.data).toMatchObject({ remixAnalysisStage: 'completed', referenceEvidenceRef: { artifactId: 'evidence', revision: 1 }, referenceTranscriptStatus: 'completed' });
    await expect(waitForReferenceVideoAnalysis(result)).resolves.toMatchObject({ analysisNodeId: 'story' });
  });
  it('creates a manual analysis branch beside an agent-owned analysis without editing it', async () => {
    state.nodes.push({ id: 'agent-words', type: 'transcriptNode', data: {
      sourceMediaPath: 'freezone/_uploads/clip.mp4', sourceMediaHash: 'hash',
      status: 'completed', artifactRef: { artifactId: 'words', revision: 1 },
      words: [{ text: '原话', startUs: 123456, endUs: 765432 }], transcriptText: '原话',
    } });
    state.nodes.push({ id: 'agent-story', type: 'videoStoryNode', data: {
      sourceVideoUrl: url, referenceAnalysisRunId: 'agent-run', isAnalyzing: true, rows: [],
    } });
    state.edges.push({ source: 'video', target: 'agent-words' },
      { source: 'agent-words', target: 'agent-story' }, { source: 'video', target: 'agent-story' });
    const pinnedTranscript = JSON.stringify(state.nodes.find(node => node.id === 'agent-words')!.data);
    const pinnedAnalysis = JSON.stringify(state.nodes.find(node => node.id === 'agent-story')!.data);

    const manual = analyzeReferenceVideo('video', { videoUrl: url });
    await manual.completion;

    expect(manual.nodeId).toBe('story');
    expect(state.edges).toContainEqual({ source: 'agent-words', target: 'story' });
    expect(state.nodes.find(node => node.id === 'agent-words')!.data).toEqual(JSON.parse(pinnedTranscript));
    expect(state.nodes.find(node => node.id === 'agent-story')!.data).toEqual(JSON.parse(pinnedAnalysis));
    expect(api.apiCall).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({
      json: expect.objectContaining({ transcript_node_id: 'agent-words', analysis_node_id: 'story' }),
    }));
  });
  it('stops analysis when the independent recognition fails', async () => {
    state.autoCompleteTranscript = false;
    const result = analyzeReferenceVideo('video', { videoUrl: url });
    await vi.waitFor(() => expect(state.nodes.find(n => n.type === 'transcriptNode')?.data.autoStartMode).toBe('transcribe'));
    state.updateNodeData('words-node', { autoStartMode: null, status: 'failed', unresolved: ['Speech recognition unavailable'] });
    await result.completion;
    expect(api.apiCall).not.toHaveBeenCalled();
    expect(state.nodes.find(n => n.id === 'story')?.data.analysisError).toContain('Speech recognition unavailable');
  });
  it('does not infer speech from audio stream metadata', async () => {
    state.autoCompleteTranscript = false;
    const result = analyzeReferenceVideo('video', { videoUrl: url });
    await vi.waitFor(() => expect(state.nodes.find(n => n.type === 'transcriptNode')?.data.autoStartMode).toBe('transcribe'));
    state.updateNodeData('words-node', { autoStartMode: null, status: 'failed', unresolved: ['未检测到人声'] });
    await result.completion;
    expect(api.apiCall).not.toHaveBeenCalled();
    const transcript = state.nodes.find(n => n.type === 'transcriptNode');
    expect(transcript?.data).toMatchObject({ status: 'failed', unresolved: ['未检测到人声'] });
    expect(transcript?.data.artifactRef).toBeFalsy();
    expect(transcript?.data.words ?? []).toEqual([]);
  });
  it('shares an in-flight toolbar click', async () => {
    let release!: (value: unknown) => void;
    api.apiCall.mockReturnValueOnce(new Promise(resolve => { release = resolve; }));
    const first = analyzeReferenceVideo('video', { videoUrl: url }); const second = analyzeReferenceVideo('video', { videoUrl: url });
    expect(second.completion).toBe(first.completion); expect(state.addNode).toHaveBeenCalledOnce();
    await vi.waitFor(() => expect(api.apiCall).toHaveBeenCalledOnce());
    release({ operation_id: 'op', status: 'completed', analysis_ref: ref }); await first.completion;
  });
  it('creates both connected result nodes before the joint analysis returns', async () => {
    let release!: (value: unknown) => void;
    api.apiCall.mockReturnValueOnce(new Promise(resolve => { release = resolve; }));
    const result = analyzeReferenceVideo('video', { videoUrl: url });
    await vi.waitFor(() => expect(api.apiCall).toHaveBeenCalledOnce());
    const transcript = state.nodes.find(n => n.type === 'transcriptNode');
    try {
      expect(transcript).toBeDefined();
      expect(state.nodes.filter(n => n.type === 'videoStoryNode')).toHaveLength(1);
      expect(transcript?.data).toMatchObject({ autoStartMode: null, status: 'completed', artifactRef: { artifactId: 'words', revision: 1 } });
      expect(transcript?.data.managedOperationId).toBeFalsy();
      expect(state.edges).toContainEqual({ source: 'video', target: transcript?.id });
      expect(state.edges).toContainEqual({ source: transcript?.id, target: result.nodeId });
      expect(api.fetchRemixArtifact).not.toHaveBeenCalled();
    } finally {
      release({ operation_id: 'op', status: 'completed', analysis_ref: ref });
      await result.completion;
    }
    expect(state.nodes.filter(n => n.type === 'transcriptNode')).toHaveLength(1);
    expect(transcript?.data.managedOperationId).toBeFalsy();
  });
  it('keeps the completed transcript when the subsequent analysis fails', async () => {
    api.apiCall.mockRejectedValueOnce(new Error('分析提交失败'));
    await analyzeReferenceVideo('video', { videoUrl: url }).completion;
    expect(state.nodes.find(n => n.type === 'transcriptNode')?.data).toMatchObject({
      status: 'completed', artifactRef: { artifactId: 'words', revision: 1 }, transcriptText: '原话',
    });
    expect(state.nodes.find(n => n.id === 'story')?.data.analysisError).toBe('分析提交失败');
  });
  it('does not create a transcript for a video without an audio track', async () => {
    api.attachRemixMedia.mockResolvedValueOnce({ media_path: 'freezone/_uploads/clip.mp4', media_hash: 'hash', media_kind: 'video', has_audio: false, url });
    await analyzeReferenceVideo('video', { videoUrl: url }).completion;
    expect(state.nodes.filter(n => n.type === 'transcriptNode')).toHaveLength(0);
    expect(state.nodes.find(n => n.id === 'story')?.data.remixAnalysisStage).toBe('completed');
  });
  it('drops stale evidence when the source changes during processing', async () => {
    api.fetchRemixArtifact.mockImplementationOnce(async () => { state.nodes.find(n => n.id === 'video')!.data.videoUrl = '/new.mp4'; return { schema: 'reference_analysis.v1', payload: { media_hash: 'sha256:hash', story: { video_story: { summary: 'Old' } } } }; });
    await analyzeReferenceVideo('video', { videoUrl: url }).completion;
    expect(state.nodes.find(n => n.id === 'story')?.data).toMatchObject({ remixAnalysisStage: 'failed', referenceEvidenceRef: null, referenceAnalysisRef: null });
  });
  it('keeps agent completion pending until usable analysis is persisted', async () => {
    state.nodes.push({ id: 'ready', type: 'videoStoryNode', data: { remixAnalysisStage: 'completed', rawResult: { video_story: { summary: 'Ready' } } } });
    let release!: () => void; const persist = vi.fn(() => new Promise<void>(resolve => { release = resolve; }));
    let settled = false; const output = waitForReferenceVideoAnalysis({ nodeId: 'ready', completion: Promise.resolve() }, persist).then(() => { settled = true; });
    await Promise.resolve(); expect(settled).toBe(false); release(); await output; expect(settled).toBe(true);
  });
});

describe('saved transcription precedes analysis admission', () => {
  beforeEach(() => {
    vi.clearAllMocks(); state.listeners.clear(); state.autoCompleteTranscript = false;
    state.nodes.splice(0, state.nodes.length, { id: 'video', type: 'videoNode', data: { videoUrl: url,
      remixMedia: { media_path: 'freezone/_uploads/clip.mp4', media_hash: 'hash', media_kind: 'video', has_audio: true, url } } });
    state.edges.splice(0);
    api.apiCall.mockResolvedValue({ operation_id: 'op', status: 'completed', analysis_ref: ref });
    api.fetchRemixArtifact.mockImplementation(async (_project, input) => input.artifactId === 'analysis'
      ? { schema: 'reference_analysis.v1', payload: { media_hash: 'sha256:hash', transcript_status: 'completed',
        transcript_ref: { artifact_id: 'saved-corrected', revision: 2 }, story: { video_story: { summary: '校字输入' } } } }
      : { schema: 'word_timeline.v1', payload: { media_hash: 'sha256:hash', status: 'completed', language: 'zh', transcript: '校字',
        words: [{ text: '校字', start_char: 0, end_char: 2, start_us: 123456, end_us: 765432 }] } });
  });
  it('creates both nodes, actually starts independent transcription, and waits for saved corrected words before analysis', async () => {
    const result = analyzeReferenceVideo('video', { videoUrl: url });
    await vi.waitFor(() => expect(state.nodes.find(n => n.type === 'transcriptNode')?.data.autoStartMode).toBe('transcribe'));
    const words = state.nodes.find(n => n.type === 'transcriptNode')!;
    expect(words.data.autoStartMode).toBe('transcribe');
    expect(words.data.managedOperationId).toBeFalsy();
    expect(api.apiCall).not.toHaveBeenCalled();
    expect(state.edges).toContainEqual({ source: words.id, target: result.nodeId });
    state.updateNodeData(words.id, { autoStartMode: null, operationId: null, status: 'completed', artifactRef: { artifactId: 'saved-corrected', revision: 2 },
      words: [{ text: '校字', startUs: 123456, endUs: 765432 }], transcriptText: '校字' });
    await result.completion;
    expect(api.apiCall).toHaveBeenCalledOnce();
    expect(api.apiCall).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({ json: expect.objectContaining({ transcript_node_id: words.id, analysis_node_id: result.nodeId }) }));
    expect(state.nodes.find(n => n.id === result.nodeId)?.data.referenceTranscriptRef).toEqual(words.data.artifactRef);
  });
  it('keeps a completed genuine transcript when analysis request validation returns 422', async () => {
    const actual = { status: 'completed', artifactRef: { artifactId: 'saved-corrected', revision: 2 }, words: [{ text: '校字', startUs: 123456, endUs: 765432 }],
      transcriptText: '校字', sourceMediaPath: 'freezone/_uploads/clip.mp4', sourceMediaHash: 'hash' };
    state.nodes.push({ id: 'saved-words', type: 'transcriptNode', data: actual });
    state.edges.push({ source: 'video', target: 'saved-words' });
    api.apiCall.mockRejectedValueOnce(new Error('POST /reference-analysis 422'));
    await analyzeReferenceVideo('video', { videoUrl: url }).completion;
    expect(state.nodes.find(n => n.id === 'saved-words')?.data).toEqual(actual);
    expect(state.nodes.find(n => n.id === 'story')?.data.analysisError).toBe('POST /reference-analysis 422');
  });
  it('does not start analysis or invent an analysis error on the transcript when real transcription fails', async () => {
    const result = analyzeReferenceVideo('video', { videoUrl: url });
    await vi.waitFor(() => expect(state.nodes.find(n => n.type === 'transcriptNode')?.data.autoStartMode).toBe('transcribe'));
    const words = state.nodes.find(n => n.type === 'transcriptNode')!;
    expect(api.apiCall).not.toHaveBeenCalled();
    state.updateNodeData(words.id, { autoStartMode: null, operationId: null, status: 'failed', unresolved: ['真实识别失败'] });
    await result.completion;
    expect(api.apiCall).not.toHaveBeenCalled();
    expect(words.data.unresolved).toEqual(['真实识别失败']);
    expect(state.nodes.find(n => n.id === 'story')?.data.analysisError).toContain('真实识别失败');
  });
  it('waits for a running re-transcription instead of consuming the older completed artifact behind it', async () => {
    state.nodes.push({ id: 'saved-words', type: 'transcriptNode', data: { status: 'completed', artifactRef: { artifactId: 'old', revision: 1 },
      words: [{ text: '旧词' }], sourceMediaPath: 'freezone/_uploads/clip.mp4', sourceMediaHash: 'hash', transcriptSubmissionPending: 'actual-retranscription-key' } });
    state.edges.push({ source: 'video', target: 'saved-words' });
    const result = analyzeReferenceVideo('video', { videoUrl: url });
    await vi.waitFor(() => expect(state.edges).toContainEqual({ source: 'saved-words', target: 'story' }));
    expect(api.apiCall).not.toHaveBeenCalled();
    state.updateNodeData('saved-words', { transcriptSubmissionPending: null, status: 'completed', artifactRef: { artifactId: 'saved-corrected', revision: 2 },
      words: [{ text: '校字', startUs: 123456, endUs: 765432 }] });
    await result.completion;
    expect(state.nodes.find(n => n.id === 'story')?.data.referenceTranscriptRef).toEqual({ artifactId: 'saved-corrected', revision: 2 });
  });
});

describe('completed reference transcript projection', () => {
  beforeEach(() => {
    vi.clearAllMocks(); state.nodes.splice(0, state.nodes.length, { id: 'video', type: 'videoNode', data: { videoUrl: url } }); state.edges.splice(0);
    api.apiCall.mockResolvedValue({ operation_id: 'op', status: 'completed', analysis_ref: ref });
    fakeCompletedTranscriptAndAnalysis();
  });
  it('recognizes an idle empty transcript before analysis without creating another transcript node', async () => {
    state.nodes.push({id:'pending-words',type:'transcriptNode',data:{sourceMediaPath:'freezone/_uploads/clip.mp4',sourceMediaHash:'hash',words:[],autoStartMode:null,status:'needs_review'}});
    state.edges.push({source:'video',target:'pending-words'});
    api.attachRemixMedia.mockResolvedValue({media_path:'freezone/_uploads/clip.mp4',media_hash:'hash',media_kind:'video',has_audio:true,url});
    api.fetchRemixArtifact.mockResolvedValueOnce({schema:'reference_analysis.v1',payload:{media_path:'freezone/_uploads/clip.mp4',media_hash:'sha256:hash',transcript_status:'completed',
      transcript_ref:{artifact_id:'words',revision:1},story:{video_story:{summary:'Actual'}}}}).mockResolvedValueOnce({schema:'word_timeline.v1',payload:{media_hash:'sha256:hash',status:'completed',language:'zh',transcript:'原话',
      words:[{text:'原话',start_char:0,end_char:2,start_us:123456,end_us:765432}]}});
    await analyzeReferenceVideo('video',{videoUrl:url}).completion;
    expect(state.nodes.filter(n=>n.type==='transcriptNode')).toHaveLength(1);
    expect(state.nodes.find(n=>n.id==='pending-words')?.data).toMatchObject({status:'completed',autoStartMode:null,artifactRef:{artifactId:'words',revision:1},words:[{text:'原话',startUs:123456,endUs:765432}]});
  });

  it('shows real recognition words and preserves corrections on repeated completion', async () => {
    const source = state.nodes.find(n => n.id === 'video')!;
    source.data.remixMedia = { media_path: 'freezone/_uploads/clip.mp4', media_hash: 'hash', media_kind: 'video', has_audio: true, url };
    api.fetchRemixArtifact.mockResolvedValueOnce({ schema: 'reference_analysis.v1', payload: { media_path: 'freezone/_uploads/clip.mp4', media_hash: 'sha256:hash', transcript_status: 'completed',
      transcript_ref: { artifact_id: 'words', revision: 1 }, story: { video_story: { summary: 'Original' } } } }).mockResolvedValueOnce({ schema: 'word_timeline.v1', payload: {
        media_hash: 'sha256:hash', status: 'completed', language: 'zh', transcript: '原话', words: [{ text: '原话', start_char: 0, end_char: 2, start_us: 123456, end_us: 765432 }] } });
    await analyzeReferenceVideo('video', { videoUrl: url }).completion;
    const transcript = state.nodes.find(n => n.type === 'transcriptNode');
    expect(transcript?.data).toMatchObject({ artifactRef: { artifactId: 'words', revision: 1 }, sourceMediaPath: 'freezone/_uploads/clip.mp4', sourceMediaHash: 'hash', status: 'completed',
      words: [{ text: '原话', startUs: 123456, endUs: 765432 }], transcriptText: '原话' });
    expect(state.edges).toContainEqual({ source: 'video', target: transcript?.id });
    expect(state.edges).toContainEqual({ source: transcript?.id, target: 'story' });
    const analysisArtifact = await api.fetchRemixArtifact.mock.results[0].value;
    const wordArtifact = await api.fetchRemixArtifact.mock.results[1].value;
    const corrected = {artifactRef:{artifactId:'words',revision:2},words:[{text:'校字',startUs:123456,endUs:765432}],transcriptText:'校字'};
    state.updateNodeData(transcript!.id,corrected);
    api.fetchRemixArtifact.mockResolvedValueOnce({ ...analysisArtifact, payload: { ...analysisArtifact.payload, transcript_ref: { artifact_id: 'words', revision: 2 } } })
      .mockResolvedValueOnce({ ...wordArtifact, payload: { ...wordArtifact.payload, transcript: '校字' } });
    await analyzeReferenceVideo('video',{videoUrl:url,targetStoryId:'story'}).completion;
    expect(api.apiCall).toHaveBeenLastCalledWith('projects/demo/freezone/canvases/canvas/reference-analysis', expect.objectContaining({
      json: expect.objectContaining({ transcript_node_id: transcript!.id, analysis_node_id: 'story' }),
    }));
    expect(state.nodes.filter(n=>n.type==='transcriptNode')).toHaveLength(1);
    expect(state.nodes.find(n=>n.id===transcript!.id)?.data).toMatchObject(corrected);
  });
  it('rejects analysis that did not consume the selected corrected revision', async () => {
    state.nodes[0].data.remixMedia = { media_path: 'freezone/_uploads/clip.mp4', media_hash: 'hash', media_kind: 'video', has_audio: true, url };
    const corrected = { status: 'completed', artifactRef: { artifactId: 'words', revision: 2 }, words: [{ text: '校字' }], sourceMediaPath: 'freezone/_uploads/clip.mp4', sourceMediaHash: 'hash' };
    state.nodes.push({ id: 'corrected', type: 'transcriptNode', data: corrected });
    state.edges.push({ source: 'video', target: 'corrected' });
    api.fetchRemixArtifact.mockResolvedValueOnce({ schema: 'reference_analysis.v1', payload: { media_hash: 'sha256:hash', transcript_status: 'completed',
      transcript_ref: { artifact_id: 'words', revision: 1 }, story: { video_story: { summary: 'Old transcript' } } } });
    await analyzeReferenceVideo('video', { videoUrl: url }).completion;
    expect(state.nodes.find(n => n.id === 'story')?.data).toMatchObject({ remixAnalysisStage: 'failed', analysisError: '原片分析未使用当前转写修订，请重新分析' });
    expect(state.nodes.find(n => n.id === 'corrected')?.data).toEqual(corrected);
  });

  it.each([{ operationId: 'standalone-asr' }, { autoStartMode: 'transcribe' }, { managedOperationId: 'standalone-submitting' }])('waits behind existing independent recognition %o', async busy => {
    state.nodes[0].data.remixMedia = { media_path: 'freezone/_uploads/clip.mp4', media_hash: 'hash', media_kind: 'video', has_audio: true, url };
    const data = { ...busy, words: [], sourceMediaPath: 'freezone/_uploads/clip.mp4', sourceMediaHash: 'hash', status: 'needs_review' };
    state.nodes.push({ id: 'independent', type: 'transcriptNode', data });
    state.edges.push({ source: 'video', target: 'independent' });
    const result = analyzeReferenceVideo('video', { videoUrl: url });
    await vi.waitFor(() => expect(state.edges).toContainEqual({ source: 'independent', target: 'story' }));
    expect(api.apiCall).not.toHaveBeenCalled();
    expect(state.nodes.find(n => n.id === 'independent')?.data).toEqual(data);
    state.updateNodeData('independent', { operationId: null, autoStartMode: null, managedOperationId: null, status: 'completed', artifactRef: { artifactId: 'words', revision: 1 }, words: [{ text: '原话' }] });
    await result.completion;
    expect(state.nodes.find(n => n.id === 'story')?.data.remixAnalysisStage).toBe('completed');
  });
  it('defers while independent transcription is preparing media before its node exists', async () => {
    let release!: (value: unknown) => void;
    api.attachRemixMedia.mockReturnValueOnce(new Promise(resolve => { release = resolve; }));
    const independent = startSourceTranscript('video');
    try {
      await analyzeReferenceVideo('video', { videoUrl: url }).completion;
      expect(api.apiCall).not.toHaveBeenCalled();
      expect(state.nodes.find(n => n.id === 'story')?.data.analysisError).toBe('原片转写仍在处理中，请完成后重新点击分析原片');
    } finally {
      release({ media_path: 'freezone/_uploads/clip.mp4', media_hash: 'hash', media_kind: 'video', has_audio: true, url });
      await independent;
    }
  });
  it('pins the independently completed correction while awaiting the existing recognition', async () => {
    state.nodes[0].data.remixMedia = { media_path: 'freezone/_uploads/clip.mp4', media_hash: 'hash', media_kind: 'video', has_audio: true, url };
    state.nodes.push({ id: 'independent', type: 'transcriptNode', data: { operationId: 'standalone', words: [], sourceMediaPath: 'freezone/_uploads/clip.mp4', sourceMediaHash: 'hash' } });
    state.edges.push({ source: 'video', target: 'independent' });
    const result = analyzeReferenceVideo('video', { videoUrl: url });
    await vi.waitFor(() => expect(state.edges).toContainEqual({ source: 'independent', target: 'story' }));
    expect(api.apiCall).not.toHaveBeenCalled();
    state.updateNodeData('independent', { operationId: null, status: 'completed', artifactRef: { artifactId: 'standalone', revision: 2 }, words: [{ text: '校字' }] });
    api.fetchRemixArtifact.mockResolvedValueOnce({ schema: 'reference_analysis.v1', payload: { media_hash: 'sha256:hash', transcript_status: 'completed', transcript_ref: { artifact_id: 'standalone', revision: 2 }, story: { video_story: { summary: '校字' } } } })
      .mockResolvedValueOnce({ schema: 'word_timeline.v1', payload: { media_hash: 'sha256:hash', status: 'completed', language: 'zh', words: [{ text: '校字', start_char: 0, end_char: 2, start_us: 0, end_us: 10 }] } });
    await result.completion;
    const input = state.nodes.find(n => n.id === 'independent')!;
    const analysis = state.nodes.find(n => n.id === 'story')!;
    expect(analysis.data.remixAnalysisStage).toBe('completed');
    expect(analysis.data.referenceTranscriptRef).toEqual(input.data.artifactRef);
    expect(state.edges).toContainEqual({ source: 'independent', target: 'story' });
  });
  it('uses the selected story dependency even when another source transcript precedes it', async () => {
    state.nodes[0].data.remixMedia = { media_path: 'freezone/_uploads/clip.mp4', media_hash: 'hash', media_kind: 'video', has_audio: true, url };
    for (const [id, artifactId] of [['A', 'old'], ['B', 'corrected']]) {
      state.nodes.push({ id, type: 'transcriptNode', data: { status: 'completed', artifactRef: { artifactId, revision: 1 }, words: [{ text: id }], sourceMediaPath: 'freezone/_uploads/clip.mp4', sourceMediaHash: 'hash' } });
      state.edges.push({ source: 'video', target: id });
    }
    state.nodes.push({ id: 'story', type: 'videoStoryNode', data: { sourceVideoUrl: url } });
    state.edges.push({ source: 'video', target: 'story' }, { source: 'B', target: 'story' });
    api.fetchRemixArtifact.mockResolvedValueOnce({ schema: 'reference_analysis.v1', payload: { media_hash: 'sha256:hash', transcript_status: 'completed', transcript_ref: { artifact_id: 'corrected', revision: 1 }, story: { video_story: { summary: 'Corrected input' } } } })
      .mockResolvedValueOnce({ schema: 'word_timeline.v1', payload: { media_hash: 'sha256:hash', status: 'completed', language: 'zh', words: [{ text: 'B', start_char: 0, end_char: 1, start_us: 0, end_us: 1 }] } });
    await analyzeReferenceVideo('video', { videoUrl: url, targetStoryId: 'story' }).completion;
    expect(api.apiCall).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({ json: expect.objectContaining({ transcript_node_id: 'B' }) }));
    expect(state.nodes.find(n => n.id === 'story')?.data.referenceTranscriptRef).toEqual({ artifactId: 'corrected', revision: 1 });
    expect(state.edges).not.toContainEqual({ source: 'A', target: 'story' });
    expect(state.edges).toContainEqual({ source: 'B', target: 'story' });
  });
  it.each([false, true])('rejects ambiguous transcript inputs (already linked: %s)', async targetLinks => {
    state.nodes[0].data.remixMedia = { media_path: 'freezone/_uploads/clip.mp4', media_hash: 'hash', media_kind: 'video', has_audio: true, url };
    state.nodes.push({ id: 'story', type: 'videoStoryNode', data: { sourceVideoUrl: url } });
    state.edges.push({ source: 'video', target: 'story' });
    for (const id of ['A', 'B']) {
      state.nodes.push({ id, type: 'transcriptNode', data: { status: 'completed', artifactRef: { artifactId: id, revision: 1 }, words: [{ text: id }], sourceMediaPath: 'freezone/_uploads/clip.mp4', sourceMediaHash: 'hash' } });
      state.edges.push({ source: 'video', target: id });
      if (targetLinks) state.edges.push({ source: id, target: 'story' });
    }
    const edges = [...state.edges];
    await analyzeReferenceVideo('video', { videoUrl: url, targetStoryId: 'story' }).completion;
    expect(api.apiCall).not.toHaveBeenCalled();
    expect(state.nodes.find(n => n.id === 'story')?.data.analysisError).toBe('存在多个原片转写输入，请保留一个明确连接的转写后重新分析');
    expect(state.edges).toEqual(edges);
  });
  it('discards completion after the user removes the selected transcript dependency', async () => {
    state.nodes[0].data.remixMedia = { media_path: 'freezone/_uploads/clip.mp4', media_hash: 'hash', media_kind: 'video', has_audio: true, url };
    state.nodes.push({ id: 'selected', type: 'transcriptNode', data: { status: 'completed', artifactRef: { artifactId: 'words', revision: 1 }, words: [{ text: '原话' }], sourceMediaPath: 'freezone/_uploads/clip.mp4', sourceMediaHash: 'hash' } });
    state.edges.push({ source: 'video', target: 'selected' });
    let release!: (value: unknown) => void;
    api.apiCall.mockReturnValueOnce(new Promise(resolve => { release = resolve; }));
    const result = analyzeReferenceVideo('video', { videoUrl: url });
    await vi.waitFor(() => expect(api.apiCall).toHaveBeenCalledOnce());
    state.edges.splice(state.edges.findIndex(e => e.source === 'selected' && e.target === 'story'), 1);
    release({ operation_id: 'op', status: 'completed', analysis_ref: ref });
    await result.completion;
    expect(state.nodes.find(n => n.id === 'story')?.data).toMatchObject({ remixAnalysisStage: 'failed', referenceAnalysisRef: null });
    expect(state.edges).not.toContainEqual({ source: 'selected', target: 'story' });
    expect(api.fetchRemixArtifact).not.toHaveBeenCalled();
  });
});
