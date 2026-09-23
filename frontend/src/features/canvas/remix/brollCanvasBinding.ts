import type { CanvasEdge, CanvasNode, CanvasNodeData, CanvasNodeType } from '@/features/canvas/domain/canvasNodes';
import { CANVAS_NODE_TYPES, isExportImageNode, isImageEditNode, isImageGenNode, isUploadNode, isVideoNode } from '@/features/canvas/domain/canvasNodes';
import type { RemixUploadedMedia } from '@/api/remix';
import { projectMediaPathFromUrl } from './remixMediaPath';

interface BrollCanvasStore {
  nodes: CanvasNode[];
  edges?: CanvasEdge[];
  addNode: (type: CanvasNodeType, position: { x: number; y: number }, data?: Partial<CanvasNodeData>) => string;
  addEdge: (source: string, target: string) => string | null;
  setSelectedNode: (id: string | null) => void;
  findNodePosition: (nodeId: string, width: number, height: number) => { x: number; y: number };
}

export interface BrollVisualAttachment {
  mediaRevision: string;
  commit: () => void;
}

export function createBrollVisualNode(
  store: BrollCanvasStore,
  scriptId: string,
  mode: 'generate' | 'upload',
  selectionId: string,
  eventName: string,
  suggestedPrompt?: string,
) {
  if (!store.nodes.some((node) => node.id === scriptId)) return null;
  const type = mode === 'generate' ? CANVAS_NODE_TYPES.imageGen : CANVAS_NODE_TYPES.upload;
  const label = mode === 'generate' ? '生成画面' : '上传画面';
  const displayName = `${label} · ${eventName.trim() || selectionId}`;
  const childId = store.addNode(type, store.findNodePosition(scriptId, 420, 320), {
    displayName,
    remixRole: 'visual',
    ...(mode === 'generate' && suggestedPrompt?.trim() ? { prompt: suggestedPrompt.trim() } : {}),
  });
  store.addEdge(childId, scriptId);
  store.setSelectedNode(childId);
  return { id: childId, label: displayName };
}

export function createBackgroundVisualNode(store: BrollCanvasStore, scriptId: string) {
  if (!store.nodes.some((node) => node.id === scriptId)) return null;
  const childId = store.addNode(CANVAS_NODE_TYPES.upload, store.findNodePosition(scriptId, 420, 320), {
    displayName: '成片背景 / A-roll',
    remixRole: 'background',
  });
  store.addEdge(childId, scriptId);
  store.setSelectedNode(childId);
  return childId;
}

/** Prepare editable generation requests for the speaking shots absent from B-roll. */
export function createPrimaryVisualNodes(store: BrollCanvasStore, scriptId: string, storyId: string, eventIds: string[]): string[] {
  const script = store.nodes.find((node) => node.id === scriptId);
  const story = store.nodes.find((node) => node.id === storyId);
  if (!script || !story) return [];
  const draft = script.data.remixScriptDraft as { dialogue?: string; selections?: Array<{ id: string; label: string; start: number; end: number }> } | undefined;
  const selections = draft?.selections ?? [];
  const referenceEvents = Array.isArray(story.data.referenceEvents) ? story.data.referenceEvents as Array<Record<string, unknown>> : [];
  const scriptRef = script.data.remixScriptRef;
  const topic = typeof script.data.remixTopic === 'string' ? script.data.remixTopic.trim() : '';
  const created: string[] = [];
  for (const eventId of eventIds) {
    const index = selections.findIndex((selection) => selection.id === eventId);
    if (index < 0) continue;
    const selection = selections[index];
    const existing = store.nodes.find((node) => node.data.remixRole === 'primary' && node.data.remixEventId === eventId
      && store.edges?.some((edge) => edge.source === node.id && edge.target === scriptId)
      && (node.data.remixScriptRef as { artifactId?: string; revision?: number } | undefined)?.artifactId === (scriptRef as { artifactId?: string } | undefined)?.artifactId
      && (node.data.remixScriptRef as { revision?: number } | undefined)?.revision === (scriptRef as { revision?: number } | undefined)?.revision);
    if (existing) continue;
    const observed = referenceEvents[index];
    const model = observed?.model && typeof observed.model === 'object' ? observed.model as Record<string, unknown> : null;
    const visualObservation = [observed?.visual_observation, model?.visual_observation].find((value) => typeof value === 'string' && value.trim()) as string | undefined;
    const dialogue = Array.from(draft?.dialogue ?? '').slice(selection.start, selection.end).join('').trim();
    const prompt = [
      '竖屏 9:16 短视频主画面，真实摄影质感。根据新主题与当前台词呈现人物、场景和动作。',
      topic ? `新主题：${topic}。` : '',
      `当前段落：${selection.label}。${dialogue ? `台词：${dialogue}。` : ''}`,
      visualObservation ? `参考片镜头观察：${visualObservation}。保留镜头语言和构图节奏，人物、产品及道具按新主题重新呈现。` : '',
      '不要复制参考片的人物或品牌。画面中不要字幕、文字、水印和标志。',
    ].filter(Boolean).join('\n');
    const basePosition = store.findNodePosition(scriptId, 420, 320);
    const id = store.addNode(CANVAS_NODE_TYPES.imageGen, { x: basePosition.x, y: basePosition.y + created.length * 360 }, {
      displayName: `主画面 · ${selection.label.trim() || eventId}`,
      remixRole: 'primary', remixEventId: eventId, remixScriptRef: scriptRef,
      aspectRatio: '9:16', prompt,
    });
    store.addEdge(id, scriptId);
    created.push(id);
  }
  if (created.length) store.setSelectedNode(created[0]);
  return created;
}

