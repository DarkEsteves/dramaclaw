import { orderPlanSpeechTakes } from './productionSpeechTakeOrder';
import type { PlanAnchorV2, PlanPlacementV2, ProductionPlanV2 } from './productionPlanV2Model';

/** One bound file may serve many placements; every unbound row remains a distinct request. */
export function countIndependentAudioSources(plan: ProductionPlanV2, readyResourceIds?: ReadonlySet<string>): { ready: number; total: number } {
  const sources = new Set(plan.placements.filter((row) => row.kind === 'audio')
    .map((row) => row.resource_id ? `resource:${row.resource_id}` : `placement:${row.id}`));
  const ready = [...sources].filter((key) => key.startsWith('resource:')
    && (readyResourceIds === undefined || readyResourceIds.has(key.slice('resource:'.length)))).length;
  return { ready, total: sources.size };
}

function retainedSpeechOrigin(plan: ProductionPlanV2, placementId: string, turnId: string): boolean { return (plan.speech_takes ?? []).some((take) => take.origin_placement_id === placementId && take.turn_ids.includes(turnId)); }

type RemovedIds = {
  beats: Set<string>;
  turns: Set<string>;
  words: Set<string>;
  selections: Set<string>;
  placements: Set<string>;
};

function anchorReferencesRemoved(anchor: PlanAnchorV2 | null | undefined, removed: RemovedIds): boolean {
  if (!anchor?.ref_id) return false;
  if (anchor.kind === 'beat') return removed.beats.has(anchor.ref_id);
  if (anchor.kind === 'turn') return removed.turns.has(anchor.ref_id);
  if (anchor.kind === 'word') return removed.words.has(anchor.ref_id);
  if (anchor.kind === 'selection') return removed.selections.has(anchor.ref_id);
  if (anchor.kind === 'placement') return removed.placements.has(anchor.ref_id);
  return false;
}

function pruneReferences(plan: ProductionPlanV2, removed: RemovedIds): void {
  let changed = true;
  while (changed) {
    changed = false;
    for (const beat of plan.beats) for (const turn of beat.turns) {
      if (removed.turns.has(turn.id) || retainedSpeechOrigin(plan, turn.speech_timing?.placement_id ?? '', turn.id) || !turn.speech_timing || !removed.placements.has(turn.speech_timing.placement_id)) continue;
      for (const word of turn.speech_timing.words) if (!removed.words.has(word.id)) {
        removed.words.add(word.id);
        changed = true;
      }
      if (turn.timing_basis === 'aligned') for (const word of turn.words ?? []) if (!removed.words.has(word.id)) {
        removed.words.add(word.id);
        changed = true;
      }
    }
    for (const selection of plan.selections ?? []) {
      if (removed.selections.has(selection.id) || ![selection.start, selection.end].some((anchor) => anchorReferencesRemoved(anchor, removed))) continue;
      removed.selections.add(selection.id);
      changed = true;
    }
    for (const placement of plan.placements) {
      if (removed.placements.has(placement.id) || !(placement.turn_id && removed.turns.has(placement.turn_id))
        && ![placement.start, placement.end].some((anchor) => anchorReferencesRemoved(anchor, removed))) continue;
      removed.placements.add(placement.id);
      changed = true;
    }
  }

  const removedTakes = new Set((plan.speech_takes ?? []).filter((take) => removed.beats.has(take.beat_id) || take.turn_ids.every((id) => removed.turns.has(id))).map((take) => take.id));
  const removedCues = new Set((plan.speech_cues ?? []).filter((cue) => removed.turns.has(cue.turn_id) || removedTakes.has(cue.take_id)).map((cue) => cue.id));
  if (plan.speech_takes) { plan.speech_takes = plan.speech_takes.filter((take) => !removedTakes.has(take.id)); for (const take of plan.speech_takes) take.turn_ids = take.turn_ids.filter((id) => !removed.turns.has(id)); }
  if (plan.speech_cues) plan.speech_cues = plan.speech_cues.filter((cue) => !removedCues.has(cue.id));
  plan.placements = plan.placements.filter((item) => ![item.start, item.end].some((anchor) => anchor.kind === 'cue' && removedCues.has(anchor.ref_id ?? '') || anchor.kind === 'take' && removedTakes.has(anchor.ref_id ?? '')));
  plan.beats = plan.beats.filter((beat) => !removed.beats.has(beat.id));
  for (const beat of plan.beats) {
    beat.turns = beat.turns.filter((turn) => !removed.turns.has(turn.id));
    for (const turn of beat.turns) {
      if (turn.speech_placement_id && !retainedSpeechOrigin(plan, turn.speech_placement_id, turn.id) && removed.placements.has(turn.speech_placement_id)) turn.speech_placement_id = null;
      if (turn.speech_timing && !retainedSpeechOrigin(plan, turn.speech_timing.placement_id, turn.id) && removed.placements.has(turn.speech_timing.placement_id)) {
        turn.speech_timing = null;
        if (turn.timing_basis === 'aligned') {
          turn.timing_basis = 'estimated'; turn.words = []; turn.start_frame = null; turn.end_frame = null;
        }
      }
    }
  }
  if (plan.selections) plan.selections = plan.selections.filter((selection) => !removed.selections.has(selection.id));
  plan.placements = plan.placements.filter((placement) => !removed.placements.has(placement.id));
}

