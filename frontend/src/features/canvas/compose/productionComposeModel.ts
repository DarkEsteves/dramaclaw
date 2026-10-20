import type { PlanAnchorV2, PlanBeatV2, PlanPlacementV2, PlanSelectionV2, PlanSpeechCueV2, PlanSpeechTakeV2, PlanTurnV2, ProductionPlanV2 } from '@/features/canvas/remix/productionPlanV2Model';
import { upgradeMigratedPlanAnchors } from '@/features/canvas/remix/productionPlanV2Upgrade';
import { locateTextBoundary } from '@/features/canvas/remix/authoredTextTiming';

export interface ProjectedPlacement extends PlanPlacementV2 {
  startFrame: number | null;
  endFrame: number | null;
  issue: string | null;
  sourceOutUs?: number;
}
export interface ProjectedBeat { id: string; purpose: string; startFrame: number; endFrame: number; turns: Array<{ id: string; speaker: string; text: string; startFrame: number; endFrame: number; estimated: boolean }> }
export interface ProjectedWord { id: string; text: string; turnId: string; startFrame: number; endFrame: number }
export interface ProjectedSelection extends PlanSelectionV2 { startFrame: number | null; endFrame: number | null; estimated: boolean; issue: string | null }
export interface ProjectedSpeechCue extends PlanSpeechCueV2 { startFrame: number | null; endFrame: number | null; issue: string | null }
export interface ProjectedSpeechTake extends PlanSpeechTakeV2 { startFrame: number; endFrame: number; sourceOutUs: number }
export interface ProductionTimelineProjection { issues: CanvasBindingIssue[]; speechCues: ProjectedSpeechCue[]; speechTakes: ProjectedSpeechTake[]; durationFrames: number; beats: ProjectedBeat[]; words: ProjectedWord[]; selections: ProjectedSelection[]; placements: ProjectedPlacement[] }
export interface CanvasBindingIssue { placement_id: string; resource_id?: string; code: string; message: string }

function roundRatio(numerator: bigint, denominator: bigint): number {
  if (denominator <= 0n) throw new Error('时间比例无效');
  const negative = numerator < 0n;
  const absolute = negative ? -numerator : numerator;
  let quotient = absolute / denominator;
  const remainder = absolute % denominator;
  if (remainder * 2n > denominator || remainder * 2n === denominator && quotient % 2n === 1n) quotient += 1n;
  return Number(negative ? -quotient : quotient);
}

export function sourceRangeForPlacement(plan: ProductionPlanV2, placementId: string): { sourceInUs: number; sourceOutUs: number } | null {
  const placement = plan.placements.find((item) => item.id === placementId);
  if (!placement) return null;
  if (![plan.output.fps_num, plan.output.fps_den, placement.speed_num ?? 1, placement.speed_den ?? 1].every((value) => Number.isSafeInteger(value) && value > 0)) return null;
  const projected = projectProductionTimeline(plan).placements.find((item) => item.id === placementId);
  if (!projected || projected.startFrame === null || projected.endFrame === null || projected.issue) return null;
  const take = (plan.speech_takes ?? []).find((item) => item.id === placement.speech_take_id || !placement.speech_take_id && item.origin_placement_id === placement.id);
  if (take) {
    const takeRange = projectProductionTimeline(plan).speechTakes.find((item) => item.id === take.id);
    if (!takeRange) return null;
    const sample = (frame: number) => take.source_in_us + roundRatio(BigInt(frame - takeRange.startFrame) * BigInt(plan.output.fps_den) * 1_000_000n * BigInt(take.speed_num), BigInt(plan.output.fps_num) * BigInt(take.speed_den));
    return { sourceInUs: sample(projected.startFrame), sourceOutUs: projected.endFrame === takeRange.endFrame ? takeRange.sourceOutUs : Math.min(takeRange.sourceOutUs, sample(projected.endFrame)) };
  }
  const sourceInUs = placement.source_in_us ?? 0;
  const expectedSpan = roundRatio(
    BigInt(projected.endFrame - projected.startFrame) * BigInt(plan.output.fps_den) * 1_000_000n * BigInt(placement.speed_num ?? 1),
    BigInt(plan.output.fps_num) * BigInt(placement.speed_den ?? 1),
  );
  const sourceOutUs = projected.sourceOutUs ?? placement.source_out_us ?? sourceInUs + expectedSpan;
  const resource = plan.resources.find((item) => item.id === placement.resource_id);
  const tolerance = Math.max(1, 500_000 * plan.output.fps_den / plan.output.fps_num * (placement.speed_num ?? 1) / (placement.speed_den ?? 1));
  return sourceOutUs > sourceInUs && Math.abs(sourceOutUs - sourceInUs - expectedSpan) <= tolerance
    && resource && sourceOutUs <= resource.duration_us ? { sourceInUs, sourceOutUs } : null;
}

