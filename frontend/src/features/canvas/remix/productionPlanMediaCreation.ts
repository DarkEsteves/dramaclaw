import { CANVAS_NODE_TYPES, type CanvasNodeData, type CanvasNodeType } from '@/features/canvas/domain/canvasNodes';
import type { PlanBeat, PlanCue } from './productionPlanModel';
import { planCueKey, type PlanCueKind, type PlanPendingBindings } from './productionPlanReadiness';

type MaterialMode = 'image' | 'video' | 'upload' | 'audio';

interface MaterialStore {
  nodes: Array<{ id: string; data: Record<string, unknown> }>;
  addNode: (type: CanvasNodeType, position: { x: number; y: number }, data?: Partial<CanvasNodeData>) => string;
  addEdge: (source: string, target: string) => string | null;
  deleteNodes: (ids: string[]) => void;
  updateNodeData: (id: string, patch: Record<string, unknown>) => void;
  findNodePosition: (nodeId: string, width: number, height: number) => { x: number; y: number };
  setSelectedNode: (id: string | null) => void;
  requestFocusNode: (id: string) => void;
}

/** Create a material node and remember which plan cue should receive its result. */
export function createPlanMaterialNode(store: MaterialStore, planId: string, beat: PlanBeat, cue: PlanCue,
  kind: PlanCueKind, mode: MaterialMode, fps: number): string {
  const planNode = store.nodes.find((node) => node.id === planId);
  if (!planNode) throw new Error('作品方案节点已不存在');
  if (kind === 'audio' ? mode !== 'audio' : mode === 'audio') throw new Error('素材类型与当前条目不匹配');
  const title = cue.description.trim() || beat.purpose.trim() || '未命名素材';
  const durationSec = Math.max(4, Math.min(12, Math.round(beat.duration_frames / fps)));
  const type = mode === 'image' ? CANVAS_NODE_TYPES.imageGen : mode === 'video' ? CANVAS_NODE_TYPES.video
    : mode === 'upload' ? CANVAS_NODE_TYPES.upload : CANVAS_NODE_TYPES.audio;
  const data: Partial<CanvasNodeData> = mode === 'image'
    ? { displayName: `生成画面 · ${title}`, remixRole: 'visual', prompt: `${cue.description.trim() || beat.purpose.trim()}。竖屏画面，不要文字、水印或标志。` }
    : mode === 'video'
      ? { displayName: `生成视频 · ${title}`, remixRole: 'visual', aspectRatio: '9:16', genMode: 'textToVideo',
          durationSec, generateAudio: false, prompt: `${cue.description.trim() || beat.purpose.trim()}。竖屏动态镜头，不要字幕、文字或水印。` }
      : mode === 'upload'
        ? { displayName: `上传画面 · ${title}`, remixRole: 'visual' }
        : cue.role === 'bgm'
          ? { displayName: `制作音乐 · ${title}`, remixRole: 'bgm', audioKind: 'music', text: cue.description,
              musicLengthMs: Math.round(beat.duration_frames / fps * 1000) }
          : cue.role === 'sfx'
            ? { displayName: `上传音效 · ${title}`, remixRole: 'sfx', audioKind: 'speech' }
            : { displayName: `制作人声 · ${title}`, remixRole: 'narration', audioKind: 'speech',
                text: beat.turns.map((turn) => turn.text).filter(Boolean).join('\n') };
  const nodeId = store.addNode(type, store.findNodePosition(planId, 420, 320), data);
  if (!store.addEdge(nodeId, planId)) {
    store.deleteNodes([nodeId]);
    throw new Error('无法连接素材节点到作品方案');
  }
  const pending = (planNode.data.productionPlanPendingBindings as PlanPendingBindings | undefined) ?? {};
  store.updateNodeData(planId, {
    productionPlanPendingBindings: { ...pending, [planCueKey(beat.id, kind, cue.id)]: nodeId },
  });
  store.setSelectedNode(nodeId);
  store.requestFocusNode(nodeId);
  return nodeId;
}
