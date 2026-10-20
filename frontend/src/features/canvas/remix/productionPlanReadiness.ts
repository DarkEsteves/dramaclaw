import type { CanvasEdge, CanvasNode } from '@/features/canvas/domain/canvasNodes';
import type { PlanBeat, PlanCue, ProductionPlan } from './productionPlanModel';
import { planMediaCandidates } from './productionPlanModel';

export type PlanCueKind = 'visuals' | 'audio';
export type PlanMediaCandidate = ReturnType<typeof planMediaCandidates>[number];
export type PlanPendingBindings = Record<string, string>;

export interface PlanReadinessIssue {
  kind: 'goal' | 'purpose' | 'turn' | 'coverage' | 'timing' | 'length' | PlanCueKind;
  status: 'missing' | 'stale' | 'creating' | 'invalid';
  beatIndex: number;
  cueIndex?: number;
  key: string;
  reason: string;
  nodeId?: string;
}

export interface PlanReadiness {
  issues: PlanReadinessIssue[];
  visualReady: number;
  visualTotal: number;
  audioReady: number;
  audioTotal: number;
}

export function planCueKey(beatId: string, kind: PlanCueKind, cueId: string): string {
  return `${beatId}:${kind}:${cueId}`;
}

/** Give each primary visual a continuous span; keep overlays on their own timing. */
export function distributePrimaryVisuals(beat: PlanBeat): void {
  const primaries = beat.visuals.filter((cue) => cue.role === 'primary');
  if (!primaries.length) return;
  primaries.forEach((cue, index) => {
    cue.start_frame = Math.floor(beat.duration_frames * index / primaries.length);
    cue.end_frame = Math.floor(beat.duration_frames * (index + 1) / primaries.length);
  });
}

function hasPrimaryCoverage(beat: PlanBeat): boolean {
  const primaries = beat.visuals.filter((cue) => cue.role === 'primary')
    .sort((left, right) => (left.start_frame ?? 0) - (right.start_frame ?? 0));
  let covered = 0;
  for (const cue of primaries) {
    const start = cue.start_frame ?? 0;
    const end = cue.end_frame ?? beat.duration_frames;
    if (start !== covered || end <= start || end > beat.duration_frames) return false;
    covered = end;
  }
  return covered === beat.duration_frames;
}

function invalidBounds(start: number | null | undefined, end: number | null | undefined, duration: number): boolean {
  if ((start == null) !== (end == null)) return true;
  if (start == null || end == null) return false;
  return !Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end <= start || end > duration;
}

/** Backend schema constraints that must hold even while materials are still unbound. */
export function inspectPlanSchema(plan: ProductionPlan): PlanReadinessIssue[] {
  const issues: PlanReadinessIssue[] = [];
  if (!plan.goal.trim() || plan.goal.length > 2000) issues.push({ kind: 'goal', status: 'invalid', beatIndex: -1, key: 'goal',
    reason: !plan.goal.trim() ? '填写作品目标' : '作品目标不能超过 2000 字' });
  if (plan.beats.length < 1 || plan.beats.length > 40) issues.push({ kind: 'length', status: 'invalid', beatIndex: -1,
    key: 'length', reason: '作品方案需要 1 至 40 个段落' });
  if (plan.beats.reduce((total, beat) => total + beat.duration_frames, 0) > plan.output.fps * 60) {
    issues.push({ kind: 'length', status: 'invalid', beatIndex: -1, key: 'length', reason: '成片总时长不能超过 60 秒' });
  }
  plan.beats.forEach((beat, beatIndex) => {
    if (!beat.purpose.trim() || beat.purpose.length > 1000) issues.push({ kind: 'purpose', status: 'invalid', beatIndex,
      key: beat.id, reason: !beat.purpose.trim() ? `第 ${beatIndex + 1} 段缺少叙事作用` : `第 ${beatIndex + 1} 段叙事作用超过 1000 字` });
    if (!Number.isInteger(beat.duration_frames) || beat.duration_frames <= 0) issues.push({ kind: 'timing', status: 'invalid', beatIndex,
      key: beat.id, reason: `第 ${beatIndex + 1} 段时长无效` });
    if (beat.turns.some((turn) => !turn.text.trim() || turn.text.length > 2000 || turn.speaker.length > 80)) {
      issues.push({ kind: 'turn', status: 'invalid', beatIndex, key: beat.id,
        reason: beat.turns.some((turn) => !turn.text.trim()) ? `第 ${beatIndex + 1} 段有空台词，请填写或删除`
          : `第 ${beatIndex + 1} 段台词超出长度限制，请修改` });
    }
    if (beat.turns.length > beat.duration_frames || beat.turns.some((turn) => invalidBounds(turn.start_frame, turn.end_frame, beat.duration_frames))) {
      issues.push({ kind: 'timing', status: 'invalid', beatIndex, key: beat.id, reason: `第 ${beatIndex + 1} 段台词时间超出段落` });
    }
    const cues = [...beat.visuals, ...beat.audio];
    if (new Set(cues.map((cue) => cue.id)).size !== cues.length || cues.some((cue) => !cue.id || cue.description.length > 1000
      || invalidBounds(cue.start_frame, cue.end_frame, beat.duration_frames))) {
      issues.push({ kind: 'timing', status: 'invalid', beatIndex, key: beat.id,
        reason: `第 ${beatIndex + 1} 段画面或声音的时间、描述有误` });
    }
  });
  return issues;
}

