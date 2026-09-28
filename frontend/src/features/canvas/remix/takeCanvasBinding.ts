import { CANVAS_NODE_TYPES, isAudioNode, isExportImageNode, isImageEditNode, isImageGenNode, isStoryboardGenNode, isUploadNode, isVideoNode, type CanvasEdge, type CanvasNode, type CanvasNodeData, type CanvasNodeType } from '@/features/canvas/domain/canvasNodes';
import { sortUpstreamByReferenceOrder, upstreamNodesInEdgeOrder } from '@/features/canvas/nodes/referenceOrdering';
import type { RemixUploadedMedia } from '@/api/remix';
import { projectMediaPathFromUrl } from './remixMediaPath';
import { referenceBindingsForSegment, spokenTextForSegment, type RemixPerformanceSegment, type RemixReferenceBinding, type RemixScriptDraft } from './remixScriptModel';

interface Store {
  nodes: CanvasNode[];
  addNode: (type: CanvasNodeType, position: { x: number; y: number }, data?: Partial<CanvasNodeData>) => string;
  addEdge: (source: string, target: string) => string | null;
  setSelectedNode: (id: string | null) => void;
  findNodePosition: (nodeId: string, width: number, height: number) => { x: number; y: number };
}

function referenceTitle(kind: 'person' | 'product' | 'setting' | 'generic', mode: 'upload' | 'generate', segment: RemixPerformanceSegment, segmentIndex: number): string {
  const label = { person: '人物', product: '产品', setting: '场景', generic: '素材' }[kind];
  const speaker = segment.speaker.trim() || '未命名角色';
  return `${mode === 'upload' ? '上传' : '生成'}${label}参考 · ${String(segmentIndex + 1).padStart(2, '0')} ${speaker}`;
}

export function legacyPerformanceReferenceLabelPatches(nodes: CanvasNode[], edges: CanvasEdge[], scriptId: string,
  segments: RemixPerformanceSegment[]): Array<{ id: string; displayName: string; remixSegmentId: string }> {
  const patches: Array<{ id: string; displayName: string; remixSegmentId: string }> = [];
  const seen = new Set<string>();
  const fields = [['personNodeId', 'person'], ['productNodeId', 'product'], ['settingNodeId', 'setting']] as const;
  segments.forEach((segment, segmentIndex) => {
    for (const [field, kind] of fields) {
      const id = segment[field];
      if (!id || seen.has(id)) continue;
      const node = nodes.find((candidate) => candidate.id === id);
      const label = { person: '人物', product: '产品', setting: '场景' }[kind];
      const mode = node?.data.displayName === `上传${label}参考` ? 'upload' : 'generate';
      if (!node || node.data.remixRole !== 'identity' || node.data.remixIdentityKind !== kind
        || node.data.displayName !== `${mode === 'upload' ? '上传' : '生成'}${label}参考`
        || !edges.some((edge) => edge.source === id && edge.target === scriptId)) continue;
      seen.add(id);
      patches.push({ id, displayName: referenceTitle(kind, mode, segment, segmentIndex), remixSegmentId: segment.id });
    }
  });
  return patches;
}

export function createPerformanceReferenceNode(store: Store, scriptId: string, kind: 'person' | 'product' | 'setting' | 'generic', mode: 'upload' | 'generate',
  segment: RemixPerformanceSegment, segmentIndex: number, scope: 'segment' | 'speaker' = 'segment') {
  const label = { person: '人物', product: '产品', setting: '场景', generic: '素材' }[kind];
  const type = mode === 'upload' ? CANVAS_NODE_TYPES.upload : CANVAS_NODE_TYPES.imageGen;
  const title = scope === 'speaker'
    ? `${mode === 'upload' ? '上传' : '生成'}${label}参考 · ${segment.speaker.trim() || '未命名角色'}（共用）`
    : referenceTitle(kind, mode, segment, segmentIndex);
  const copies = store.nodes.filter((node) => node.data.displayName === title
    || (typeof node.data.displayName === 'string' && node.data.displayName.startsWith(`${title} · `))).length;
  const id = store.addNode(type, store.findNodePosition(scriptId, 420, 300), {
    displayName: copies ? `${title} · ${copies + 1}` : title,
    ...(kind === 'generic' ? { remixRole: 'reference_asset' } : { remixRole: 'identity', remixIdentityKind: kind }),
    ...(scope === 'segment' ? { remixSegmentId: segment.id } : {}),
    ...(mode === 'generate' ? { prompt: `竖屏视频${label}视觉参考图，真实摄影质感，不要文字、水印或标志。` } : {}),
  });
  store.addEdge(id, scriptId);
  store.setSelectedNode(id);
  return id;
}

