import { attachRemixMedia, fetchRemixArtifact, fetchRemixOperation, fetchRemixTaskStatus, submitRemixProcessAction, type RemixArtifactRef, type RemixUploadedMedia } from '@/api/remix';
import { CANVAS_NODE_TYPES } from '@/features/canvas/domain/canvasNodes';
import { timelineWords, type TranscriptTimeline } from '@/features/canvas/remix/transcriptModel';
import { projectMediaPathFromUrl } from '@/features/canvas/remix/remixMediaPath';
import { readUrl } from '@/lib/url-params';
import { useCanvasStore } from '@/stores/canvasStore';
import { startSourceTranscript } from './startSourceTranscript';
import { reanalyzeVideoStory } from './videoAnalyzeStory';

const running = new Map<string, Promise<void>>();

function patch(id: string, value: Record<string, unknown>): void {
  useCanvasStore.getState().updateNodeData(id, value);
}

function nodeData(id: string): Record<string, unknown> | null {
  return useCanvasStore.getState().nodes.find((node) => node.id === id)?.data ?? null;
}

async function waitForOutput(project: string, operationId: string): Promise<RemixArtifactRef> {
  const deadline = Date.now() + 30 * 60_000;
  while (Date.now() < deadline) {
    const operation = await fetchRemixOperation(project, operationId);
    if (operation.status === 'failed' || operation.status === 'cancelled') throw new Error('原片处理任务失败');
    if (operation.status === 'needs_confirmation' || operation.status === 'completed') {
      const output = operation.output_refs.find((item) => item.artifact_id && item.revision);
      if (!output?.artifact_id || !output.revision) throw new Error('原片处理任务没有返回产物');
      return { artifactId: output.artifact_id, revision: output.revision };
    }
    if (operation.task_type && operation.task_scope) {
      const task = await fetchRemixTaskStatus(project, operation.task_type, operation.task_scope);
      if (task?.status === 'failed') throw new Error(task.error || '原片处理任务失败');
    }
    await new Promise((resolve) => window.setTimeout(resolve, 1_000));
  }
  throw new Error('原片处理超时，请检查任务状态');
}

async function ensureVideoMedia(project: string, sourceId: string, videoUrl: string): Promise<RemixUploadedMedia> {
  const source = nodeData(sourceId);
  if (!source || source.videoUrl !== videoUrl) throw new Error('原片已变化，请重新分析');
  const path = projectMediaPathFromUrl(project, videoUrl);
  const existing = source.remixMedia as RemixUploadedMedia | undefined;
  if (existing?.media_kind === 'video' && (path ? existing.media_path === path : existing.url === videoUrl)) return existing;
  if (!path) throw new Error('原片尚未保存到当前项目');
  const media = await attachRemixMedia(project, path);
  if (media.media_kind !== 'video' || nodeData(sourceId)?.videoUrl !== videoUrl) throw new Error('原片已变化，请重新分析');
  patch(sourceId, { remixMedia: media, remixRole: 'reference' });
  return media;
}

async function ensureEvidence(project: string, storyId: string, media: RemixUploadedMedia): Promise<RemixArtifactRef> {
  const story = nodeData(storyId);
  const cached = story?.referenceEvidenceRef as RemixArtifactRef | null | undefined;
  if (cached && story?.sourceMediaHash === media.media_hash) return cached;
  patch(storyId, { remixAnalysisStage: 'extracting', referenceEvidenceRef: null, referenceBlueprintRef: null });
  const task = await submitRemixProcessAction(project, {
    action: 'analyze_reference_video', mediaPath: media.media_path,
    idempotencyKey: crypto.randomUUID(), nodeRevision: Number(story?.blueprintRevision ?? 0) + 1,
  });
  patch(storyId, { managedEvidenceOperationId: task.operation_id });
  const ref = await waitForOutput(project, task.operation_id);
  const artifact = await fetchRemixArtifact(project, ref);
  if (artifact.schema !== 'reference_evidence.v1' || artifact.payload.media_hash !== `sha256:${media.media_hash}`) throw new Error('原片证据与当前视频不匹配');
  patch(storyId, { referenceEvidenceRef: ref, managedEvidenceOperationId: null, blueprintRevision: Number(story?.blueprintRevision ?? 0) + 1 });
  return ref;
}