function emptyRemoved(): RemovedIds {
  return { beats: new Set(), turns: new Set(), words: new Set(), selections: new Set(), placements: new Set() };
}

function reanchorWordsToTurn(plan: ProductionPlanV2, turnId: string, wordIds: Set<string>): void {
  if (!wordIds.size) return;
  const reanchor = (anchor: PlanAnchorV2): PlanAnchorV2 => anchor.kind === 'word' && anchor.ref_id && wordIds.has(anchor.ref_id)
    ? { kind: 'turn', ref_id: turnId, edge: anchor.edge ?? 'start' } : anchor;
  for (const selection of plan.selections ?? []) {
    selection.start = reanchor(selection.start);
    selection.end = reanchor(selection.end);
  }
  for (const placement of plan.placements) {
    placement.start = reanchor(placement.start);
    placement.end = reanchor(placement.end);
  }
}

export function movePlanBeat(plan: ProductionPlanV2, index: number, direction: -1 | 1): boolean {
  const next = index + direction;
  if (index < 0 || next < 0 || index >= plan.beats.length || next >= plan.beats.length) return false;
  [plan.beats[index], plan.beats[next]] = [plan.beats[next], plan.beats[index]];
  return true;
}

export function removePlanBeat(plan: ProductionPlanV2, beatId: string): boolean {
  const beat = plan.beats.find((item) => item.id === beatId);
  if (!beat || plan.beats.length <= 1) return false;
  const removed = emptyRemoved();
  removed.beats.add(beatId);
  for (const turn of beat.turns) {
    removed.turns.add(turn.id);
    for (const word of turn.words ?? []) removed.words.add(word.id);
    for (const word of turn.speech_timing?.words ?? []) removed.words.add(word.id);
  }
  pruneReferences(plan, removed);
  return true;
}

export function removePlanTurn(plan: ProductionPlanV2, turnId: string): boolean {
  const turn = plan.beats.flatMap((beat) => beat.turns).find((item) => item.id === turnId);
  if (!turn) return false;
  const removed = emptyRemoved();
  removed.turns.add(turnId);
  for (const word of turn.words ?? []) removed.words.add(word.id);
  for (const word of turn.speech_timing?.words ?? []) removed.words.add(word.id);
  pruneReferences(plan, removed);
  return true;
}

export function removePlanPlacement(plan: ProductionPlanV2, placementId: string): boolean {
  if (!plan.placements.some((placement) => placement.id === placementId)) return false;
  for (const beat of plan.beats) for (const turn of beat.turns) {
    if (turn.speech_timing?.placement_id !== placementId || retainedSpeechOrigin(plan, placementId, turn.id)) continue;
    const wordIds = new Set([...(turn.speech_timing.words ?? []), ...(turn.timing_basis === 'aligned' ? turn.words ?? [] : [])].map((word) => word.id));
    reanchorWordsToTurn(plan, turn.id, wordIds);
  }
  const removed = emptyRemoved();
  removed.placements.add(placementId);
  pruneReferences(plan, removed);
  return true;
}

export function setPlanTurnText(plan: ProductionPlanV2, turnId: string, text: string): boolean {
  const turn = plan.beats.flatMap((beat) => beat.turns).find((item) => item.id === turnId);
  if (!turn) return false;
  if (turn.text === text) return true;
  const wordIds = new Set([...(turn.words ?? []), ...(turn.speech_timing?.words ?? [])].map((word) => word.id));
  for (const cue of plan.speech_cues ?? []) if (cue.turn_id === turnId) for (const id of cue.word_ids) wordIds.add(id);
  if ([...plan.placements, ...(plan.selections ?? [])].some((item) => [item.start, item.end].some((anchor) => anchor.kind === 'word' && wordIds.has(anchor.ref_id ?? '')))) {
    throw new Error('这句台词正在被词锚点使用，请先调整相关镜头、字幕或命名范围，再改台词并重新校准');
  }
  for (const cue of plan.speech_cues ?? []) if (cue.turn_id === turnId) { cue.timing_status = 'stale'; cue.word_ids = []; }
  turn.text = text;
  turn.words = [];
  turn.speech_timing = null;
  if (turn.timing_basis === 'aligned') {
    turn.timing_basis = 'estimated'; turn.start_frame = null; turn.end_frame = null;
  }
  return true;
}