function speakingTake(plan: ProductionPlanV2, beat: PlanBeatV2, turn: PlanTurnV2): { placement: PlanPlacementV2; sourceOutUs: number; frames: number; startOffset: number } | null {
  const timing = turn.speech_timing;
  if (!timing || beat.timing_mode === 'fixed' || timing.text !== turn.text || turn.speech_placement_id != null && turn.speech_placement_id !== timing.placement_id) return null;
  const placement = plan.placements.find((item) => item.id === timing.placement_id);
  const resource = plan.resources.find((item) => item.id === placement?.resource_id);
  if (!placement || !resource || resource.media_hash !== timing.media_hash || placement.loop || placement.turn_id != null && placement.turn_id !== turn.id) return null;
  const audible = placement.kind === 'audio' && ['speech', 'narration'].includes(placement.role)
    && (resource.media_kind === 'audio' || resource.media_kind === 'video' && resource.has_audio)
    || placement.kind === 'visual' && resource.media_kind === 'video' && resource.has_audio;
  if (!audible || [placement.start, placement.end].some((anchor, index) =>
    !['beat', 'turn'].includes(anchor.kind) || anchor.ref_id !== (anchor.kind === 'beat' ? beat.id : turn.id)
    || anchor.edge !== (index === 0 ? 'start' : 'end'))) return null;
  const startOffset = placement.start.offset_frames ?? 0;
  if (startOffset < 0 || (placement.end.offset_frames ?? 0) !== 0) return null;
  const sourceIn = placement.source_in_us ?? 0;
  const sourceOutUs = placement.source_out_us ?? resource.duration_us;
  if (sourceOutUs <= sourceIn || sourceOutUs > resource.duration_us) return null;
  const speedNum = placement.speed_num ?? 1;
  const speedDen = placement.speed_den ?? 1;
  if (!Number.isSafeInteger(sourceIn) || sourceIn < 0 ||
    ![sourceOutUs, plan.output.fps_num, plan.output.fps_den, speedNum, speedDen].every((value) => Number.isSafeInteger(value) && value > 0)) return null;
  const frames = roundRatio(BigInt(sourceOutUs - sourceIn) * BigInt(plan.output.fps_num) * BigInt(speedDen),
    1_000_000n * BigInt(plan.output.fps_den) * BigInt(speedNum));
  return frames >= 1 ? { placement, sourceOutUs, frames, startOffset } : null;
}

function speakingTakeForPlacement(plan: ProductionPlanV2, placementId: string) {
  for (const beat of plan.beats) for (const turn of beat.turns) {
    if (turn.speech_placement_id !== placementId && turn.speech_timing?.placement_id !== placementId) continue;
    const take = speakingTake(plan, beat, turn);
    if (take?.placement.id === placementId) return take;
  }
  return null;
}

/** Canvas bindings are checked separately from the saved project media hashes. */
export function canvasBindingIssues(plan: ProductionPlanV2, edges: Array<{ source: string; target: string }>, planNodeId: string,
  candidates: Array<{ node: { id: string }; media: { media_hash: string; media_path: string } | null }>): CanvasBindingIssue[] {
  const resources = new Map(plan.resources.map((item) => [item.id, item]));
  const available = new Map(candidates.map((item) => [item.node.id, item.media]));
  const connected = new Set(edges.filter((edge) => edge.target === planNodeId).map((edge) => edge.source));
  const issues: CanvasBindingIssue[] = [];
  for (const placement of [...plan.placements, ...(plan.speech_takes ?? []).map((take) => ({ id: take.origin_placement_id, resource_id: take.resource_id }))]) {
    if (!placement.resource_id) continue;
    const resource = resources.get(placement.resource_id);
    if (!resource) continue;
    if (!connected.has(resource.node_id)) issues.push({ placement_id: placement.id, resource_id: resource.id, code: 'disconnected_canvas_media', message: '素材节点未连接作品方案' });
    const media = available.get(resource.node_id);
    if (!media || media.media_hash !== resource.media_hash || media.media_path !== resource.media_path) {
      issues.push({ placement_id: placement.id, resource_id: resource.id, code: 'stale_canvas_media', message: '画布素材版本已变化，请重新绑定' });
    }
  }
  return issues;
}