function currentMedia(cue: PlanCue, candidate: PlanMediaCandidate | undefined, connected: boolean): boolean {
  const media = candidate?.media;
  const ref = cue.media;
  return Boolean(connected && ref && media && candidate?.node.data.remixVisualOutputCurrent !== false
    && candidate.node.data.workflowResultStale !== true
    && ref.media_hash === media.media_hash && ref.media_path === media.media_path
    && ref.media_kind === media.media_kind && ref.duration_us === media.duration_us
    && ref.has_audio === (media.has_audio === true));
}

export function inspectProductionPlan(plan: ProductionPlan, candidates: PlanMediaCandidate[],
  pending: PlanPendingBindings, nodes: CanvasNode[], planNodeId: string, edges: CanvasEdge[]): PlanReadiness {
  const result: PlanReadiness = { issues: inspectPlanSchema(plan), visualReady: 0, visualTotal: 0, audioReady: 0, audioTotal: 0 };
  plan.beats.forEach((beat, beatIndex) => {
    const graphicOnly = beat.visuals.length > 0 && beat.visuals.every((cue) => cue.role === 'graphic');
    if (!graphicOnly && !hasPrimaryCoverage(beat)) result.issues.push({ kind: 'coverage', status: 'invalid', beatIndex, key: beat.id,
      reason: `第 ${beatIndex + 1} 段需要连续覆盖全段的主画面` });
    beat.audio.forEach((cue) => {
      if (cue.role !== 'speech' || cue.media?.media_kind !== 'video') return;
      const start = cue.start_frame ?? 0;
      const end = cue.end_frame ?? beat.duration_frames;
      const matchingPicture = beat.visuals.some((visual) => visual.role === 'primary'
        && visual.media?.node_id === cue.media?.node_id
        && (visual.start_frame ?? 0) === start
        && (visual.end_frame ?? beat.duration_frames) === end);
      if (!matchingPicture) result.issues.push({ kind: 'timing', status: 'invalid', beatIndex, key: beat.id,
        reason: `第 ${beatIndex + 1} 段的同期人声使用视频声音时，需要与同一视频主画面的起止时间一致；可调整时段、改用独立音频，或改为旁白` });
    });
    for (const kind of ['visuals', 'audio'] as const) {
      beat[kind].forEach((cue, cueIndex) => {
        if (kind === 'visuals' && cue.role === 'graphic') return;
        const key = planCueKey(beat.id, kind, cue.id);
        if (kind === 'visuals') result.visualTotal += 1;
        else result.audioTotal += 1;
        const candidate = candidates.find((item) => item.node.id === cue.media?.node_id);
        const connected = edges.some((edge) => edge.source === cue.media?.node_id && edge.target === planNodeId);
        if (currentMedia(cue, candidate, connected)) {
          if (kind === 'visuals') result.visualReady += 1;
          else result.audioReady += 1;
          return;
        }
        const nodeId = pending[key];
        const creating = !cue.media && nodeId && nodes.some((node) => node.id === nodeId);
        result.issues.push({ kind, beatIndex, cueIndex, key,
          status: cue.media ? 'stale' : creating ? 'creating' : 'missing',
          reason: cue.media ? !connected ? `第 ${beatIndex + 1} 段素材连线已断开，请重新绑定`
            : `第 ${beatIndex + 1} 段${kind === 'visuals' ? '画面' : '声音'}素材已变化，请重新绑定`
            : creating ? `第 ${beatIndex + 1} 段素材制作中` : `第 ${beatIndex + 1} 段${kind === 'visuals' ? '画面' : '声音'}未绑定`,
          ...(creating ? { nodeId } : {}),
        });
      });
    }
  });
  return result;
}
