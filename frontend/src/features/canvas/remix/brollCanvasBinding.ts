import type { CanvasNode, CanvasNodeData, CanvasNodeType } from '@/features/canvas/domain/canvasNodes';
import { CANVAS_NODE_TYPES, isExportImageNode, isImageEditNode, isImageGenNode, isUploadNode, isVideoNode } from '@/features/canvas/domain/canvasNodes';
import type { RemixUploadedMedia } from '@/api/remix';
import { projectMediaPathFromUrl } from './remixMediaPath';

interface BrollCanvasStore {
  nodes: CanvasNode[];
  addNode: (type: CanvasNodeType, position: { x: number; y: number }, data?: Partial<CanvasNodeData>) => string;
  addEdge: (source: string, target: string) => string | null;
  setSelectedNode: (id: string | null) => void;
  findNodePosition: (nodeId: string, width: number, height: number) => { x: number; y: number };
}

export interface BrollVisualAttachment {
  mediaRevision: string;
  commit: () => void;
}

export function createBrollVisualNode(store: BrollCanvasStore, scriptId: string, mode: 'generate' | 'upload', selectionId: string) {
  if (!store.nodes.some((node) => node.id === scriptId)) return null;
  const type = mode === 'generate' ? CANVAS_NODE_TYPES.imageGen : CANVAS_NODE_TYPES.upload;
  const label = mode === 'generate' ? '生成画面' : '上传画面';
  const childId = store.addNode(type, store.findNodePosition(scriptId, 420, 320), {
    displayName: `${label} · ${selectionId}`,
    remixRole: 'visual',
  });
  store.addEdge(childId, scriptId);
  store.setSelectedNode(childId);
  return { id: childId, label };
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
  const media = visual.data.remixMedia as RemixUploadedMedia | undefined;
  const currentPath = currentVisualProjectPath(project, visual);
  const sourceUrl = visualSourceUrl(visual);
  const exactUrl = Boolean(media?.url && sourceUrl && media.url === sourceUrl);
  if (!media || (!exactUrl && (!currentPath || media.media_path !== currentPath))
    || (media.media_kind !== 'image' && media.media_kind !== 'video')) {
    throw new Error(`画面素材 ${visual.id} 已变化，请重新选择`);
  }
  return media;
}

export async function attachBrollVisualMedia(
  project: string,
  visual: CanvasNode,
  attach: (project: string, mediaPath: string) => Promise<RemixUploadedMedia>,
  updateNodeData: (nodeId: string, data: Partial<CanvasNodeData>) => void,
  role: 'visual' | 'background' = 'visual',
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