/** Mirrors the saved plan's anchor rules for immediate editor feedback. The server remains authoritative at export. */
export function projectProductionTimeline(plan: ProductionPlanV2): ProductionTimelineProjection {
  plan = upgradeMigratedPlanAnchors(plan);
  const resourcesById = new Map(plan.resources.map((resource) => [resource.id, resource]));
  const placementsById = new Map(plan.placements.map((placement) => [placement.id, placement]));
  const takesByBeat = new Map<string, PlanSpeechTakeV2[]>();
  const cuesByTake = new Map<string, PlanSpeechCueV2[]>();
  for (const take of plan.speech_takes ?? []) takesByBeat.set(take.beat_id, [...(takesByBeat.get(take.beat_id) ?? []), take]);
  for (const cue of plan.speech_cues ?? []) cuesByTake.set(cue.take_id, [...(cuesByTake.get(cue.take_id) ?? []), cue]);
  const semantic = new Map<string, { start: number; end: number }>();
  const beats: ProjectedBeat[] = [];
  const words: ProjectedWord[] = [];
  const estimatedTurns = new Set<string>();
  const pinnedSpeechPlacements = new Map<string, { start: number; end: number }>();
  const speechTakeRanges = new Map<string, { start: number; end: number; sourceOutUs: number }>();
  const issues: CanvasBindingIssue[] = [];
  const sourceOwners = new Map<string, string>();
  const speechCues: ProjectedSpeechCue[] = [];
  const speechTakes: ProjectedSpeechTake[] = [];
  let cursor = 0;
  for (const beat of plan.beats) {
    const startFrame = cursor;
    const candidates = new Map(beat.turns.map((turn) => [turn.id, speakingTake(plan, beat, turn)]));
    const candidateIds = [...candidates.values()].filter((item) => item != null).map((item) => item.placement.id);
    const seenSources = new Map<string, { start: number; end: number }>();
    const explicitTakes = takesByBeat.get(beat.id) ?? [];
    const explicitTurns = new Map<string, { start: number; end: number }>();
    let explicitDuration = 0;
    for (const take of explicitTakes) {
      const resource = resourcesById.get(take.resource_id);
      if (!resource) continue;
      const sourceOutUs = take.source_out_us ?? resource.duration_us;
      const sourceFrame = (us: number) => roundRatio(BigInt(us - take.source_in_us) * BigInt(plan.output.fps_num) * BigInt(take.speed_den), 1_000_000n * BigInt(plan.output.fps_den) * BigInt(take.speed_num));
      explicitDuration += take.lead_in_frames;
      const start = startFrame + explicitDuration;
      const end = start + sourceFrame(sourceOutUs);
      explicitDuration = end - startFrame;
      semantic.set(`take:${take.id}`, { start, end });
      speechTakes.push({ ...take, startFrame: start, endFrame: end, sourceOutUs });
      speechTakeRanges.set(take.origin_placement_id, { start, end, sourceOutUs });
      if (placementsById.has(take.origin_placement_id) && !placementsById.get(take.origin_placement_id)!.speech_take_id) pinnedSpeechPlacements.set(take.origin_placement_id, { start, end });
      for (const id of take.turn_ids) explicitTurns.set(id, { start, end });
      for (const cue of cuesByTake.get(take.id) ?? []) {
        const ready = cue.timing_status === "ready" && cue.source_start_us != null && cue.source_end_us != null;
        const ca = ready ? start + sourceFrame(cue.source_start_us!) : null;
        const cb = ready ? start + sourceFrame(cue.source_end_us!) : null;
        const valid = ca != null && cb != null && ca >= start && ca < cb && cb <= end;
        if (valid) semantic.set(`cue:${cue.id}`, { start: ca, end: cb });
        speechCues.push({ ...cue, startFrame: valid ? ca : null, endFrame: valid ? cb : null, issue: valid ? null : "台词需要校准或复核" });
        if (!valid) issues.push({ placement_id: take.origin_placement_id, code: "unresolved_speech_timing", message: `台词 ${cue.id} 需要校准或复核` });
      }
      for (const id of take.turn_ids) {
        if (!(cuesByTake.get(take.id) ?? []).some((cue) => cue.turn_id === id)) issues.push({ placement_id: take.origin_placement_id, code: "unresolved_speech_timing", message: `台词 ${id} 需要校准` });
      }
      for (const turn of beat.turns) {
        const timing = turn.speech_timing;
        if (!take.turn_ids.includes(turn.id) || !timing || timing.text !== turn.text || timing.media_hash !== resource.media_hash || timing.placement_id !== take.origin_placement_id) continue;
        const acceptedWords = new Set((cuesByTake.get(take.id) ?? []).filter((cue) => cue.turn_id === turn.id && cue.timing_status === "ready").flatMap((cue) => cue.word_ids));
        for (const word of timing.words) {
          if (!acceptedWords.has(word.id)) continue;
          if (word.start_us < take.source_in_us || word.end_us > sourceOutUs) continue;
          const wa = start + sourceFrame(word.start_us), wb = start + sourceFrame(word.end_us);
          semantic.set(`word:${word.id}`, { start: wa, end: wb });
          words.push({ id: word.id, text: word.text, turnId: turn.id, startFrame: wa, endFrame: wb });
        }
      }
    }
    for (const turn of beat.turns) {
      const accepted = speechCues.filter((cue) => cue.turn_id === turn.id && cue.startFrame != null);
      if (accepted.length) explicitTurns.set(turn.id, { start: Math.min(...accepted.map((cue) => cue.startFrame!)), end: Math.max(...accepted.map((cue) => cue.endFrame!)) });
    }
    const reflow = candidateIds.length > 0 && candidates.size > 0;
    let localCursor = 0;
    let originalCursor = 0;
    let lastSource: string | null = null;
    const turns = beat.turns.map((turn, index) => {
      const originalStart = turn.start_frame ?? Math.floor(beat.duration_frames * index / beat.turns.length);
      const originalEnd = turn.end_frame ?? Math.floor(beat.duration_frames * (index + 1) / beat.turns.length);
      const take = !explicitTakes.length && reflow ? candidates.get(turn.id) : null;
      const duplicate = take ? seenSources.get(take.placement.id) : null;
      if (take) {
        if (sourceOwners.has(take.placement.id) && sourceOwners.get(take.placement.id) !== beat.id || duplicate && lastSource !== take.placement.id) {
          issues.push({ placement_id: take.placement.id, code: "ambiguous_speech_instance", message: "共享发声素材需要明确分配播放实例" });
        }
        sourceOwners.set(take.placement.id, beat.id);
        lastSource = take.placement.id;
      }
      const explicit = explicitTurns.get(turn.id);
      if (explicitTakes.length && !explicit && beat.timing_mode !== "fixed") issues.push({ placement_id: explicitTakes[0].origin_placement_id, code: "unresolved_speech_timing", message: `台词 ${turn.id} 尚未分配发声素材实例` });

      // Beat-relative offsets already contain the estimated turn position.
      const leadIn = take ? take.placement.start.kind === 'beat'
        ? Math.max(0, take.startOffset - originalStart) : take.startOffset : 0;
      if (reflow && !duplicate && !explicit) localCursor += Math.max(0, originalStart - originalCursor) + leadIn;
      const start = explicit?.start ?? duplicate?.start ?? startFrame + (reflow ? localCursor : originalStart);
      if (reflow && !duplicate && !explicit) localCursor += take?.frames ?? originalEnd - originalStart;
      const end = explicit?.end ?? duplicate?.end ?? startFrame + (reflow ? localCursor : originalEnd);
      originalCursor = originalEnd;
      if (take) {
        seenSources.set(take.placement.id, { start, end });
        pinnedSpeechPlacements.set(take.placement.id, { start, end });
        speechTakeRanges.set(take.placement.id, { start, end, sourceOutUs: take.sourceOutUs });
      }
      semantic.set(`turn:${turn.id}`, { start, end });
      if (explicit && speechCues.some((cue) => cue.turn_id === turn.id && cue.startFrame != null)) estimatedTurns.delete(turn.id);
      else if (turn.timing_basis === 'estimated' || !turn.timing_basis) estimatedTurns.add(turn.id);
      for (const word of explicitTakes.length ? [] : turn.words ?? []) {
        const wordStart = start + word.start_frame - originalStart;
        const wordEnd = start + word.end_frame - originalStart;
        semantic.set(`word:${word.id}`, { start: wordStart, end: wordEnd });
        words.push({ id: word.id, text: word.text, turnId: turn.id, startFrame: wordStart, endFrame: wordEnd });
      }
      return { id: turn.id, speaker: turn.speaker, text: turn.text, startFrame: start, endFrame: end, estimated: explicit ? !speechCues.some((cue) => cue.turn_id === turn.id && cue.startFrame != null) : turn.timing_basis === 'estimated' || !turn.timing_basis };
    });
    let beatFrames = explicitTakes.length ? beat.timing_mode === 'fixed' ? beat.duration_frames : explicitDuration
      : reflow ? localCursor + Math.max(0, beat.duration_frames - originalCursor) : beat.duration_frames;
    if (beat.duration_source_placement_id) {
      const source = placementsById.get(beat.duration_source_placement_id);
      const media = resourcesById.get(source?.resource_id ?? '');
      if (source && media && media.duration_us > 0) {
        const sourceOut = source.source_out_us ?? media.duration_us;
        const span = sourceOut - (source.source_in_us ?? 0);
        if (span > 0 && sourceOut <= media.duration_us) {
          beatFrames = roundRatio(BigInt(span) * BigInt(plan.output.fps_num) * BigInt(source.speed_den ?? 1),
            1_000_000n * BigInt(plan.output.fps_den) * BigInt(source.speed_num ?? 1));
        } else issues.push({ placement_id: source.id, code: 'unresolved_media_duration', message: '素材使用范围无法确定段落时长' });
      } else issues.push({ placement_id: beat.duration_source_placement_id, code: 'unresolved_media_duration', message: '请先绑定时长依据素材' });
    }
    const endFrame = startFrame + beatFrames;
    if (explicitTakes.length && beat.timing_mode === "fixed" && explicitDuration > beat.duration_frames) issues.push({ placement_id: explicitTakes[0].origin_placement_id, code: "speech_exceeds_fixed_beat", message: "发声素材超过固定段落时长" });
    semantic.set(`beat:${beat.id}`, { start: startFrame, end: endFrame });
    beats.push({ id: beat.id, purpose: beat.purpose, startFrame, endFrame, turns });
    cursor = endFrame;
  }
  const baseSemantic = new Map(semantic);

  for (const beat of plan.beats) for (const turn of beat.turns) {
    if ((plan.speech_takes ?? []).some((take) => take.turn_ids.includes(turn.id))) continue;
    const timing = turn.speech_timing;
    if (!timing || timing.text !== turn.text || turn.speech_placement_id != null && turn.speech_placement_id !== timing.placement_id) continue;
    const placement = plan.placements.find((item) => item.id === timing.placement_id);
    const resource = plan.resources.find((item) => item.id === placement?.resource_id);
    if (!placement || !resource || resource.media_hash !== timing.media_hash || placement.loop) continue;
    if (![plan.output.fps_num, plan.output.fps_den, placement.speed_num ?? 1, placement.speed_den ?? 1].every((value) => Number.isSafeInteger(value) && value > 0)) continue;
    const audible = placement.kind === 'audio' && ['speech', 'narration'].includes(placement.role)
      && (resource.media_kind === 'audio' || resource.media_kind === 'video' && resource.has_audio);
    const videoWithAudio = placement.kind === 'visual' && resource.media_kind === 'video' && resource.has_audio;
    if (!audible && !videoWithAudio) continue;
    const directFrame = (anchor: PlanAnchorV2): number | null => {
      if (anchor.kind === 'program_frame') return anchor.frame ?? null;
      if (anchor.kind !== 'beat' && anchor.kind !== 'turn') return null;
      const edge = baseSemantic.get(`${anchor.kind}:${anchor.ref_id ?? ''}`);
      return edge && anchor.edge ? edge[anchor.edge] + (anchor.offset_frames ?? 0) : null;
    };
    const takeRange = speechTakeRanges.get(placement.id);
    const placementStart = takeRange?.start ?? directFrame(placement.start);
    const placementEnd = takeRange?.end ?? directFrame(placement.end);
    if (placementStart === null || placementEnd === null || placementStart < 0 || placementEnd > cursor || placementStart >= placementEnd) continue;
    const sourceIn = placement.source_in_us ?? 0;
    const sourceOut = takeRange?.sourceOutUs ?? placement.source_out_us ?? sourceIn + roundRatio(
      BigInt(placementEnd - placementStart) * BigInt(plan.output.fps_den) * 1_000_000n * BigInt(placement.speed_num ?? 1),
      BigInt(plan.output.fps_num) * BigInt(placement.speed_den ?? 1),
    );
    // Full-media recognition survives edits to the placement range. Projection
    // still requires each spoken word to fit inside the current edit below.
    const mapped: ProjectedWord[] = [];
    for (const word of timing.words) {
      if (word.start_us < sourceIn || word.end_us > sourceOut) break;
      const startFrame = placementStart + roundRatio(
        BigInt(word.start_us - sourceIn) * BigInt(plan.output.fps_num) * BigInt(placement.speed_den ?? 1),
        1_000_000n * BigInt(plan.output.fps_den) * BigInt(placement.speed_num ?? 1),
      );
      const endFrame = placementStart + roundRatio(
        BigInt(word.end_us - sourceIn) * BigInt(plan.output.fps_num) * BigInt(placement.speed_den ?? 1),
        1_000_000n * BigInt(plan.output.fps_den) * BigInt(placement.speed_num ?? 1),
      );
      if (startFrame < placementStart || endFrame > placementEnd || startFrame >= endFrame) break;
      mapped.push({ id: word.id, text: word.text, turnId: turn.id, startFrame, endFrame });
    }
    if (!mapped.length || mapped.length !== timing.words.length) continue;
    const projectedBeat = beats.find((item) => item.id === beat.id)!;
    const spokenStart = Math.min(...mapped.map((word) => word.startFrame));
    const spokenEnd = Math.max(...mapped.map((word) => word.endFrame));
    if (spokenStart < projectedBeat.startFrame || spokenEnd > projectedBeat.endFrame) continue;
    for (const word of mapped) { semantic.set(`word:${word.id}`, { start: word.startFrame, end: word.endFrame }); words.push(word); }
    pinnedSpeechPlacements.set(placement.id, { start: placementStart, end: placementEnd });
    semantic.set(`turn:${turn.id}`, { start: spokenStart, end: spokenEnd });
    const projectedTurn = projectedBeat.turns.find((item) => item.id === turn.id)!;
    projectedTurn.startFrame = spokenStart;
    projectedTurn.endFrame = spokenEnd;
    projectedTurn.estimated = false;
    estimatedTurns.delete(turn.id);
  }

  const textAnchorFrame = (anchor: PlanAnchorV2): { frame: number; estimated: boolean } => {
    const turn = plan.beats.flatMap((beat) => beat.turns).find((row) => row.id === anchor.ref_id);
    if (!turn || anchor.char_offset == null || !anchor.affinity) throw new Error('文字范围缺少有效台词边界');
    const timing = turn.speech_timing;
    if (!timing || timing.text !== turn.text || timing.text_hash !== anchor.text_hash)
      throw new Error(`台词 ${turn.id} 的文字范围已过期或尚无语音证据`);
    const take = speechTakes.find((row) => row.turn_ids.includes(turn.id) && row.origin_placement_id === timing.placement_id);
    const placement = placementsById.get(timing.placement_id);
    const origin = take ? take.startFrame : pinnedSpeechPlacements.get(timing.placement_id)?.start;
    if (origin == null || !placement) throw new Error(`台词 ${turn.id} 缺少可用发声素材`);
    const cue = take ? (cuesByTake.get(take.id) ?? []).find((row) => row.turn_id === turn.id && row.timing_status === 'ready') : undefined;
    const accepted = new Set(cue?.word_ids ?? []);
    const words = timing.words.filter((word) => (take ? accepted.has(word.id) : true) && semantic.has(`word:${word.id}`));
    const located = locateTextBoundary({ text: turn.text, charOffset: anchor.char_offset, affinity: anchor.affinity,
      words, cueStartUs: cue?.source_start_us, cueEndUs: cue?.source_end_us });
    if (located.sourceUs == null) throw new Error(`台词 ${turn.id} 的文字边界缺少可信时间：${located.issueCode}`);
    const sourceIn = take?.source_in_us ?? placement.source_in_us ?? 0;
    const speedNum = take?.speed_num ?? placement.speed_num ?? 1;
    const speedDen = take?.speed_den ?? placement.speed_den ?? 1;
    const frame = origin + roundRatio(BigInt(located.sourceUs - sourceIn) * BigInt(plan.output.fps_num) * BigInt(speedDen),
      1_000_000n * BigInt(plan.output.fps_den) * BigInt(speedNum)) + (anchor.offset_frames ?? 0);
    return { frame, estimated: located.quality === 'estimated' };
  };

  const selections = (plan.selections ?? []).map((item): ProjectedSelection => {
    let estimated = [item.start, item.end].some((anchor) => anchor.kind === 'turn' && estimatedTurns.has(anchor.ref_id ?? ''));
    try {
      const edgeFrame = (anchor: PlanAnchorV2): number => {
        if (anchor.kind === 'program_frame') {
          if (anchor.frame == null) throw new Error(`范围 ${item.label} 缺少节目帧`);
          return anchor.frame;
        }
        if (anchor.kind === 'selection' || anchor.kind === 'placement') throw new Error(`范围 ${item.label} 不能引用其他范围或条目`);
        if (anchor.kind === 'text') {
          const located = textAnchorFrame(anchor);
          estimated ||= located.estimated;
          return located.frame;
        }
        const target = semantic.get(`${anchor.kind}:${anchor.ref_id ?? ''}`);
        if (!target || !anchor.edge) throw new Error(`范围 ${item.label} 引用了不存在的${anchor.kind}`);
        return target[anchor.edge] + (anchor.offset_frames ?? 0);
      };
      const startFrame = edgeFrame(item.start);
      const endFrame = edgeFrame(item.end);
      const issue = startFrame < 0 || startFrame >= endFrame || endFrame > cursor ? '范围时间超出作品范围或结束早于开始' : null;
      if (!issue) semantic.set(`selection:${item.id}`, { start: startFrame, end: endFrame });
      return { ...item, startFrame, endFrame, estimated, issue };
    } catch (error) {
      return { ...item, startFrame: null, endFrame: null, estimated, issue: error instanceof Error ? error.message : String(error) };
    }
  });

  const byId = new Map(plan.placements.map((item) => [item.id, item]));
  const resolved = new Map<string, number>();
  const visiting = new Set<string>();
  const resolve = (id: string, edge: 'start' | 'end'): number => {
    const key = `${id}:${edge}`;
    const cached = resolved.get(key);
    if (cached !== undefined) return cached;
    const pinned = pinnedSpeechPlacements.get(id);
    if (pinned) return pinned[edge];
    if (visiting.has(key)) throw new Error(`条目 ${id} 的时间锚点存在循环`);
    const item = byId.get(id);
    if (!item) throw new Error(`找不到条目 ${id}`);
    visiting.add(key);
    try {
      const anchor = item[edge];
      let frame: number;
      if (anchor.kind === 'program_frame') {
        if (anchor.frame == null) throw new Error(`条目 ${id} 缺少节目帧`);
        frame = anchor.frame;
      } else if (anchor.kind === 'placement') {
        if (!anchor.ref_id || !anchor.edge) throw new Error(`条目 ${id} 缺少时间锚点`);
        frame = resolve(anchor.ref_id, anchor.edge) + (anchor.offset_frames ?? 0);
      } else if (anchor.kind === 'text') {
        frame = textAnchorFrame(anchor).frame;
      } else {
        const target = semantic.get(`${anchor.kind}:${anchor.ref_id ?? ''}`);
        if (!target || !anchor.edge) throw new Error(`条目 ${id} 引用了不存在的${anchor.kind}`);
        frame = target[anchor.edge] + (anchor.offset_frames ?? 0);
      }
      resolved.set(key, frame);
      return frame;
    } finally {
      visiting.delete(key);
    }
  };
  const placements = plan.placements.map((item): ProjectedPlacement => {
    try {
      const startFrame = resolve(item.id, 'start');
      const endFrame = resolve(item.id, 'end');
      const take = speechTakes.find((take) => take.id === item.speech_take_id || !item.speech_take_id && take.origin_placement_id === item.id);
      const source = take ? {
        speed_num: take.speed_num, speed_den: take.speed_den, volume: take.volume ?? 1,
        source_in_us: take.source_in_us + roundRatio(BigInt(startFrame - take.startFrame) * BigInt(plan.output.fps_den) * 1_000_000n * BigInt(take.speed_num), BigInt(plan.output.fps_num) * BigInt(take.speed_den)),
        sourceOutUs: endFrame === take.endFrame ? take.sourceOutUs : Math.min(take.sourceOutUs, take.source_in_us + roundRatio(BigInt(endFrame - take.startFrame) * BigInt(plan.output.fps_den) * 1_000_000n * BigInt(take.speed_num), BigInt(plan.output.fps_num) * BigInt(take.speed_den))),
      } : {};
      return { ...item, ...source, startFrame, endFrame, ...(!take && speechTakeRanges.has(item.id) ? { sourceOutUs: speechTakeRanges.get(item.id)!.sourceOutUs } : {}),
        issue: startFrame < 0 || startFrame >= endFrame || endFrame > cursor ? '条目时间超出作品范围或结束早于开始' : take && (startFrame < take.startFrame || endFrame > take.endFrame) ? '画面显示窗口超出发声素材实例' : null };
    } catch (error) {
      return { ...item, startFrame: null, endFrame: null, issue: error instanceof Error ? error.message : String(error) };
    }
  });
  return { durationFrames: cursor, beats, words, selections, placements, speechCues, speechTakes, issues };
}

