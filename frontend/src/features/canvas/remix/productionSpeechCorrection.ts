import type { PlanSourceWordV2, ProductionPlanV2 } from './productionPlanV2Model';
import { identifyRecognizedWords } from './productionSpeechWordIdentity';

type AcceptedCue = { id: string; turn_id: string; text: string; start_us: number; end_us: number };
type CorrectedWord = Omit<PlanSourceWordV2, 'id'>;
type EvidenceRef = { artifact_id: string; revision: number };

/** Preflight and apply an import atomically; referenced semantic targets must stay explicit. */
export function applyAcceptedSpeechCues(
  plan: ProductionPlanV2, takeId: string, accepted: AcceptedCue[], basis: 'manual' | 'srt_import',
  evidenceRef: EvidenceRef, corrected?: CorrectedWord[], textHashes: Record<string, string> = {},
): void {
  const old = (plan.speech_cues ?? []).filter((cue) => cue.take_id === takeId && accepted.some((row) => row.turn_id === cue.turn_id));
  const retained = new Set(accepted.map((row) => row.id));
  const removedCues = new Set(basis === 'srt_import' ? old.filter((cue) => !retained.has(cue.id)).map((cue) => cue.id) : []);
  const removedWords = new Set(corrected?.length ? [] : old.flatMap((cue) => cue.word_ids));
  for (const item of [...plan.placements, ...(plan.selections ?? [])]) for (const anchor of [item.start, item.end]) {
    if (anchor.kind === 'word' && removedWords.has(anchor.ref_id ?? '')) throw new Error('素材 SRT 只有整句时间，已有词锚点需要先调整，导入尚未保存');
    if (anchor.kind === 'cue' && removedCues.has(anchor.ref_id ?? '')) throw new Error('导入将替换已使用的台词行，请先调整相关句锚点，导入尚未保存');
  }
  const next = structuredClone(plan);
  next.speech_cues = (next.speech_cues ?? []).filter((cue) => !removedCues.has(cue.id));
  for (const row of accepted) {
    const previous = next.speech_cues.find((cue) => cue.id === row.id);
    const cue = { id: row.id, turn_id: row.turn_id, take_id: takeId, text: row.text,
      source_start_us: row.start_us, source_end_us: row.end_us, timing_source: basis,
      timing_status: 'ready' as const, word_ids: corrected?.length ? previous?.word_ids ?? [] : [], evidence_ref: evidenceRef };
    const index = next.speech_cues.findIndex((item) => item.id === row.id);
    if (index >= 0) next.speech_cues[index] = cue; else next.speech_cues.push(cue);
  }
  if (corrected?.length) applyCorrectedSpeechWords(next, takeId, accepted, corrected, evidenceRef, textHashes);
  Object.assign(plan, next);
}

/** Attach reviewed source words to accepted cues without moving their acoustic times. */
export function applyCorrectedSpeechWords(
  plan: ProductionPlanV2, takeId: string, accepted: AcceptedCue[], corrected: CorrectedWord[],
  evidenceRef: EvidenceRef, textHashes: Record<string, string>,
): void {
  const take = plan.speech_takes?.find((item) => item.id === takeId);
  const resource = plan.resources.find((item) => item.id === take?.resource_id);
  if (!take || !resource || !corrected.length) throw new Error('发声素材或逐词证据缺失');
  const assigned = new Set<number>();
  const updates: Array<{ turnId: string; words: PlanSourceWordV2[] }> = [];
  const priorIds = new Set<string>(); const retainedIds = new Set<string>();
  for (const cue of plan.speech_cues ?? []) if (cue.take_id === takeId) {
    for (const id of cue.word_ids) priorIds.add(id);
  }
  for (const row of accepted) {
    if (!take.turn_ids.includes(row.turn_id)) throw new Error('台词不属于当前发声素材');
    const turn = plan.beats.flatMap((beat) => beat.turns).find((item) => item.id === row.turn_id);
    if (!turn || turn.text !== row.text || !textHashes[turn.id]) throw new Error('台词或文字版本已变化');
    const selected = corrected.flatMap((word, index) => {
      if (word.start_us < row.start_us || word.end_us > row.end_us) return [];
      if (assigned.has(index)) throw new Error('逐词证据跨越多句台词');
      assigned.add(index); return [word];
    });
    if (!selected.length) throw new Error('这句台词没有对应的真实逐词时间');
    const previous = turn.speech_timing?.words ?? [];
    for (const word of previous) priorIds.add(word.id);
    const words = identifyRecognizedWords(turn.id, evidenceRef.artifact_id, evidenceRef.revision, selected, previous);
    for (const word of words) retainedIds.add(word.id);
    updates.push({ turnId: turn.id, words });
  }
  if (assigned.size !== corrected.length) throw new Error('有识别词未能分配到作品方案台词');
  const removed = new Set([...priorIds].filter((id) => !retainedIds.has(id)));
  const anchored = [...plan.placements, ...(plan.selections ?? [])].some((item) =>
    [item.start, item.end].some((anchor) => anchor.kind === 'word' && removed.has(anchor.ref_id ?? '')));
  if (anchored) throw new Error('逐词校正改变了已使用的词锚点；请先调整相关镜头或字幕后重试');

  for (const { turnId, words } of updates) {
    const turn = plan.beats.flatMap((beat) => beat.turns).find((item) => item.id === turnId)!;
    turn.speech_timing = {
      placement_id: take.origin_placement_id, media_hash: resource.media_hash,
      text: turn.text, text_hash: textHashes[turnId], evidence_ref: evidenceRef,
      words,
      ...(take.source_out_us != null ? { source_in_us: take.source_in_us, source_out_us: take.source_out_us } : {}),
    };
    for (const cue of plan.speech_cues ?? []) if (cue.take_id === takeId && cue.turn_id === turnId) {
      cue.word_ids = words.filter((word) => cue.source_start_us != null && cue.source_end_us != null
        && word.start_us >= cue.source_start_us && word.end_us <= cue.source_end_us).map((word) => word.id);
      cue.evidence_ref = evidenceRef;
    }
  }
}