export function createPerformanceTakeNode(store: Store, scriptId: string, segment: RemixPerformanceSegment, dialogue: string, topic: string, referenceObservation = '') {
  if (!store.nodes.some((node) => node.id === scriptId)) throw new Error('脚本节点不存在');
  const caption = spokenTextForSegment(dialogue, segment).trim();
  if (!caption) throw new Error('当前表演段落没有台词');
  const name = `${segment.speaker} · ${caption.slice(0, 16)}`;
  const isVoiceover = segment.source === 'voiceover';
  const isOriginalSound = segment.source === 'original_sound';
  const bindings = referenceBindingsForSegment(segment).filter((binding) => store.nodes.some((node) => node.id === binding.nodeId));
  const refs = bindings.map((binding) => binding.nodeId);
  const speakerTurns = segment.speechRanges?.length && segment.speechRanges.length > 1
    ? segment.speechRanges.map(({ start, end }) => {
      const chars = Array.from(dialogue);
      const lineStart = chars.lastIndexOf('\n', start - 1) + 1;
      const label = /^\s*([^：:\n]{1,24})[：:]\s*$/u.exec(chars.slice(lineStart, start).join(''))?.[1]?.trim();
      return `${label || segment.speaker}：${chars.slice(start, end).join('')}`;
    }).join('\n') : '';
  const prompt = [
    '生成竖屏 9:16 有声视频，按台词安排画面中的说话者，画面和声音来自同一个视频片段。',
    topic ? `新主题：${topic}。` : '',
    `说话者：${segment.speaker}。逐字台词：${caption}`,
    speakerTurns ? `按下列顺序由对应说话者发言：\n${speakerTurns}` : '',
    referenceObservation ? `原片对应镜头观察：${referenceObservation}。按台词和所选参考决定需要保留的画面细节。` : '',
    ...bindings.map(({ nodeId, purpose }, index) => {
      const ref = store.nodes.find((node) => node.id === nodeId)!;
      return `参考 ${index + 1}（${String(ref.data.displayName || ref.id)}）${purpose.trim() ? `：${purpose.trim()}` : ''}。`;
    }),
    '画面不要字幕、文字、水印和背景音乐。',
  ].filter(Boolean).join('\n');
  const id = store.addNode(isVoiceover ? CANVAS_NODE_TYPES.audio : CANVAS_NODE_TYPES.video,
    store.findNodePosition(scriptId, 420, 320), isVoiceover
      ? { displayName: `独立旁白 · ${name}`, remixRole: 'take_audio', remixSegmentId: segment.id, audioKind: 'speech', text: caption,
          remixReferenceBindings: bindings, remixReferenceNodeIds: refs }
      : isOriginalSound ? { displayName: `上传原声片段 · ${name}`, remixRole: 'performance', remixSegmentId: segment.id, referenceOnly: true }
      : { displayName: `口播视频 · ${name}`, remixRole: 'performance', remixSegmentId: segment.id,
          remixReferenceNodeIds: refs,
          remixReferenceBindings: bindings,
          referenceOrder: refs,
          aspectRatio: '9:16', genMode: refs.length ? 'allReference' : 'textToVideo', durationSec: Array.from(caption).length > 24 ? 10 : 5,
          generateAudio: true, prompt });
  for (const refId of refs) store.addEdge(refId, id);
  store.addEdge(id, scriptId);
  store.setSelectedNode(id);
  return id;
}

export function performanceSourceUrl(node: CanvasNode): string | null {
  if (node.type === CANVAS_NODE_TYPES.video) return typeof node.data.videoUrl === 'string' ? node.data.videoUrl : null;
  if (node.type === CANVAS_NODE_TYPES.audio) return typeof node.data.audioUrl === 'string' ? node.data.audioUrl : null;
  return null;
}

/** The same ordered incoming media nodes that a video node uses when submitting a generation. */
export function takeCanvasReferenceIds(take: CanvasNode, nodes: CanvasNode[], edges: CanvasEdge[]): string[] {
  const referenceOrder = Array.isArray(take.data.referenceOrder)
    ? take.data.referenceOrder.filter((id): id is string => typeof id === 'string') : undefined;
  const ordered = sortUpstreamByReferenceOrder(upstreamNodesInEdgeOrder(nodes, edges, take.id), referenceOrder);
  return [...new Set(ordered.filter((node) => isAudioNode(node) || isVideoNode(node) || isUploadNode(node)
    || isImageEditNode(node) || isImageGenNode(node) || isExportImageNode(node) || isStoryboardGenNode(node)).map((node) => node.id))];
}

export function takeCanvasReferenceBindings(take: CanvasNode, segment: RemixPerformanceSegment,
  nodes: CanvasNode[], edges: CanvasEdge[]): RemixReferenceBinding[] {
  const annotated = new Map<string, string>();
  for (const binding of Array.isArray(take.data.remixReferenceBindings) ? take.data.remixReferenceBindings : [])
    annotated.set(binding.nodeId, binding.purpose);
  for (const binding of referenceBindingsForSegment(segment)) annotated.set(binding.nodeId, binding.purpose);
  return takeCanvasReferenceIds(take, nodes, edges).map((nodeId) => ({ nodeId, purpose: annotated.get(nodeId) ?? '' }));
}