function shiftAnchor(anchor: PlanAnchorV2, delta: number): PlanAnchorV2 {
  return anchor.kind === 'program_frame'
    ? { ...anchor, frame: Math.max(0, (anchor.frame ?? 0) + delta) }
    : { ...anchor, offset_frames: (anchor.offset_frames ?? 0) + delta };
}

function attachTakeConsumer(plan: ProductionPlanV2, id: string): ProductionPlanV2 {
  const take = (plan.speech_takes ?? []).find((take) => take.origin_placement_id === id);
  if (!take) return plan;
  return { ...plan, placements: plan.placements.map((item) => item.id === id ? { ...item, speech_take_id: take.id } : item) };
}

/** A drag keeps both semantic anchors and edits their offsets. */
export function movePlacement(plan: ProductionPlanV2, id: string, deltaFrames: number): ProductionPlanV2 {
  if (!Number.isInteger(deltaFrames)) return plan;
  plan = attachTakeConsumer(plan, id);
  const speaking = plan.placements.find((item) => item.id === id)?.speech_take_id ? null : speakingTakeForPlacement(plan, id);
  if (speaking) return { ...plan, placements: plan.placements.map((item) => item.id === id
    ? { ...item, start: { ...item.start, offset_frames: Math.max(0, (item.start.offset_frames ?? 0) + deltaFrames) } } : item) };
  return { ...plan, placements: plan.placements.map((item) => item.id === id
    ? { ...item, start: shiftAnchor(item.start, deltaFrames), end: shiftAnchor(item.end, deltaFrames) } : item) };
}

