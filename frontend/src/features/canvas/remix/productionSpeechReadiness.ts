import type { PlanPlacementV2, PlanTurnV2, ProductionPlanV2 } from './productionPlanV2Model';
import { projectProductionTimeline, sourceRangeForPlacement, type ProjectedPlacement } from '@/features/canvas/compose/productionComposeModel';

export interface SpeechReadinessIssue {
  turnId: string;
  beatIndex: number;
  turnIndex: number;
  reason: 'select_source' | 'invalid_source' | 'calibrate' | 'running';
}

export interface SpeechReviewSnapshot {
  status: 'no_speech' | 'script_mismatch' | 'duration_mismatch' | 'erhua_variant';
  placementId: string;
  mediaHash: string;
  text: string;
  sourceInUs: number;
  sourceOutUs: number;
  takeId?: string;
}

export interface PendingSpeechOperation {
  placementId: string;
  mediaHash: string;
  text: string;
  sourceInUs: number;
  sourceOutUs: number;
  takeId?: string;
}

/** Dialogue in one placed take shares recognition, while retaining one timing per turn. */
export function speechTurnsForPlacement(plan: ProductionPlanV2, placementId: string): PlanTurnV2[] {
  const take = plan.speech_takes?.find((item) => item.origin_placement_id === placementId);
  return plan.beats.flatMap((beat) => beat.turns).filter((turn) =>
    take?.turn_ids.includes(turn.id)
    ||
    turn.speech_placement_id === placementId
    || turn.speech_placement_id === undefined && turn.speech_timing?.placement_id === placementId);
}

/** Match the native draft-take reassignment guard before offering a source change. */
export function sharedSpeechTakeSourceBlocker(plan: ProductionPlanV2, takeId: string): string | null {
  const take = plan.speech_takes?.find((item) => item.id === takeId);
  if (!take || take.turn_ids.length < 2) return null;
  const turns = plan.beats.flatMap((beat) => beat.turns).filter((turn) => take.turn_ids.includes(turn.id));
  const cues = (plan.speech_cues ?? []).filter((cue) => cue.take_id === take.id);
  const wordIds = new Set(turns.flatMap((turn) => [...(turn.words ?? []), ...(turn.speech_timing?.words ?? [])].map((word) => word.id)));
  const rows = [...plan.placements, ...(plan.selections ?? [])];
  if (turns.some((turn) => turn.speech_timing || turn.words?.length || turn.timing_basis && turn.timing_basis !== 'estimated')
    || cues.some((cue) => cue.timing_status === 'ready' || cue.evidence_ref || cue.word_ids.length
      || cue.source_start_us != null || cue.source_end_us != null)
    || rows.some((row) => [row.start, row.end].some((anchor) => anchor.kind === 'word' && wordIds.has(anchor.ref_id ?? '')))) {
    return '已确认语音时间或词锚点依赖此来源，当前不能直接更换；请先处理关联证据。';
  }
  const cueIds = new Set(cues.map((cue) => cue.id));
  if (plan.placements.some((row) => row.id !== take.origin_placement_id && row.speech_take_id === take.id)
    || rows.some((row) => [row.start, row.end].some((anchor) => anchor.kind === 'take' && anchor.ref_id === take.id
      || anchor.kind === 'cue' && cueIds.has(anchor.ref_id ?? '')))) {
    return '现有时间引用依赖此发声实例，当前不能直接更换；请先处理关联引用。';
  }
  return null;
}

/** Choices remain visible while media is being prepared; recognition uses bound sources below. */
export function plannedSpeechSourcePlacementsForTurn(plan: ProductionPlanV2, turnId: string): PlanPlacementV2[] {
  const timeline = projectProductionTimeline(plan);
  const beatIndex = plan.beats.findIndex((beat) => beat.turns.some((turn) => turn.id === turnId));
  const beat = timeline.beats[beatIndex];
  if (!beat) return [];
  return timeline.placements.filter((row) => (row.kind === 'visual' && row.source_audio !== false
      || row.kind === 'audio' && (row.role === 'speech' || row.role === 'narration'))
    && !row.loop && !row.issue && (!row.turn_id || row.turn_id === turnId)
    && row.startFrame !== null && row.endFrame !== null
    && row.startFrame < beat.endFrame && row.endFrame > beat.startFrame)
    .filter((row) => {
      const resource = plan.resources.find((item) => item.id === row.resource_id);
      return !resource || row.kind === 'visual' && resource.media_kind === 'video' && resource.has_audio
        || row.kind === 'audio' && resource.has_audio;
    });
}

/** Only a direct, audible placement actually played during this turn's beat can time its speech. */
export function speechSourceCandidatesForTurn(
  plan: ProductionPlanV2, turnId: string, timeline = projectProductionTimeline(plan),
): ProjectedPlacement[] {
  const beatIndex = plan.beats.findIndex((beat) => beat.turns.some((turn) => turn.id === turnId));
  const beat = timeline.beats[beatIndex];
  if (!beat) return [];
  const resources = new Map(plan.resources.map((resource) => [resource.id, resource]));
  return timeline.placements.filter((placement) => {
    const resource = resources.get(placement.resource_id ?? '');
    if (!resource || placement.loop || placement.issue || placement.turn_id && placement.turn_id !== turnId
      || placement.startFrame === null || placement.endFrame === null
      || placement.startFrame >= beat.endFrame || placement.endFrame <= beat.startFrame
      || !['program_frame', 'beat', 'turn'].includes(placement.start.kind)
      || !['program_frame', 'beat', 'turn'].includes(placement.end.kind)) return false;
    return placement.kind === 'audio' && ['speech', 'narration'].includes(placement.role)
      && (resource.media_kind === 'audio' || resource.media_kind === 'video' && resource.has_audio)
      || placement.kind === 'visual' && placement.source_audio !== false && resource.media_kind === 'video' && resource.has_audio;
  });
}

