import { attachRemixMedia } from '@/api/remix';
import { CANVAS_NODE_TYPES } from '@/features/canvas/domain/canvasNodes';
import { projectMediaPathFromUrl } from '@/features/canvas/remix/remixMediaPath';
import type { UploadedRemixMedia } from '@/features/canvas/remix/workbenchModel';
import { readUrl } from '@/lib/url-params';
import { useCanvasStore } from '@/stores/canvasStore';

const inFlight = new Map<string, Promise<string>>();

async function createTranscript(sourceNodeId: string, autoStart: boolean): Promise<string> {
  const project = readUrl().project;
  if (!project) throw new Error('请先打开项目画布');
  const store = useCanvasStore.getState();
  const source = store.nodes.find((node) => node.id === sourceNodeId);
  if (!source || (source.type !== CANVAS_NODE_TYPES.audio && source.type !== CANVAS_NODE_TYPES.video)) throw new Error('请选择音频或视频节点');
  const isAudio = source.type === CANVAS_NODE_TYPES.audio;
  const url = isAudio ? source.data.audioUrl : source.data.videoUrl;
  if (typeof url !== 'string' || !url) throw new Error(isAudio ? '请先上传或生成音频' : '请先上传或生成视频');
  let media = source.data.remixMedia as UploadedRemixMedia | undefined;
  const path = projectMediaPathFromUrl(project, url);
  if (!path && (!media || media.url !== url)) throw new Error('当前素材尚未保存到项目，请先在“用途”中选择本地文件');
  if (!media || (path && media.media_path !== path)) {
    if (!path) throw new Error('当前素材尚未保存到项目，请先在“用途”中选择本地文件');
    media = await attachRemixMedia(project, path);
    const latest = useCanvasStore.getState().nodes.find((node) => node.id === sourceNodeId);
    if (!latest || (isAudio ? latest.data.audioUrl : latest.data.videoUrl) !== url) throw new Error('源素材已变化，请重新点击转写');
    useCanvasStore.getState().updateNodeData(sourceNodeId, { remixMedia: media });
  }
  if (media.media_kind !== (isAudio ? 'audio' : 'video')) throw new Error('素材类型不匹配');
  if (!media.has_audio) throw new Error('当前视频没有声音，无法转写');

  const current = useCanvasStore.getState();
  const existing = current.edges.find((edge) => edge.source === sourceNodeId && current.nodes.some((node) => node.id === edge.target && node.type === CANVAS_NODE_TYPES.transcript && node.data.sourceMediaPath === media.media_path && node.data.sourceMediaHash === media.media_hash));
  if (existing) {
    const child = current.nodes.find((node) => node.id === existing.target);
    if (autoStart && child && Array.isArray(child.data.words) && child.data.words.length === 0 && !child.data.operationId) {
      current.updateNodeData(child.id, { autoStartMode: 'transcribe' });
    }
    return existing.target;
  }
  const child = current.addNode(CANVAS_NODE_TYPES.transcript, current.findNodePosition(sourceNodeId, 480, 220));
  if (!current.addEdge(sourceNodeId, child)) {
    current.deleteNode(child);
    throw new Error('连接转写节点失败');
  }
  current.updateNodeData(child, { sourceMediaPath: media.media_path, sourceMediaHash: media.media_hash, autoStartMode: autoStart ? 'transcribe' : null });
  return child;
}

/** Create a result node from an audio/video source and start transcription once it mounts. */
export function startSourceTranscript(sourceNodeId: string, options: { autoStart?: boolean } = {}): Promise<string> {
  const autoStart = options.autoStart !== false;
  const key = `${sourceNodeId}:${autoStart}`;
  const pending = inFlight.get(key);
  if (pending) return pending;
  const started = createTranscript(sourceNodeId, autoStart).finally(() => inFlight.delete(key));
  inFlight.set(key, started);
  return started;
}
