import { CANVAS_NODE_TYPES, type CanvasEdge, type CanvasNode } from '@/features/canvas/domain/canvasNodes';
import { useCanvasStore } from '@/stores/canvasStore';

type Ref = { artifactId: string; revision: number };

function ref(value: unknown): Ref | null {
  if (!value || typeof value !== 'object') return null;
  const item = value as Record<string, unknown>;
  return typeof item.artifactId === 'string' && Number.isInteger(item.revision) && Number(item.revision) > 0
    ? { artifactId: item.artifactId, revision: Number(item.revision) } : null;
}

function sameRef(left: Ref | null, right: Ref | null): boolean {
  return Boolean(left && right && left.artifactId === right.artifactId && left.revision === right.revision);
}

function canonicalMediaHash(value: unknown): string | null {
  if (typeof value !== 'string' || !value.trim()) return null;
  const normalized = value.trim().toLowerCase();
  return normalized.startsWith('sha256:') ? normalized : `sha256:${normalized}`;
}

function ancestors(nodes: CanvasNode[], edges: CanvasEdge[], targetId: string): CanvasNode[] {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const result: CanvasNode[] = [];
  const seen = new Set([targetId]);
  const queue = [targetId];
  while (queue.length) {
    const target = queue.shift()!;
    for (const edge of edges) {
      if (edge.target !== target || seen.has(edge.source)) continue;
      seen.add(edge.source);
      const node = byId.get(edge.source);
      if (node) { result.push(node); queue.push(node.id); }
    }
  }
  return result;
}

export function planTimelineFromScript(nodes: CanvasNode[], edges: CanvasEdge[], scriptId: string): { sourceIds: string[] } {
  const script = nodes.find((node) => node.id === scriptId && node.type === CANVAS_NODE_TYPES.script);
  const scriptRef = ref(script?.data.remixScriptRef);
  if (!scriptRef) throw new Error('请先保存改编脚本。');
  const narration = nodes.find((node) => node.type === CANVAS_NODE_TYPES.audio
    && node.data.remixRole === 'narration'
    && sameRef(ref(node.data.remixScriptRef), scriptRef)
    && edges.some((edge) => edge.source === scriptId && edge.target === node.id));
  const media = narration?.data.remixMedia as { url?: string; media_path?: string; media_hash?: string } | undefined;
  if (narration && !narration.data.audioUrl) {
    throw new Error('当前配音没有音频，请先生成或上传配音。');
  }
  if (narration?.data.audioUrl && !media) {
    throw new Error('当前配音尚未附着媒体，请重新按脚本对齐。');
  }
  if (narration?.data.audioUrl && media?.url && narration.data.audioUrl !== media.url) {
    throw new Error('当前配音音频已变化，请重新按脚本对齐。');
  }
  const scriptAlignments = nodes.filter((node) => node.type === CANVAS_NODE_TYPES.transcript
    && edges.some((edge) => edge.source === scriptId && edge.target === node.id)
    && ref(node.data.artifactRef) && sameRef(ref(node.data.alignedScriptRef), scriptRef)
    && node.data.status === 'completed' && !node.data.alignmentStale);
  const aligned = narration?.data.audioUrl && media ? scriptAlignments.filter((node) =>
    edges.some((edge) => edge.source === narration.id && edge.target === node.id)
    && node.data.sourceMediaPath === media.media_path
    && node.data.sourceMediaHash === media.media_hash,
  ) : scriptAlignments;
  if (aligned.length !== 1) {
    if (scriptAlignments.length && !aligned.length && narration?.data.audioUrl && media) {
      throw new Error('当前配音音频已变化，请重新按脚本对齐。');
    }
    throw new Error(aligned.length ? '只能连接一个按当前脚本对齐的转写节点。' : '请先将转写节点连接到脚本，并按当前脚本对齐。');
  }
  return { sourceIds: [scriptId, aligned[0].id] };
}