export function resizePlacement(plan: ProductionPlanV2, id: string, edge: 'start' | 'end', deltaFrames: number): ProductionPlanV2 {
  if (!Number.isInteger(deltaFrames)) return plan;
  plan = attachTakeConsumer(plan, id);
  const speaking = plan.placements.find((item) => item.id === id)?.speech_take_id ? null : speakingTakeForPlacement(plan, id);
  if (speaking) {
    const item = speaking.placement;
    const resource = plan.resources.find((row) => row.id === item.resource_id)!;
    const deltaUs = roundRatio(BigInt(deltaFrames) * BigInt(plan.output.fps_den) * 1_000_000n * BigInt(item.speed_num ?? 1),
      BigInt(plan.output.fps_num) * BigInt(item.speed_den ?? 1));
    return { ...plan, placements: plan.placements.map((row) => row.id === id ? {
      ...row,
      ...(edge === 'start'
        ? { source_in_us: Math.max(0, Math.min(speaking.sourceOutUs - 1, (row.source_in_us ?? 0) + deltaUs)) }
        : { source_out_us: Math.max((row.source_in_us ?? 0) + 1, Math.min(resource.duration_us, speaking.sourceOutUs + deltaUs)) }),
    } : row) };
  }
  return { ...plan, placements: plan.placements.map((item) => item.id === id
    ? { ...item, [edge]: shiftAnchor(item[edge], deltaFrames) } : item) };
}