export function syncPerformanceReferenceSegment(segment: RemixPerformanceSegment, nodes: CanvasNode[], edges: CanvasEdge[]): RemixPerformanceSegment {
  const take = nodes.find((node) => node.id === segment.takeNodeId);
  if (!take) return segment;
  const referenceBindings = takeCanvasReferenceBindings(take, segment, nodes, edges);
  const prior = referenceBindingsForSegment(segment);
  if (JSON.stringify(prior) === JSON.stringify(referenceBindings)) return segment;
  const inputsChanged = JSON.stringify(prior.map(({ nodeId }) => nodeId)) !== JSON.stringify(referenceBindings.map(({ nodeId }) => nodeId));
  return { ...segment, referenceBindings, ...(inputsChanged ? { takeMediaRevision: undefined } : {}) };
}

export function syncPerformanceReferenceDraft(draft: RemixScriptDraft, nodes: CanvasNode[], edges: CanvasEdge[]): RemixScriptDraft {
  if (!draft.segments?.length) return draft;
  const segments = draft.segments.map((segment) => syncPerformanceReferenceSegment(segment, nodes, edges));
  return segments.every((segment, index) => segment === draft.segments?.[index]) ? draft : { ...draft, segments };
}

export function takeReferenceSnapshot(nodes: CanvasNode[], ids: string[]) {
  return ids.map((id) => {
    const node = nodes.find((candidate) => candidate.id === id);
    const data = node?.data as { videoUrl?: string; audioUrl?: string; previewImageUrl?: string; imageUrl?: string; referenceImageUrl?: string } | undefined;
    const rawUrl = data?.videoUrl || data?.audioUrl || data?.imageUrl || data?.referenceImageUrl || data?.previewImageUrl;
    const media = node?.data.remixMedia as { media_hash?: unknown } | undefined;
    return { id, url: typeof rawUrl === 'string' ? rawUrl : null,
      hash: typeof media?.media_hash === 'string' ? media.media_hash : null };
  });
}

/** One-time migration for takes generated before the video node recorded its submitted references. */
export function legacyTakeReferencePatch(take: CanvasNode, segment: RemixPerformanceSegment,
  nodes: CanvasNode[], edges: CanvasEdge[]): Partial<CanvasNodeData> | null {
  const sourceUrl = performanceSourceUrl(take);
  if (!sourceUrl || take.data.remixGenerationReferenceUrl) return null;
  const remixReferenceBindings = takeCanvasReferenceBindings(take, segment, nodes, edges);
  const remixReferenceNodeIds = remixReferenceBindings.map((binding) => binding.nodeId);
  return { remixReferenceBindings, remixReferenceNodeIds,
    remixReferenceSnapshot: takeReferenceSnapshot(nodes, remixReferenceNodeIds), remixGenerationReferenceUrl: sourceUrl };
}

export function takeReferencesCurrent(nodes: CanvasNode[], ids: string[], saved: unknown): boolean {
  if (!Array.isArray(saved) || saved.length !== ids.length) return false;
  const current = takeReferenceSnapshot(nodes, ids);
  return current.every((item, index) => {
    const prior = saved[index];
    return prior && typeof prior === 'object' && prior.id === item.id && prior.url === item.url
      && (prior.hash === null ? true : prior.hash === item.hash);
  });
}

export function takeBindingsCurrent(take: CanvasNode, segment: RemixPerformanceSegment): boolean {
  const expected = referenceBindingsForSegment(segment).map((binding) => binding.nodeId);
  const saved = take.data.remixReferenceNodeIds;
  if (Array.isArray(saved)) return JSON.stringify(saved) === JSON.stringify(expected);
  const legacy = take.data.remixReferenceBindings;
  return JSON.stringify(Array.isArray(legacy) ? legacy.map((binding) => binding.nodeId) : []) === JSON.stringify(expected);
}

export function takeReferencesConnected(take: CanvasNode, segment: RemixPerformanceSegment, nodes: CanvasNode[], edges: CanvasEdge[]): boolean {
  return JSON.stringify(takeCanvasReferenceIds(take, nodes, edges))
    === JSON.stringify(referenceBindingsForSegment(segment).map((binding) => binding.nodeId));
}

