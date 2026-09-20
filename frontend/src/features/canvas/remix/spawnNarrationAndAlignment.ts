import { attachRemixMedia } from '@/api/remix';
import { CANVAS_NODE_TYPES, type CanvasNode } from '@/features/canvas/domain/canvasNodes';
import { projectMediaPathFromUrl } from './remixMediaPath';
import type { UploadedRemixMedia } from './workbenchModel';
import { readUrl } from '@/lib/url-params';
import { useCanvasStore } from '@/stores/canvasStore';

type Ref = { artifactId: string; revision: number };

function validRef(value: unknown): Ref | null {
  if (!value || typeof value !== 'object') return null;
  const ref = value as Record<string, unknown>;
  return typeof ref.artifactId === 'string' && Number.isInteger(ref.revision) && Number(ref.revision) > 0
    ? { artifactId: ref.artifactId, revision: Number(ref.revision) } : null;
}

function sameRef(left: Ref | null, right: Ref | null): boolean {
  return Boolean(left && right && left.artifactId === right.artifactId && left.revision === right.revision);
}

function connectedScriptForAudio(audioId: string): { node: CanvasNode; ref: Ref } {
  const { nodes, edges } = useCanvasStore.getState();
  const parents = nodes.filter((node) => node.type === CANVAS_NODE_TYPES.script
    && edges.some((edge) => edge.source === node.id && edge.target === audioId));
  if (parents.length !== 1) throw new Error('旁白音频需要连接一份改编脚本。');
  const ref = validRef(parents[0].data.remixScriptRef);
  if (!ref) throw new Error('请先保存改编脚本。');
  return { node: parents[0], ref };
}

/** Continue a saved script into one editable narration node for that exact version. */
export function createNarrationFromScript(scriptId: string): string {
  const store = useCanvasStore.getState();
  const script = store.nodes.find((node) => node.id === scriptId && node.type === CANVAS_NODE_TYPES.script);
  if (!script) throw new Error('未找到改编脚本节点。');
  const scriptRef = validRef(script.data.remixScriptRef);
  if (!scriptRef) throw new Error('请先保存改编脚本。');
  const dialogue = (script.data.remixScriptDraft as { dialogue?: unknown } | undefined)?.dialogue;
  if (typeof dialogue !== 'string' || !dialogue.trim()) throw new Error('已保存脚本缺少台词，请重新打开脚本确认。');
  const existing = store.nodes.find((node) => node.type === CANVAS_NODE_TYPES.audio
    && node.data.remixRole === 'narration' && sameRef(validRef(node.data.remixScriptRef), scriptRef)
    && store.edges.some((edge) => edge.source === scriptId && edge.target === node.id));
  if (existing) {
    store.setSelectedNode(existing.id);
    store.requestFocusNode(existing.id);
    return existing.id;
  }
  const childId = store.addNode(CANVAS_NODE_TYPES.audio, store.findNodePosition(scriptId, 480, 210), {
    audioKind: 'speech', remixRole: 'narration', remixScriptRef: scriptRef, text: dialogue,
  });
  if (!store.addEdge(scriptId, childId)) {
    store.deleteNodes([childId]);
    throw new Error('连接旁白节点失败，请检查画布连线规则。');
  }
  store.setSelectedNode(childId);
  store.requestFocusNode(childId);
  return childId;
}

/** Attach project-local audio and continue into alignment; the transcript mounts and submits the job. */
async function createAlignedTranscript(audioId: string): Promise<string> {
  const project = readUrl().project;
  if (!project) throw new Error('请先打开项目画布。');
  const initial = useCanvasStore.getState();
  const audio = initial.nodes.find((node) => node.id === audioId && node.type === CANVAS_NODE_TYPES.audio);
  if (!audio || audio.data.remixRole !== 'narration') throw new Error('请选择改编脚本的旁白音频节点。');
  const { node: script, ref: scriptRef } = connectedScriptForAudio(audioId);
  if (!sameRef(validRef(audio.data.remixScriptRef), scriptRef)) throw new Error('旁白音频不属于当前脚本版本，请重新制作配音。');
  const url = audio.data.audioUrl;
  if (typeof url !== 'string' || !url) throw new Error('请先生成或上传新脚本的配音。');
  const path = projectMediaPathFromUrl(project, url);
  let media = audio.data.remixMedia as UploadedRemixMedia | undefined;
  if (!path && (!media || media.url !== url)) throw new Error('当前配音尚未保存到项目，请上传项目内音频。');
  if (!media || (path && media.media_path !== path)) {
    if (!path) throw new Error('当前配音尚未保存到项目，请上传项目内音频。');
    media = await attachRemixMedia(project, path);
    const latestAudio = useCanvasStore.getState().nodes.find((node) => node.id === audioId);
    if (!latestAudio || latestAudio.data.audioUrl !== url) throw new Error('配音已变化，请重新点击按脚本对齐。');
    useCanvasStore.getState().updateNodeData(audioId, { remixMedia: media });
  }
  if (media.media_kind !== 'audio' || media.has_audio === false) throw new Error('当前文件不是可用配音音频。');
  const store = useCanvasStore.getState();
  if (!sameRef(validRef(store.nodes.find((node) => node.id === script.id)?.data.remixScriptRef), scriptRef)) throw new Error('脚本已更新，请重新制作配音。');
  const existing = store.nodes.find((node) => node.type === CANVAS_NODE_TYPES.transcript
    && node.data.sourceMediaPath === media.media_path && node.data.sourceMediaHash === media.media_hash
    && store.edges.some((edge) => edge.source === audioId && edge.target === node.id)
    && store.edges.some((edge) => edge.source === script.id && edge.target === node.id));
  if (existing) {
    const current = existing.data.status === 'completed'
      && sameRef(validRef(existing.data.alignedScriptRef), scriptRef) && !existing.data.alignmentStale;
    if (!current && !existing.data.operationId) store.updateNodeData(existing.id, { autoStartMode: 'align' });
    store.setSelectedNode(existing.id);
    store.requestFocusNode(existing.id);
    return existing.id;
  }
  const childId = store.addNode(CANVAS_NODE_TYPES.transcript, store.findNodePosition(audioId, 420, 300));
  if (!store.addEdge(audioId, childId) || !store.addEdge(script.id, childId)) {
    store.deleteNodes([childId]);
    throw new Error('转写节点连线失败，请检查画布连线规则。');
  }
  store.updateNodeData(childId, {
    sourceMediaPath: media.media_path, sourceMediaHash: media.media_hash,
    pendingScriptRef: scriptRef, autoStartMode: 'align',
  });
  store.setSelectedNode(childId);
  store.requestFocusNode(childId);
  return childId;
}

const alignmentInFlight = new Map<string, Promise<string>>();

export function createAlignedTranscriptFromAudio(audioId: string): Promise<string> {
  const pending = alignmentInFlight.get(audioId);
  if (pending) return pending;
  const started = createAlignedTranscript(audioId).finally(() => alignmentInFlight.delete(audioId));
  alignmentInFlight.set(audioId, started);
  return started;
}