/** Split at a program frame. Outer anchors survive; the new inner boundary is explicit. */
export function splitPlacement(plan: ProductionPlanV2, id: string, frame: number, newId: string): ProductionPlanV2 {
  const projected = projectProductionTimeline(plan).placements.find((item) => item.id === id);
  if (!projected || projected.startFrame == null || projected.endFrame == null || frame <= projected.startFrame || frame >= projected.endFrame || !newId || plan.placements.some((item) => item.id === newId)) return plan;
  const index = plan.placements.findIndex((item) => item.id === id);
  if (index < 0) return plan;
  const item = plan.placements[index];
  const take = (plan.speech_takes ?? []).find((take) => take.id === item.speech_take_id || take.origin_placement_id === item.id);
  if (take && item.kind === 'audio') return plan;
  if (take && item.kind === 'visual') {
    const takeRange = projectProductionTimeline(plan).speechTakes.find((row) => row.id === take.id)!;
    const boundary: PlanAnchorV2 = { kind: 'take', ref_id: take.id, edge: 'start', offset_frames: frame - takeRange.startFrame };
    const { source_in_us: _in, source_out_us: _out, speed_num: _num, speed_den: _den, ...consumer } = item;
    void _in; void _out; void _num; void _den;
    const first: PlanPlacementV2 = { ...consumer, speech_take_id: take.id, end: boundary };
    const second: PlanPlacementV2 = { ...consumer, id: newId, speech_take_id: take.id, start: boundary };
    return { ...plan, placements: [...plan.placements.slice(0, index), first, second, ...plan.placements.slice(index + 1)] };
  }
  const sourceStart = item.source_in_us ?? 0;
  const sourceEnd = item.source_out_us;
  const splitUs = sourceEnd == null
    ? Math.round(sourceStart + (frame - projected.startFrame) * plan.output.fps_den / plan.output.fps_num * 1_000_000 * (item.speed_num ?? 1) / (item.speed_den ?? 1))
    : Math.round(sourceStart + (sourceEnd - sourceStart) * (frame - projected.startFrame) / (projected.endFrame - projected.startFrame));
  const first: PlanPlacementV2 = { ...item, end: { kind: 'program_frame', frame }, source_out_us: splitUs };
  const second: PlanPlacementV2 = { ...item, id: newId, start: { kind: 'program_frame', frame }, source_in_us: splitUs };
  return { ...plan, placements: [...plan.placements.slice(0, index), first, second, ...plan.placements.slice(index + 1)] };
}

/** Public mainline projection shares the composition resolver. */
export function projectSpeechMainline(plan: ProductionPlanV2): ProductionTimelineProjection {
  return projectProductionTimeline(plan);
}