export function inspectProductionSpeechReadiness(
  plan: ProductionPlanV2,
  reviews: Record<string, SpeechReviewSnapshot>,
  pending: Record<string, PendingSpeechOperation>,
): SpeechReadinessIssue[] {
  const timeline = projectProductionTimeline(plan);
  const resources = new Map(plan.resources.map((item) => [item.id, item]));
  const projectedWords = new Set(timeline.words.map((word) => word.id));
  const issues: SpeechReadinessIssue[] = [];
  for (const [beatIndex, beat] of plan.beats.entries()) {
    const projectedBeat = timeline.beats[beatIndex];
    for (const [turnIndex, turn] of beat.turns.entries()) {
      if (!turn.text.trim()) continue;
      const take = plan.speech_takes?.find((item) => item.beat_id === beat.id && item.turn_ids.includes(turn.id));
      if (take) {
        const resource = resources.get(take.resource_id);
        const projectedTake = timeline.speechTakes.find((item) => item.id === take.id);
        const issue = (reason: SpeechReadinessIssue['reason']) => issues.push({ turnId: turn.id, beatIndex, turnIndex, reason });
        if (!resource || !resource.has_audio || projectedTake?.startFrame == null || projectedTake.endFrame == null) {
          issue('invalid_source'); continue;
        }
        const sourceOutUs = take.source_out_us ?? resource.duration_us;
        const sameTake = (item: SpeechReviewSnapshot | PendingSpeechOperation | undefined) => Boolean(item
          && (item.takeId === take.id || item.placementId === take.origin_placement_id)
          && item.mediaHash === resource.media_hash && item.text === turn.text
          && item.sourceInUs === 0 && item.sourceOutUs === resource.duration_us);
        if (sameTake(pending[turn.id])) { issue('running'); continue; }
        const cues = plan.speech_cues?.filter((cue) => cue.take_id === take.id && cue.turn_id === turn.id) ?? [];
        const validCues = cues.length > 0 && cues.every((cue) => cue.timing_status === 'ready'
          && cue.evidence_ref && cue.source_start_us != null && cue.source_end_us != null
          && cue.source_start_us >= take.source_in_us && cue.source_end_us <= sourceOutUs
          && cue.source_start_us < cue.source_end_us);
        const sameText = cues.map((cue) => cue.text.replace(/\s+/g, '')).join('') === turn.text.replace(/\s+/g, '');
        if (validCues && sameText) continue;
        if (reviews[turn.id]?.status === 'no_speech' && sameTake(reviews[turn.id])) continue;
        issue('calibrate'); continue;
      }
      // null is an author's explicit choice to keep this text without speech.
      // An omitted source still needs selection when audible dialogue exists.
      if (turn.speech_placement_id === null) continue;
      const spoken = speechSourceCandidatesForTurn(plan, turn.id, timeline);
      const selectedId = turn.speech_placement_id === undefined ? turn.speech_timing?.placement_id : turn.speech_placement_id;
      const projectedTurn = projectedBeat?.turns[turnIndex];
      const overlapsVoice = spoken.some((placement) => (placement.kind === 'audio'
        || placement.kind === 'visual' && placement.role === 'primary' && placement.volume !== 0)
        && projectedTurn && placement.startFrame !== null
        && placement.endFrame !== null && placement.startFrame < projectedTurn.endFrame
        && projectedTurn.startFrame < placement.endFrame);
      if (!selectedId && !overlapsVoice) continue;
      const issue = (reason: SpeechReadinessIssue['reason']) => issues.push({ turnId: turn.id, beatIndex, turnIndex, reason });
      if (!selectedId) { issue('select_source'); continue; }
      if (!spoken.some((placement) => placement.id === selectedId)) {
        issue(plannedSpeechSourcePlacementsForTurn(plan, turn.id).some((row) => row.id === selectedId) ? 'invalid_source' : 'select_source');
        continue;
      }
      const source = spoken.find((placement) => placement.id === selectedId);
      const resource = source ? resources.get(source.resource_id ?? '') : null;
      const range = sourceRangeForPlacement(plan, selectedId);
      if (!source || !resource || !range) { issue('invalid_source'); continue; }
      const identity = { placementId: selectedId, mediaHash: resource.media_hash, text: turn.text,
        sourceInUs: 0, sourceOutUs: resource.duration_us };
      const sameSource = (item: SpeechReviewSnapshot | PendingSpeechOperation | undefined) => item
        && item.placementId === identity.placementId && item.mediaHash === identity.mediaHash
        && item.text === identity.text && item.sourceInUs === identity.sourceInUs
        && item.sourceOutUs === identity.sourceOutUs;
      if (sameSource(pending[turn.id])) { issue('running'); continue; }
      const timing = turn.speech_timing;
      if (timing && timing.words.length && timing.text === turn.text && timing.placement_id === selectedId
        && timing.media_hash === resource.media_hash
        && timing.words.every((word) => word.start_us >= range.sourceInUs && word.end_us <= range.sourceOutUs)
        && timing.words.every((word) => projectedWords.has(word.id))) continue;
      if (reviews[turn.id]?.status === 'no_speech' && sameSource(reviews[turn.id])) continue;
      issue('calibrate');
    }
  }
  return issues;
}
