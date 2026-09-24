import { CANVAS_NODE_TYPES, type CanvasEdge, type CanvasNode, type CanvasNodeData, type CanvasNodeType } from '@/features/canvas/domain/canvasNodes';
import type { RemixUploadedMedia } from '@/api/remix';
import { projectMediaPathFromUrl } from './remixMediaPath';
import type { RemixPerformanceSegment } from './remixScriptModel';

interface Store {
  nodes: CanvasNode[];
  addNode: (type: CanvasNodeType, position: { x: number; y: number }, data?: Partial<CanvasNodeData>) => string;
  addEdge: (source: string, target: string) => string | null;
  setSelectedNode: (id: string | null) => void;
  findNodePosition: (nodeId: string, width: number, height: number) => { x: number; y: number };
}

function referenceTitle(kind: 'person' | 'product' | 'setting', mode: 'upload' | 'generate', segment: RemixPerformanceSegment, segmentIndex: number): string {
  const label = { person: '人物', product: '产品', setting: '场景' }[kind];
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

export function createPerformanceReferenceNode(store: Store, scriptId: string, kind: 'person' | 'product' | 'setting', mode: 'upload' | 'generate',
  segment: RemixPerformanceSegment, segmentIndex: number, scope: 'segment' | 'speaker' = 'segment') {
  const label = { person: '人物', product: '产品', setting: '场景' }[kind];
  const type = mode === 'upload' ? CANVAS_NODE_TYPES.upload : CANVAS_NODE_TYPES.imageGen;
  const title = scope === 'speaker'
    ? `${mode === 'upload' ? '上传' : '生成'}${label}参考 · ${segment.speaker.trim() || '未命名角色'}（共用）`
    : referenceTitle(kind, mode, segment, segmentIndex);
  const copies = store.nodes.filter((node) => node.data.displayName === title
    || (typeof node.data.displayName === 'string' && node.data.displayName.startsWith(`${title} · `))).length;
  const id = store.addNode(type, store.findNodePosition(scriptId, 420, 300), {
    displayName: copies ? `${title} · ${copies + 1}` : title,
    remixRole: 'identity', remixIdentityKind: kind, ...(scope === 'segment' ? { remixSegmentId: segment.id } : {}),
    ...(mode === 'generate' ? { prompt: `竖屏视频${label}视觉参考图，真实摄影质感，不要文字、水印或标志。` } : {}),
  });
  store.addEdge(id, scriptId);
  store.setSelectedNode(id);
  return id;
}

export function createPerformanceTakeNode(store: Store, scriptId: string, segment: RemixPerformanceSegment, dialogue: string, topic: string, referenceObservation = '') {
  if (!store.nodes.some((node) => node.id === scriptId)) throw new Error('脚本节点不存在');
  const caption = Array.from(dialogue).slice(segment.start, segment.end).join('').trim();
  if (!caption) throw new Error('当前表演段落没有台词');
  const name = `${segment.speaker} · ${caption.slice(0, 16)}`;
  const isVoiceover = segment.source === 'voiceover';
  const isOriginalSound = segment.source === 'original_sound';
  const refs = [...new Set(([segment.personNodeId, segment.productNodeId, segment.settingNodeId, segment.motionNodeId, segment.voiceNodeId] as Array<string | undefined>)
    .filter((value): value is string => Boolean(value && store.nodes.some((node) => node.id === value))))];
  const prompt = [
    '生成竖屏 9:16 真人口播视频，人物在镜头前自然说话，画面和人物声音来自同一个视频片段。',
    topic ? `新主题：${topic}。` : '',
    `说话者：${segment.speaker}。逐字台词：${caption}`,
    referenceObservation ? `原片对应镜头：${referenceObservation}。保留构图、机位、动作与剪辑节奏；人物、产品和场景按新主题及所选参考替换。` : '',
    ...(['personNodeId', 'productNodeId', 'settingNodeId', 'motionNodeId'] as const).map((key) => {
      const ref = store.nodes.find((node) => node.id === segment[key]);
      const label = { personNodeId: '人物形象', productNodeId: '产品与道具', settingNodeId: '场景环境', motionNodeId: '镜头动作' }[key];
      return ref ? `${label}参考：${String(ref.data.displayName || ref.id)}。` : '';
    }),
    '保留参考的镜头节奏，按新主题替换人物、产品和场景。画面不要字幕、文字、水印和背景音乐。',
  ].filter(Boolean).join('\n');
  const id = store.addNode(isVoiceover ? CANVAS_NODE_TYPES.audio : CANVAS_NODE_TYPES.video,
    store.findNodePosition(scriptId, 420, 320), isVoiceover
      ? { displayName: `独立旁白 · ${name}`, remixRole: 'take_audio', remixSegmentId: segment.id, audioKind: 'speech', text: caption }
      : isOriginalSound ? { displayName: `上传原声片段 · ${name}`, remixRole: 'performance', remixSegmentId: segment.id, referenceOnly: true }
      : { displayName: `口播视频 · ${name}`, remixRole: 'performance', remixSegmentId: segment.id,
          remixReferenceNodeIds: refs,
          aspectRatio: '9:16', genMode: refs.length ? 'allReference' : 'textToVideo', durationSec: Array.from(caption).length > 24 ? 10 : 5,
          generateAudio: true, prompt });
  for (const refId of refs) if (!isVoiceover) store.addEdge(refId, id);
  store.addEdge(id, scriptId);
  store.setSelectedNode(id);
  return id;
}

export function performanceSourceUrl(node: CanvasNode): string | null {
  if (node.type === CANVAS_NODE_TYPES.video) return typeof node.data.videoUrl === 'string' ? node.data.videoUrl : null;
  if (node.type === CANVAS_NODE_TYPES.audio) return typeof node.data.audioUrl === 'string' ? node.data.audioUrl : null;
  return null;
}

export function takeReferenceSnapshot(nodes: CanvasNode[], ids: string[]) {
  return ids.map((id) => {
    const node = nodes.find((candidate) => candidate.id === id);
    const data = node?.data as { videoUrl?: string; audioUrl?: string; previewImageUrl?: string; imageUrl?: string; referenceImageUrl?: string } | undefined;
    const rawUrl = data?.videoUrl || data?.audioUrl || data?.previewImageUrl || data?.imageUrl || data?.referenceImageUrl;
    const media = node?.data.remixMedia as { media_hash?: unknown } | undefined;
    return { id, url: typeof rawUrl === 'string' ? rawUrl : null,
      hash: typeof media?.media_hash === 'string' ? media.media_hash : null };
  });
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
    return { segment, node: take, media };
  });
}