async function ensureTranscript(project: string, sourceId: string, storyId: string, media: RemixUploadedMedia): Promise<RemixArtifactRef | null> {
  if (!media.has_audio) return null;
  const transcriptId = await startSourceTranscript(sourceId, { autoStart: false });
  const store = useCanvasStore.getState();
  if (!store.edges.some((edge) => edge.source === transcriptId && edge.target === storyId)) {
    if (!store.addEdge(transcriptId, storyId)) throw new Error('连接原片转写与拆解节点失败');
  }
  const data = nodeData(transcriptId);
  const cached = data?.artifactRef as RemixArtifactRef | null | undefined;
  if (cached && data?.status === 'completed' && data?.sourceMediaHash === media.media_hash) {
    patch(storyId, { referenceTranscriptRef: cached });
    return cached;
  }
  patch(storyId, { remixAnalysisStage: 'transcribing', referenceTranscriptRef: null });
  const manualOperation = typeof data?.operationId === 'string' && data.operationId ? data.operationId : null;
  const managedOperation = typeof data?.managedOperationId === 'string' && data.managedOperationId !== 'pending' ? data.managedOperationId : null;
  let operationId = manualOperation ?? managedOperation;
  try {
    if (!operationId) {
      // Reserve the node before the network request, so its manual button cannot
      // submit another transcription while this request is in flight.
      patch(transcriptId, { managedOperationId: 'pending', status: 'needs_review', words: [], transcriptText: null, sourceMediaPath: media.media_path, sourceMediaHash: media.media_hash });
      const task = await submitRemixProcessAction(project, {
        action: 'transcribe_media', mediaPath: media.media_path, language: 'auto',
        idempotencyKey: crypto.randomUUID(), nodeRevision: Number(data?.nodeRevision ?? 0) + 1,
      });
      operationId = task.operation_id;
      patch(transcriptId, { managedOperationId: operationId, nodeRevision: Number(data?.nodeRevision ?? 0) + 1 });
    }
    const ref = await waitForOutput(project, operationId);
    const artifact = await fetchRemixArtifact(project, ref);
    const payload = artifact.payload as TranscriptTimeline;
    if (artifact.schema !== 'word_timeline.v1') throw new Error('原片转写格式错误');
    if (payload.status === 'no_speech') {
      patch(transcriptId, { artifactRef: null, managedOperationId: null, status: 'needs_review', words: [], transcriptText: '', unresolved: ['未检测到人声'] });
      return null;
    }
    if (payload.status !== 'completed') throw new Error('原片转写未完成');
    patch(transcriptId, { artifactRef: ref, managedOperationId: null, status: 'completed', words: timelineWords(payload), transcriptText: payload.transcript ?? '', unresolved: [] });
    patch(storyId, { referenceTranscriptRef: ref });
    return ref;
  } catch (error) {
    patch(transcriptId, { managedOperationId: null, status: 'failed' });
    throw error;
  }
}