export function planComposeFromTimeline(nodes: CanvasNode[], edges: CanvasEdge[], timelineId: string): { sourceIds: string[] } {
  const timeline = nodes.find((node) => node.id === timelineId && node.type === CANVAS_NODE_TYPES.semanticTimeline);
  if (!ref(timeline?.data.artifactRef)) throw new Error('请先完成语义时间轴编译。');
  const timingEvents = Array.isArray(timeline?.data.events) ? timeline.data.events : [];
  if (timingEvents.some((event) => event && typeof event === 'object' && Boolean((event as { invalidReason?: unknown }).invalidReason))) {
    throw new Error('语义时间轴仍有未解决的必需事件，请先修正后重新编译。');
  }
  const upstream = ancestors(nodes, edges, timelineId);
  const script = upstream.filter((node) => node.type === CANVAS_NODE_TYPES.script && ref(node.data.remixScriptRef) && edges.some((edge) => edge.source === node.id && edge.target === timelineId));
  const words = upstream.filter((node) => node.type === CANVAS_NODE_TYPES.transcript && ref(node.data.artifactRef) && edges.some((edge) => edge.source === node.id && edge.target === timelineId));
  if (script.length !== 1 || words.length !== 1 || !sameRef(ref(words[0].data.alignedScriptRef), ref(script[0].data.remixScriptRef))) throw new Error('时间轴需要连接当前脚本和已对齐的转写节点。');
  const blueprint = upstream.filter((node) => node.type === CANVAS_NODE_TYPES.videoStory && ref(node.data.referenceBlueprintRef) && edges.some((edge) => edge.source === node.id && edge.target === script[0].id));
  if (blueprint.length !== 1) throw new Error('请先将参考故事蓝图连接到脚本。');
  const reference = upstream.filter((node) => node.type === CANVAS_NODE_TYPES.video && node.data.remixRole === 'reference' && node.data.remixMedia);
  if (reference.length !== 1) throw new Error('请先连接一个参考视频素材。');
  if (!ancestors(nodes, edges, blueprint[0].id).some((node) => node.id === reference[0].id)) throw new Error('请将参考视频连接到参考故事蓝图。');
  const narration = upstream.filter((node) => node.type === CANVAS_NODE_TYPES.audio && node.data.remixRole === 'narration' && node.data.remixMedia);
  const referenceMedia = reference[0].data.remixMedia as { has_audio?: boolean };
  if (narration.length !== 1 && (narration.length > 1 || !referenceMedia.has_audio)) throw new Error('请先连接旁白音频，或使用带声音的参考视频。');
  const transcriptAncestors = ancestors(nodes, edges, words[0].id);
  if (narration.length === 1 && !transcriptAncestors.some((node) => node.id === narration[0].id)) throw new Error('请将旁白音频连接到转写节点。');
  if (narration.length === 1 && typeof timeline?.data.compiledMediaHash !== 'string') {
    throw new Error('语义时间轴缺少配音版本信息，请重新编译。');
  }
  if (narration.length === 1
    && canonicalMediaHash((narration[0].data.remixMedia as { media_hash?: unknown }).media_hash)
      !== canonicalMediaHash(timeline?.data.compiledMediaHash)) {
    throw new Error('语义时间轴使用了旧的配音版本，请重新编译。');
  }
  if (narration.length === 0 && !transcriptAncestors.some((node) => node.id === reference[0].id)) throw new Error('请将参考视频连接到转写节点。');
  return { sourceIds: [timelineId] };
}

export function createTimelineFromScript(scriptId: string): string {
  const store = useCanvasStore.getState();
  const { sourceIds } = planTimelineFromScript(store.nodes, store.edges, scriptId);
  const existing = store.nodes.find((node) => node.type === CANVAS_NODE_TYPES.semanticTimeline
    && sourceIds.every((sourceId) => store.edges.some((edge) => edge.source === sourceId && edge.target === node.id)));
  if (existing) {
    store.updateNodeData(existing.id, { autoCompileRequested: true });
    store.setSelectedNode(existing.id);
    store.requestFocusNode(existing.id);
    return existing.id;
  }
  const childId = store.addNode(CANVAS_NODE_TYPES.semanticTimeline, store.findNodePosition(scriptId, 420, 360), {
    autoCompileRequested: true,
  });
  for (const sourceId of sourceIds) {
    if (store.addEdge(sourceId, childId)) continue;
    store.deleteNodes([childId]);
    throw new Error('语义时间轴连线失败，请检查节点连接规则。');
  }
  store.setSelectedNode(childId);
  store.requestFocusNode(childId);
  return childId;
}

export function createComposeFromTimeline(timelineId: string): string {
  const store = useCanvasStore.getState();
  const { sourceIds } = planComposeFromTimeline(store.nodes, store.edges, timelineId);
  const existing = store.nodes.find((node) => node.type === CANVAS_NODE_TYPES.videoCompose
    && sourceIds.every((sourceId) => store.edges.some((edge) => edge.source === sourceId && edge.target === node.id)));
  if (existing) {
    store.updateNodeData(existing.id, { autoPrepareRequested: true });
    store.setSelectedNode(existing.id);
    store.requestFocusNode(existing.id);
    return existing.id;
  }
  const childId = store.addNode(CANVAS_NODE_TYPES.videoCompose, store.findNodePosition(timelineId, 380, 280), {
    autoPrepareRequested: true,
  });
  for (const sourceId of sourceIds) {
    if (store.addEdge(sourceId, childId)) continue;
    store.deleteNodes([childId]);
    throw new Error('视频合成节点连线失败，请检查节点连接规则。');
  }
  store.setSelectedNode(childId);
  store.requestFocusNode(childId);
  return childId;
}
