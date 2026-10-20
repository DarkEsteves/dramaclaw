import type { PlanAnchorV2, ProductionPlanV2 } from './productionPlanV2Model';

/** Rebase v1-migrated cue IDs that were saved with copied global frame positions. */
export function upgradeMigratedPlanAnchors(plan: ProductionPlanV2): ProductionPlanV2 {
  if (!plan.provenance.source_plan_ref) return plan;
  const beatStarts = new Map<string, number>();
  let cursor = 0;
  for (const beat of plan.beats) {
    beatStarts.set(beat.id, cursor);
    cursor += beat.duration_frames;
  }
  const beatsByLongestId = [...plan.beats].sort((left, right) => right.id.length - left.id.length);
  let changed = false;
  const placements = plan.placements.map((placement) => {
    if (placement.start.kind !== 'program_frame' || placement.end.kind !== 'program_frame') return placement;
    const beat = beatsByLongestId.find((item) => placement.id.startsWith(`${placement.kind}:${item.id}:`));
    if (!beat) return placement;
    const beatStart = beatStarts.get(beat.id)!;
    const start = placement.start.frame;
    const end = placement.end.frame;
    if (start == null || end == null || start < beatStart || end > beatStart + beat.duration_frames || end <= start) return placement;
    const startAnchor: PlanAnchorV2 = { kind: 'beat', ref_id: beat.id, edge: 'start', offset_frames: start - beatStart };
    const endAnchor: PlanAnchorV2 = { kind: 'beat', ref_id: beat.id, edge: 'end', offset_frames: end - beatStart - beat.duration_frames };
    changed = true;
    return { ...placement, start: startAnchor, end: endAnchor };
  });
  return changed ? { ...plan, placements } : plan;
}

/** Add stable takes/cues only for unambiguous legacy source playback instances. */
export async function upgradePlanSpeech(plan: ProductionPlanV2, verifyEvidence: (timing: NonNullable<ProductionPlanV2['beats'][number]['turns'][number]['speech_timing']>) => Promise<boolean> = async () => false): Promise<ProductionPlanV2> {
 const missing = plan.beats.flatMap((beat) => beat.turns).filter((turn) => !(plan.speech_cues ?? []).some((cue) => cue.turn_id === turn.id));
 if (!missing.length) return plan;
 const next = structuredClone(plan); const originBeats = new Map<string, Set<string>>(); const ambiguous = new Set<string>();
 for (const beat of next.beats) {
  let previous: string | null = null; const seen = new Set<string>();
  for (const turn of beat.turns) {
   const origin = turn.speech_placement_id || turn.speech_timing?.placement_id;
   if (!origin) continue;
   if (previous !== origin && seen.has(origin)) ambiguous.add(origin);
   previous = origin; seen.add(origin); const beats = originBeats.get(origin) ?? new Set<string>(); beats.add(beat.id); originBeats.set(origin, beats);
  }
 }
 let changed = false;
 for (const beat of next.beats) for (const turn of beat.turns) {
  if (next.speech_cues?.some((cue) => cue.turn_id === turn.id)) continue;
  const timing = turn.speech_timing; const origin = turn.speech_placement_id || timing?.placement_id;
  const placement = next.placements.find((item) => item.id === origin); const resource = next.resources.find((item) => item.id === placement?.resource_id);
  if (!placement || !resource || !origin || ambiguous.has(origin) || originBeats.get(origin)?.size !== 1) continue;
  const audible = placement.kind === 'visual' && resource.media_kind === 'video' && resource.has_audio || placement.kind === 'audio' && ['speech', 'narration'].includes(placement.role) && (resource.media_kind === 'audio' || resource.media_kind === 'video' && resource.has_audio);
  if (!audible) continue;
  next.speech_takes ??= []; next.speech_cues ??= [];
  let take = next.speech_takes.find((item) => item.beat_id === beat.id && item.origin_placement_id === origin);
  if (!take) { take = { id: `take:${beat.id}:${origin}`, beat_id: beat.id, resource_id: resource.id, turn_ids: [], origin_placement_id: origin, source_in_us: placement.source_in_us ?? 0, source_out_us: placement.source_out_us ?? null, speed_num: placement.speed_num ?? 1, speed_den: placement.speed_den ?? 1, lead_in_frames: 0 }; next.speech_takes.push(take); }
  if (!take.turn_ids.includes(turn.id)) take.turn_ids.push(turn.id);
  const words = timing?.words ?? [];
  let valid = Boolean(timing && words.length && timing.placement_id === origin && timing.media_hash === resource.media_hash && timing.text === turn.text && timing.source_in_us === 0 && timing.source_out_us === resource.duration_us && timing.evidence_ref?.artifact_id && Number.isInteger(timing.evidence_ref.revision) && timing.evidence_ref.revision > 0 && words.every((word) => Number.isSafeInteger(word.start_us) && Number.isSafeInteger(word.end_us) && word.start_us >= 0 && word.end_us > word.start_us));
  if (valid && timing) {
   try { const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(turn.text)); const hash = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join(''); valid = hash === timing.text_hash && await verifyEvidence(timing); } catch { valid = false; }
  }
  const start = valid ? Math.min(...words.map((word) => word.start_us)) : null; const end = valid ? Math.max(...words.map((word) => word.end_us)) : null;
  valid = valid && start! >= take.source_in_us && end! <= Math.min(take.source_out_us ?? resource.duration_us, resource.duration_us);
  next.speech_cues.push({ id: `cue:${turn.id}`, turn_id: turn.id, take_id: take.id, text: turn.text, source_start_us: valid ? start : null, source_end_us: valid ? end : null, timing_source: 'asr', timing_status: valid ? 'ready' : 'draft', word_ids: valid ? words.map((word) => word.id) : [], evidence_ref: valid ? timing!.evidence_ref : null });
  if (placement.kind === 'visual') placement.speech_take_id = take.id;
  changed = true;
 }
 return changed ? next : plan;
}
