import { CANVAS_NODE_TYPES, type CanvasEdge, type CanvasNode } from '@/features/canvas/domain/canvasNodes';
import type { RemixUploadedMedia } from '@/api/remix';
import { projectMediaPathFromUrl } from './remixMediaPath';

export interface PlanMediaRef {
  node_id: string;
  media_hash: string;
  media_path: string;
  media_kind: 'image' | 'video' | 'audio';
  duration_us: number;
  has_audio: boolean;
}
export interface PlanTurn { speaker: string; text: string; start_frame?: number | null; end_frame?: number | null }
export interface PlanCue {
  id: string;
  role: 'primary' | 'overlay' | 'speech' | 'narration' | 'bgm' | 'sfx';
  description: string;
  media?: PlanMediaRef | null;
  start_frame?: number | null;
  end_frame?: number | null;
}
export interface PlanBeat { id: string; purpose: string; duration_frames: number; turns: PlanTurn[]; visuals: PlanCue[]; audio: PlanCue[] }
export interface ProductionPlan { schema_version: 1; goal: string; output: { width: number; height: number; fps: number }; beats: PlanBeat[] }
const VISUAL_TYPES = new Set<string>([CANVAS_NODE_TYPES.upload, CANVAS_NODE_TYPES.imageGen, CANVAS_NODE_TYPES.imageEdit, CANVAS_NODE_TYPES.exportImage]);

export function emptyProductionPlan(): ProductionPlan {
  return { schema_version: 1, goal: '', output: { width: 1080, height: 1920, fps: 30 }, beats: [
    { id: crypto.randomUUID(), purpose: '', duration_frames: 90,
      turns: [], visuals: [{ id: crypto.randomUUID(), role: 'primary', description: '', media: null }], audio: [] },
  ] };
}

export function planDuration(plan: ProductionPlan): number {
  return plan.beats.reduce((sum, beat) => sum + beat.duration_frames, 0) / plan.output.fps;
}

/** A bounded evidence summary for references that have not been published as a blueprint. */
export function storyAnalysisContext(story: CanvasNode | undefined): Record<string, unknown> | null {
  if (!story || story.type !== CANVAS_NODE_TYPES.videoStory) return null;
  const raw = story.data.rawResult as { video_story?: { summary?: unknown; adaptation_structure?: unknown } } | undefined;
  const structure = raw?.video_story?.adaptation_structure;
  const analyzed = structure && typeof structure === 'object' ? structure as Record<string, unknown> : null;
  const role = typeof analyzed?.narrative_role === 'string' ? analyzed.narrative_role.trim().slice(0, 500) : '';
  const beats = Array.isArray(analyzed?.reusable_beats)
    ? analyzed.reusable_beats.filter((beat): beat is string => typeof beat === 'string' && Boolean(beat.trim()))
      .map((beat) => beat.trim().slice(0, 120)).slice(0, 12)
    : [];
  return {
    summary: typeof raw?.video_story?.summary === 'string' ? raw.video_story.summary.slice(0, 1200) : '',
    ...(role || beats.length ? { adaptation_structure: { narrative_role: role, reusable_beats: beats } } : {}),
  };
}

export function planMediaCandidates(project: string, nodes: CanvasNode[], planNodeId: string) {
  return nodes.flatMap((node) => {
    if (node.id === planNodeId) return [];
    const kind = node.type === CANVAS_NODE_TYPES.audio ? 'audio'
      : node.type === CANVAS_NODE_TYPES.video ? 'video'
      : VISUAL_TYPES.has(node.type ?? '') ? 'image' : null;
    if (!kind) return [];
    const media = node.data.remixMedia as RemixUploadedMedia | undefined;
    const sourceUrl = kind === 'audio' ? node.data.audioUrl : kind === 'video' ? node.data.videoUrl : node.data.imageUrl ?? node.data.previewImageUrl;
    const currentPath = typeof sourceUrl === 'string' ? projectMediaPathFromUrl(project, sourceUrl) : null;
    if (!currentPath && !media) return [];
    return [{ node, kind, label: String(node.data.displayName || node.id), currentPath,
      media: media && (!currentPath || media.media_path === currentPath) ? media : null }];
  });
}

export function planMediaRef(nodeId: string, media: RemixUploadedMedia): PlanMediaRef {
  return { node_id: nodeId, media_hash: media.media_hash, media_path: media.media_path,
    media_kind: media.media_kind, duration_us: media.duration_us, has_audio: media.has_audio === true };
}

export function validateProductionPlanBindings(project: string, plan: ProductionPlan, nodes: CanvasNode[], edges: CanvasEdge[], planNodeId: string): void {
  for (const beat of plan.beats) for (const cue of [...beat.visuals, ...beat.audio]) {
    const reference = cue.media;
    if (!reference) continue;
    const node = nodes.find((item) => item.id === reference.node_id);
    if (!node || !edges.some((edge) => edge.source === node.id && edge.target === planNodeId)) {
      throw new Error(`素材 ${cue.description || cue.id} 未连接到作品方案`);
    }
    const candidate = planMediaCandidates(project, [node], planNodeId)[0];
    const media = candidate?.media;
    if (node.data.remixVisualOutputCurrent === false || !media || media.media_hash !== reference.media_hash
      || media.media_path !== reference.media_path || media.media_kind !== reference.media_kind
      || media.duration_us !== reference.duration_us || (media.has_audio === true) !== reference.has_audio) {
      throw new Error(`素材 ${cue.description || cue.id} 已变化，请在方案里重新绑定`);
    }
  }
}