/** Inputs that can make a previously failed automatic take binding worth retrying. */
export function performanceTakeAutoBindKey(take: CanvasNode, segment: RemixPerformanceSegment, nodes: CanvasNode[], edges: CanvasEdge[]): string {
  const bindings = referenceBindingsForSegment(segment);
  const savedIds = Array.isArray(take.data.remixReferenceNodeIds)
    ? take.data.remixReferenceNodeIds.filter((id): id is string => typeof id === 'string') : [];
  const referenceIds = [...new Set([...bindings.map(({ nodeId }) => nodeId), ...savedIds])];
  const media = take.data.remixMedia as { url?: unknown; media_hash?: unknown } | undefined;
  return JSON.stringify({
    segmentId: segment.id, source: segment.source, takeId: take.id,
    sourceUrl: performanceSourceUrl(take), mediaUrl: media?.url, mediaHash: media?.media_hash,
    bindings, savedBindings: take.data.remixReferenceBindings, savedIds,
    references: takeReferenceSnapshot(nodes, referenceIds).map((snapshot) => ({
      ...snapshot, connected: edges.some((edge) => edge.source === snapshot.id && edge.target === take.id),
    })),
  });
}

export function createPerformanceAutoBindRetryGate() {
  let failedKey: string | null = null;
  return {
    canAttempt: (key: string) => key !== failedKey,
    recordFailure: (key: string) => { failedKey = key; },
    recordSuccess: () => { failedKey = null; },
  };
}

export function boundPerformanceTakeMedia(project: string, node: CanvasNode, source: RemixPerformanceSegment['source']): RemixUploadedMedia {
  if (source === 'voiceover' ? node.type !== CANVAS_NODE_TYPES.audio : node.type !== CANVAS_NODE_TYPES.video) {
    throw new Error('表演片段节点类型与声音来源不匹配');
  }
  const sourceUrl = performanceSourceUrl(node);
  const media = node.data.remixMedia as RemixUploadedMedia | undefined;
  if (!sourceUrl) throw new Error('表演素材尚未生成或上传');
  if (!media) throw new Error('表演素材尚未绑定');
  const path = projectMediaPathFromUrl(project, sourceUrl);
  if ((media.url && media.url !== sourceUrl) || (!media.url && (!path || path !== media.media_path))) throw new Error('表演素材已变化，请重新绑定');
  if (!/^[a-f0-9]{64}$/u.test(media.media_hash)) throw new Error('表演素材版本无效');
  if (source === 'voiceover') {
    if (media.media_kind !== 'audio') throw new Error('独立旁白需要音频素材');
  } else if (media.media_kind !== 'video' || media.has_audio !== true) {
    throw new Error('口播片段需要带人物声音的视频');
  }
  return media;
}

export async function attachPerformanceTakeMedia(project: string, node: CanvasNode,
  attach: (project: string, mediaPath: string) => Promise<RemixUploadedMedia>, update: (id: string, patch: Partial<CanvasNodeData>) => void,
  source: RemixPerformanceSegment['source']) {
  const sourceUrl = performanceSourceUrl(node);
  const path = sourceUrl ? projectMediaPathFromUrl(project, sourceUrl) : null;
  if (!path) throw new Error('请先在片段节点生成或上传素材');
  const existing = node.data.remixMedia as RemixUploadedMedia | undefined;
  const media = await attach(project, path);
  const candidate = { ...node, data: { ...node.data, remixMedia: media } } as CanvasNode;
  boundPerformanceTakeMedia(project, candidate, source);
  if (existing?.media_hash !== media.media_hash || existing.url !== media.url) update(node.id, { remixMedia: media });
  return media;
}

export function selectedPerformanceTakes(project: string, nodes: CanvasNode[], edges: CanvasEdge[], script: CanvasNode) {
  const draft = script.data.remixScriptDraft as { segments?: RemixPerformanceSegment[] } | undefined;
  if (!draft?.segments?.length) return [];
  return draft.segments.map((segment) => {
    const take = nodes.find((node) => node.id === segment.takeNodeId);
    if (!take || !edges.some((edge) => edge.source === take.id && edge.target === script.id)) {
      throw new Error(`${segment.speaker} 缺少已连接的表演片段`);
    }
    const media = boundPerformanceTakeMedia(project, take, segment.source);
    if (segment.takeMediaRevision !== media.media_hash) throw new Error(`${segment.speaker} 的表演片段已变化，请重新绑定`);
    const referenceIds = take.data.remixReferenceNodeIds;
    if (Array.isArray(referenceIds) && referenceIds.length && !takeReferencesCurrent(nodes,
      referenceIds.filter((id): id is string => typeof id === 'string'), take.data.remixReferenceSnapshot)) {
      throw new Error(`${segment.speaker} 的参考素材已变化，请重新生成片段`);
    }
    if (!takeBindingsCurrent(take, segment)) throw new Error(`${segment.speaker} 的参考素材已变化，请重新生成片段`);
    if (!takeReferencesConnected(take, segment, nodes, edges))
      throw new Error(`${segment.speaker} 的参考素材连接已变化，请重新制作片段`);
    return { segment, node: take, media };
  });
}