export function setPlanPlacementRole(plan: ProductionPlanV2, placementId: string, role: PlanPlacementV2['role']): boolean {
  const placement = plan.placements.find((item) => item.id === placementId);
  if (!placement) return false;
  const allowed = placement.kind === 'visual' ? ['primary', 'overlay'] : placement.kind === 'audio' ? ['speech', 'narration', 'bgm', 'sfx'] : [];
  if (!allowed.includes(role)) return false;
  if (placement.kind === 'audio' && role !== 'speech' && role !== 'narration') {
    for (const beat of plan.beats) for (const turn of beat.turns) {
      if (retainedSpeechOrigin(plan, placementId, turn.id) || turn.speech_placement_id !== placementId && turn.speech_timing?.placement_id !== placementId) continue;
      const wordIds = new Set([...(turn.speech_timing?.words ?? []), ...(turn.timing_basis === 'aligned' ? turn.words ?? [] : [])].map((word) => word.id));
      reanchorWordsToTurn(plan, turn.id, wordIds);
    }
    for (const beat of plan.beats) for (const turn of beat.turns) {
      if (retainedSpeechOrigin(plan, placementId, turn.id) || turn.speech_placement_id !== placementId && turn.speech_timing?.placement_id !== placementId) continue;
      turn.speech_placement_id = null;
      turn.speech_timing = null;
      if (turn.timing_basis === 'aligned') {
        turn.timing_basis = 'estimated'; turn.words = []; turn.start_frame = null; turn.end_frame = null;
      }
    }
  }
  placement.role = role;
  placement.track_id = `${placement.kind}:${role}`;
  return true;
}

/** Accept source evidence and build one playback instance per source, preserving author cue IDs. */
export function acceptPlanSpeechTimings(plan: ProductionPlanV2, timings: Record<string, import('./productionPlanV2Model').PlanSpeechTimingV2 | null>): void {
 const draft = structuredClone(plan);
 acceptPlanSpeechTimingsDraft(draft, timings);
 orderPlanSpeechTakes(draft);
 Object.assign(plan, draft);
}

function acceptPlanSpeechTimingsDraft(plan: ProductionPlanV2, timings: Record<string, import('./productionPlanV2Model').PlanSpeechTimingV2 | null>): void {
 plan.speech_takes ??= []; plan.speech_cues ??= [];
 for (const beat of plan.beats) for (const turn of beat.turns) {
  if (!Object.prototype.hasOwnProperty.call(timings, turn.id)) continue;
  const timing = timings[turn.id]; if (!timing) { const words = new Set((turn.speech_timing?.words ?? []).map((word) => word.id)); reanchorWordsToTurn(plan, turn.id, words); turn.speech_timing = null; for (const cue of plan.speech_cues ?? []) if (cue.turn_id === turn.id && cue.timing_source === 'asr') { cue.timing_status = 'review'; cue.word_ids = []; } continue; }
  const placement = plan.placements.find((item) => item.id === timing.placement_id);
  const existingTake = plan.speech_takes.find((item) => item.beat_id === beat.id && (item.turn_ids.includes(turn.id) || item.origin_placement_id === timing.placement_id));
  const resourceId = existingTake?.resource_id ?? placement?.resource_id;
  if (!resourceId || !timing.words.length) continue;
  turn.speech_timing = timing;
  const take = existingTake ?? { id: `take:${beat.id}:${timing.placement_id}`, beat_id: beat.id, resource_id: resourceId, turn_ids: [], origin_placement_id: timing.placement_id, source_in_us: placement?.source_in_us ?? 0, source_out_us: placement?.source_out_us ?? null, speed_num: placement?.speed_num ?? 1, speed_den: placement?.speed_den ?? 1, lead_in_frames: 0 };
  if (!existingTake) plan.speech_takes.push(take);
  if (!take.turn_ids.includes(turn.id)) take.turn_ids.push(turn.id);
  if (placement?.kind === 'visual') placement.speech_take_id = take.id;
  const existing = plan.speech_cues.filter((cue) => cue.turn_id === turn.id);
  if (existing.some((cue) => cue.timing_source !== 'asr')) continue;
  if (existing.length > 1) { for (const cue of existing) cue.timing_status = 'review'; continue; }
  const cue = existing[0] ?? { id: `cue:${turn.id}`, turn_id: turn.id, take_id: take.id, text: turn.text, timing_source: 'asr' as const, timing_status: 'ready' as const, source_start_us: null, source_end_us: null, word_ids: [], evidence_ref: null };
  const nextWordIds = new Set(timing.words.map((word) => word.id));
  const ambiguousWords = cue.word_ids.some((id) => !nextWordIds.has(id));
  Object.assign(cue, { take_id: take.id, text: turn.text, source_start_us: Math.min(...timing.words.map((word) => word.start_us)), source_end_us: Math.max(...timing.words.map((word) => word.end_us)), word_ids: [...nextWordIds], evidence_ref: timing.evidence_ref, timing_status: ambiguousWords ? 'review' : 'ready' });
  if (!existing.length) plan.speech_cues.push(cue);
 }
}