function visualSourceUrl(node: CanvasNode): string | null {
  if (isVideoNode(node)) return node.data.videoUrl;
  if (isUploadNode(node) || isImageEditNode(node) || isImageGenNode(node) || isExportImageNode(node)) {
    return node.data.imageUrl || node.data.previewImageUrl || null;
  }
  return null;
}

function currentVisualProjectPath(project: string, visual: CanvasNode): string | null {
  const sourceUrl = visualSourceUrl(visual);
  return typeof sourceUrl === 'string' ? projectMediaPathFromUrl(project, sourceUrl) : null;
}

export function boundBrollVisualMedia(project: string, visual: CanvasNode): RemixUploadedMedia {
  const name = typeof visual.data.displayName === 'string' && visual.data.displayName.trim()
    ? visual.data.displayName.trim() : `画面素材 ${visual.id}`;
  if (visual.data.remixVisualOutputCurrent === false) {
    throw new Error(`${name} 的生成提示已变化，请重新生成`);
  }
  const media = visual.data.remixMedia as RemixUploadedMedia | undefined;
  const currentPath = currentVisualProjectPath(project, visual);
  const sourceUrl = visualSourceUrl(visual);
  if (!sourceUrl && !media) throw new Error(visual.type === CANVAS_NODE_TYPES.imageGen
    ? `${name} 尚未生成，请先生成画面` : `${name} 尚未上传，请先上传图片或视频`);
  if (sourceUrl && !media) throw new Error(`${name} 尚未绑定，请先绑定素材`);
  const exactUrl = Boolean(media?.url && sourceUrl && media.url === sourceUrl);
  if (!media || typeof media.media_hash !== 'string' || !/^[a-f0-9]{64}$/u.test(media.media_hash)
    || (!exactUrl && (!currentPath || media.media_path !== currentPath))
    || (media.media_kind !== 'image' && media.media_kind !== 'video')) {
    throw new Error(`${name} 已变化，请重新选择`);
  }
  return media;
}

export async function attachBrollVisualMedia(
  project: string,
  visual: CanvasNode,
  attach: (project: string, mediaPath: string) => Promise<RemixUploadedMedia>,
  updateNodeData: (nodeId: string, data: Partial<CanvasNodeData>) => void,
  role: 'visual' | 'background' | 'primary' = 'visual',
): Promise<BrollVisualAttachment> {
  const mediaPath = currentVisualProjectPath(project, visual);
  if (!mediaPath) throw new Error('请先在素材节点完成生成或上传');
  const existing = visual.data.remixMedia;
  if (existing && typeof existing === 'object' && 'media_path' in existing && existing.media_path === mediaPath
    && 'media_hash' in existing && typeof existing.media_hash === 'string') {
    return { mediaRevision: existing.media_hash, commit: () => {} };
  }
  const media = await attach(project, mediaPath);
  if (media.media_kind !== 'image' && media.media_kind !== 'video') throw new Error('B-roll 只支持图片或视频');
  return {
    mediaRevision: media.media_hash,
    commit: () => updateNodeData(visual.id, { remixMedia: media, remixRole: role }),
  };
}