async function runAnalysis(sourceId: string, storyId: string, videoUrl: string): Promise<void> {
  const project = readUrl().project;
  let expectedMediaHash: string | null = null;
  try {
    if (!project) throw new Error('请先打开项目画布');
    const media = await ensureVideoMedia(project, sourceId, videoUrl);
    expectedMediaHash = media.media_hash;
    const story = nodeData(storyId);
    const canReuse = story?.sourceMediaHash === media.media_hash;
    if (!canReuse) patch(storyId, { sourceMediaHash: media.media_hash, sourceMediaPath: media.media_path, referenceEvidenceRef: null, referenceTranscriptRef: null, referenceBlueprintRef: null });
    const [evidenceRef, transcriptRef] = await Promise.all([
      ensureEvidence(project, storyId, media), ensureTranscript(project, sourceId, storyId, media),
    ]);
    if (nodeData(sourceId)?.videoUrl !== videoUrl || (nodeData(sourceId)?.remixMedia as RemixUploadedMedia | undefined)?.media_hash !== media.media_hash) throw new Error('原片已变化，请重新分析');
    patch(storyId, { remixAnalysisStage: 'analyzing', referenceEvidenceRef: evidenceRef, referenceTranscriptRef: transcriptRef });
    const analysis = reanalyzeVideoStory(storyId, { durationSec: media.duration_us / 1_000_000, evidenceRef, transcriptRef });
    if (!analysis) throw new Error('无法启动原片联合分析');
    await analysis.completion;
    if (nodeData(sourceId)?.videoUrl !== videoUrl || (nodeData(sourceId)?.remixMedia as RemixUploadedMedia | undefined)?.media_hash !== media.media_hash) throw new Error('原片已变化，请重新分析');
    const error = nodeData(storyId)?.analysisError;
    if (typeof error === 'string' && error) throw new Error(error);
    patch(storyId, { remixAnalysisStage: 'completed', isAnalyzing: false });
  } catch (error) {
    const sourceChanged = nodeData(sourceId)?.videoUrl !== videoUrl || (expectedMediaHash !== null && (nodeData(sourceId)?.remixMedia as RemixUploadedMedia | undefined)?.media_hash !== expectedMediaHash);
    patch(storyId, { remixAnalysisStage: 'failed', managedEvidenceOperationId: null, isAnalyzing: false, analysisError: error instanceof Error ? error.message : String(error), ...(sourceChanged ? { rows: [], referenceEvents: [], referenceEvidenceRef: null, referenceTranscriptRef: null, referenceBlueprintRef: null } : {}) });
  }
}

export function analyzeReferenceVideo(sourceId: string, opts: { videoUrl: string; targetStoryId?: string }): { nodeId: string; completion: Promise<void> } {
  const store = useCanvasStore.getState();
  const source = store.nodes.find((node) => node.id === sourceId);
  if (!source || source.type !== CANVAS_NODE_TYPES.video || source.data.videoUrl !== opts.videoUrl) throw new Error('请选择已上传的视频节点');

  const explicitTarget = opts.targetStoryId
    ? store.nodes.find((node) => node.id === opts.targetStoryId
      && node.type === CANVAS_NODE_TYPES.videoStory
      && store.edges.some((edge) => edge.source === sourceId && edge.target === node.id))
    : null;
  if (opts.targetStoryId && !explicitTarget) throw new Error('当前原片拆解节点未连接此视频');

  const runningTarget = !explicitTarget
    ? store.edges
      .filter((edge) => edge.source === sourceId)
      .map((edge) => store.nodes.find((node) => node.id === edge.target && node.type === CANVAS_NODE_TYPES.videoStory))
      .find((node) => node && running.has(node.id))
    : null;
  if (runningTarget) return { nodeId: runningTarget.id, completion: running.get(runningTarget.id)! };

  const storyId = explicitTarget?.id ?? store.addNode(CANVAS_NODE_TYPES.videoStory, store.findNodePosition(sourceId, 720, 360), {
    displayName: '原片拆解', sourceVideoUrl: opts.videoUrl, rows: [], rawResult: null, isAnalyzing: true, remixAnalysisStage: 'preparing', analysisError: null,
  });
  if (!explicitTarget && !store.addEdge(sourceId, storyId)) { store.deleteNode(storyId); throw new Error('连接原片拆解节点失败'); }
  const pending = running.get(storyId);
  if (pending) return { nodeId: storyId, completion: pending };
  patch(storyId, { sourceVideoUrl: opts.videoUrl, isAnalyzing: true, analysisError: null, remixAnalysisStage: 'preparing' });
  const completion = runAnalysis(sourceId, storyId, opts.videoUrl).finally(() => running.delete(storyId));
  running.set(storyId, completion);
  return { nodeId: storyId, completion };
}