export function bindPlanSpeechSource(plan: ProductionPlanV2, turnId: string, placementId: string | null): void {
 const draft = structuredClone(plan);
 bindPlanSpeechSourceDraft(draft, turnId, placementId);
 orderPlanSpeechTakes(draft);
 Object.assign(plan, draft);
}

function bindPlanSpeechSourceDraft(plan: ProductionPlanV2, turnId: string, placementId: string | null): void {
 const beat = plan.beats.find((item) => item.turns.some((turn) => turn.id === turnId));
 const turn = beat?.turns.find((item) => item.id === turnId); if (!beat || !turn) return;
 const oldTake = plan.speech_takes?.find((item) => item.turn_ids.includes(turnId));
 if ((oldTake?.origin_placement_id ?? turn.speech_placement_id ?? null) === placementId
   && (placementId !== null || turn.speech_placement_id === null)) return;
 const oldWords = new Set([...(turn.speech_timing?.words ?? []), ...(turn.words ?? [])].map((word) => word.id));
 reanchorWordsToTurn(plan, turnId, oldWords); turn.speech_timing = null; turn.speech_placement_id = placementId;
 for (const take of plan.speech_takes ?? []) take.turn_ids = take.turn_ids.filter((id) => id !== turnId);
 const placement = plan.placements.find((item) => item.id === placementId);
 let take = plan.speech_takes?.find((item) => item.beat_id === beat.id && item.origin_placement_id === placementId);
 if (placement?.resource_id) {
  plan.speech_takes ??= [];
  if (!take) { take = { id: `take:${beat.id}:${placement.id}`, beat_id: beat.id, resource_id: placement.resource_id, turn_ids: [], origin_placement_id: placement.id, source_in_us: placement.source_in_us ?? 0, source_out_us: placement.source_out_us ?? null, speed_num: placement.speed_num ?? 1, speed_den: placement.speed_den ?? 1, lead_in_frames: 0 }; plan.speech_takes.push(take); }
  take.turn_ids.push(turnId); if (placement.kind === 'visual') placement.speech_take_id = take.id;
 }
 const cueIds = new Set((plan.speech_cues ?? []).filter((cue) => cue.turn_id === turnId).map((cue) => cue.id));
 for (const cue of plan.speech_cues ?? []) if (cueIds.has(cue.id)) { if (take) cue.take_id = take.id; cue.timing_status = 'stale'; cue.word_ids = []; }
 if (!take) {
  const reanchor = (anchor: PlanAnchorV2): PlanAnchorV2 => anchor.kind === 'cue' && cueIds.has(anchor.ref_id ?? '') ? { kind: 'turn', ref_id: turnId, edge: anchor.edge ?? 'start' } : anchor;
  for (const row of [...plan.placements, ...(plan.selections ?? [])]) { row.start = reanchor(row.start); row.end = reanchor(row.end); }
  if (plan.speech_cues) plan.speech_cues = plan.speech_cues.filter((cue) => !cueIds.has(cue.id));
 }
 const empty = new Map((plan.speech_takes ?? []).filter((item) => !item.turn_ids.length).map((item) => [item.id, item.beat_id]));
 const reanchorTake = (anchor: PlanAnchorV2): PlanAnchorV2 => anchor.kind === 'take' && empty.has(anchor.ref_id ?? '') ? { kind: 'beat', ref_id: empty.get(anchor.ref_id!)!, edge: anchor.edge ?? 'start' } : anchor;
 for (const row of [...plan.placements, ...(plan.selections ?? [])]) { row.start = reanchorTake(row.start); row.end = reanchorTake(row.end); }
 for (const row of plan.placements) if (empty.has(row.speech_take_id ?? '')) row.speech_take_id = null;
 if (plan.speech_takes) plan.speech_takes = plan.speech_takes.filter((item) => !empty.has(item.id));
}

/** Binding a take's origin or display window updates its one authoritative media source. */
export function bindPlanPlacementResource(plan: ProductionPlanV2, placementId: string, resourceId: string): boolean {
 const placement = plan.placements.find((row) => row.id === placementId); const resource = plan.resources.find((row) => row.id === resourceId);
 if (!placement || !resource) return false;
 const takes = (plan.speech_takes ?? []).filter((take) => take.origin_placement_id === placementId || take.id === placement.speech_take_id);
 if (takes.length && !(resource.media_kind === 'audio' || resource.media_kind === 'video' && resource.has_audio)) throw new Error('发声素材需要音频或带声音的视频');
 const turnIds = new Set(takes.flatMap((take) => take.turn_ids)); const takeIds = new Set(takes.map((take) => take.id));
 const previousResource = plan.resources.find((row) => row.id === (takes[0]?.resource_id ?? placement.resource_id));
 const changedMedia = previousResource?.media_hash !== resource.media_hash;
 placement.resource_id = resourceId;
 for (const take of takes) take.resource_id = resourceId;
 for (const row of plan.placements) if (takeIds.has(row.speech_take_id ?? '') || takes.some((take) => take.origin_placement_id === row.id)) row.resource_id = resourceId;
 if (!changedMedia) return true;
 for (const beat of plan.beats) for (const turn of beat.turns) {
  if (!turnIds.has(turn.id) && turn.speech_placement_id !== placementId && turn.speech_timing?.placement_id !== placementId) continue;
  const words = new Set([...(turn.words ?? []), ...(turn.speech_timing?.words ?? [])].map((word) => word.id));
  reanchorWordsToTurn(plan, turn.id, words); turn.speech_timing = null; turn.words = [];
  if (turn.timing_basis === 'aligned') { turn.timing_basis = 'estimated'; turn.start_frame = null; turn.end_frame = null; }
 }
 for (const cue of plan.speech_cues ?? []) if (takeIds.has(cue.take_id)) {
  const words = new Set(cue.word_ids); reanchorWordsToTurn(plan, cue.turn_id, words);
  cue.timing_status = 'stale'; cue.word_ids = []; cue.evidence_ref = null; cue.source_start_us = null; cue.source_end_us = null;
 }
 return true;
}

export function invalidatePlanSpeechTakeEvidence(plan: ProductionPlanV2, takeId: string): void {
 const take = plan.speech_takes?.find((item) => item.id === takeId); if (!take) return;
 const turns = new Set(take.turn_ids);
 for (const beat of plan.beats) for (const turn of beat.turns) if (turns.has(turn.id)) {
  const words = new Set([...(turn.words ?? []), ...(turn.speech_timing?.words ?? []), ...(plan.speech_cues ?? []).filter((cue) => cue.take_id === takeId && cue.turn_id === turn.id).flatMap((cue) => cue.word_ids)].map((word) => typeof word === 'string' ? word : word.id));
  const reanchor = (anchor: PlanAnchorV2): PlanAnchorV2 => anchor.kind === 'word' && words.has(anchor.ref_id ?? '') ? { ...anchor, kind: 'turn', ref_id: turn.id, edge: anchor.edge ?? 'start' } : anchor;
  for (const row of [...plan.placements, ...(plan.selections ?? [])]) { row.start = reanchor(row.start); row.end = reanchor(row.end); }
  turn.words = []; turn.speech_timing = null;
  if (turn.timing_basis === 'aligned') { turn.timing_basis = 'estimated'; turn.start_frame = null; turn.end_frame = null; }
 }
 for (const cue of plan.speech_cues ?? []) if (cue.take_id === takeId) { cue.timing_status = 'stale'; cue.word_ids = []; cue.evidence_ref = null; cue.source_start_us = null; cue.source_end_us = null; }
}

export function editPlanSpeechTakeSource(plan: ProductionPlanV2, takeId: string, edit: (take: import('./productionPlanV2Model').PlanSpeechTakeV2) => void): boolean {
 const take = plan.speech_takes?.find((item) => item.id === takeId); if (!take) return false;
 const before = [take.source_in_us, take.source_out_us, take.speed_num, take.speed_den];
 edit(take);
 const after = [take.source_in_us, take.source_out_us, take.speed_num, take.speed_den];
 if (before.every((value, index) => value === after[index])) return false;
 invalidatePlanSpeechTakeEvidence(plan, takeId); return true;
}
