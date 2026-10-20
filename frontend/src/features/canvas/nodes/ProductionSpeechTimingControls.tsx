import { useEffect, useMemo, useRef, useState } from 'react';
import { fetchRemixArtifact, fetchRemixOperation, fetchRemixTaskStatus, submitRemixProcessAction, type RemixArtifactRef } from '@/api/remix';
import type { PlanSpeechTimingV2, PlanTurnV2, ProductionPlanV2 } from '@/features/canvas/remix/productionPlanV2Model';
import { sourceRangeForPlacement } from '@/features/canvas/compose/productionComposeModel';
import { flushProductionPlanV2AutoSave } from '@/features/canvas/remix/productionPlanV2AutoSave';
import { plannedSpeechSourcePlacementsForTurn, sharedSpeechTakeSourceBlocker, speechSourceCandidatesForTurn, speechTurnsForPlacement } from '@/features/canvas/remix/productionSpeechReadiness';
import { identifyRecognizedWords } from '@/features/canvas/remix/productionSpeechWordIdentity';
import { recognizedWordsToSourceSrt, type RecognizedTimedWord } from '@/features/canvas/remix/productionSrt';
import { useCanvasStore } from '@/stores/canvasStore';

interface PendingAlignment { operationId: string; planRef: RemixArtifactRef; turnId: string; turnIds?: string[]; turnTexts?: Record<string, string>; placementId: string; takeId?: string; mediaHash: string; text: string; sourceInUs: number; sourceOutUs: number }
interface AlignmentReview { status: 'no_speech' | 'script_mismatch' | 'duration_mismatch' | 'erhua_variant'; artifactRef: RemixArtifactRef; placementId: string; mediaHash: string; text: string; sourceInUs: number; sourceOutUs: number; message: string }

function formatSourceTime(microseconds: number): string {
  return (microseconds / 1_000_000).toFixed(3);
}

function pendingFor(nodeId: string, turnId: string): PendingAlignment | null {
  const data = useCanvasStore.getState().nodes.find((node) => node.id === nodeId)?.data;
  return ((data?.productionSpeechOperations as Record<string, PendingAlignment> | undefined) ?? {})[turnId] ?? null;
}

function savePending(nodeId: string, turnIds: string[], value: PendingAlignment | null): void {
  const store = useCanvasStore.getState();
  const data = store.nodes.find((node) => node.id === nodeId)?.data;
  const operations = { ...((data?.productionSpeechOperations as Record<string, PendingAlignment> | undefined) ?? {}) };
  for (const turnId of turnIds) {
    if (value) operations[turnId] = value;
    else delete operations[turnId];
  }
  store.updateNodeData(nodeId, { productionSpeechOperations: operations });
}

function reviewFor(nodeId: string, turnId: string): AlignmentReview | null {
  const data = useCanvasStore.getState().nodes.find((node) => node.id === nodeId)?.data;
  return ((data?.productionSpeechReviews as Record<string, AlignmentReview> | undefined) ?? {})[turnId] ?? null;
}

function saveReviews(nodeId: string, updates: Record<string, AlignmentReview | null>): void {
  const store = useCanvasStore.getState();
  const data = store.nodes.find((node) => node.id === nodeId)?.data;
  const reviews = { ...((data?.productionSpeechReviews as Record<string, AlignmentReview> | undefined) ?? {}) };
  for (const [turnId, value] of Object.entries(updates)) {
    if (value) reviews[turnId] = value;
    else delete reviews[turnId];
  }
  store.updateNodeData(nodeId, { productionSpeechReviews: reviews });
}

export function ProductionSpeechTimingControls({ plan, turn, label, project, nodeId, onBind, onAccept, onRecognizedSrt }: {
  plan: ProductionPlanV2; turn: PlanTurnV2; label: string; project: string | null | undefined; nodeId: string;
  onBind: (placementId: string | null) => Promise<boolean>; onAccept: (timings: Record<string, PlanSpeechTimingV2 | null>) => void;
  onRecognizedSrt?: (srt: string, ref: RemixArtifactRef, words: RecognizedTimedWord[], transcript: string) => void;
}) {
  const [pending, setPending] = useState<PendingAlignment | null>(() => pendingFor(nodeId, turn.id));
  const [review, setReview] = useState<AlignmentReview | null>(() => reviewFor(nodeId, turn.id));
  const [message, setMessage] = useState<string | null>(null);
  const [preparingSrt, setPreparingSrt] = useState(false);
  const [changingSource, setChangingSource] = useState(false);
  const acceptRef = useRef(onAccept);
  acceptRef.current = onAccept;
  useEffect(() => {
    setPending(pendingFor(nodeId, turn.id));
    setReview(reviewFor(nodeId, turn.id));
  }, [plan, nodeId, turn.id]);
  const candidates = useMemo(() => speechSourceCandidatesForTurn(plan, turn.id).flatMap((placement) => {
    const resource = plan.resources.find((item) => item.id === placement.resource_id);
    if (!resource) return [];
    return [{ placement, resource }];
  }), [plan, turn.id]);
  const plannedCandidates = useMemo(() => plannedSpeechSourcePlacementsForTurn(plan, turn.id), [plan, turn.id]);
  const take = plan.speech_takes?.find((item) => item.turn_ids.includes(turn.id));
  const takeResource = take && plan.resources.find((item) => item.id === take.resource_id);
  const takeChoice = take && takeResource ? { placement: plan.placements.find((item) => item.id === take.origin_placement_id)
    ?? { id: take.origin_placement_id, kind: 'visual' as const, description: '本段发声素材' }, resource: takeResource } : null;
  const selectedId = take?.origin_placement_id ?? (turn.speech_placement_id === undefined ? turn.speech_timing?.placement_id ?? '' : turn.speech_placement_id ?? '');
  const selected = takeChoice ?? candidates.find((item) => item.placement.id === selectedId);
  const plannedSelection = plannedCandidates.find((item) => item.id === selectedId);
  const group = take ? plan.beats.flatMap((beat) => beat.turns).filter((item) => take.turn_ids.includes(item.id))
    : selectedId ? speechTurnsForPlacement(plan, selectedId) : [turn];
  const sharedSource = group.length > 1;
  const groupLeader = group[0]?.id === turn.id;
  const sharedTake = plan.speech_takes?.find((item) => item.turn_ids.length > 1
    && group.some((peer) => item.turn_ids.includes(peer.id)));
  const sharedTakeBlocker = sharedTake ? sharedSpeechTakeSourceBlocker(plan, sharedTake.id) : null;
  const lockedSharedTake = Boolean(sharedTakeBlocker);
  const wrongBeat = Boolean(selectedId && !selected && !plannedSelection);
  const sourceRange = take && takeResource ? { sourceInUs: take.source_in_us, sourceOutUs: take.source_out_us ?? takeResource.duration_us }
    : selected ? sourceRangeForPlacement(plan, selected.placement.id) : null;
  const timing = turn.speech_timing;
  const hasCurrentTiming = Boolean(timing && timing.text === turn.text && timing.placement_id === selectedId
    && selected?.resource.media_hash === timing.media_hash);
  const acceptedCues = (plan.speech_cues ?? []).filter((cue) => cue.turn_id === turn.id);
  const correctedSrtReady = acceptedCues.length > 0 && acceptedCues.every((cue) => cue.timing_source === 'srt_import' && cue.timing_status === 'ready');
  const fullSourceEvidence = timing?.source_in_us === 0 && timing?.source_out_us === selected?.resource.duration_us;
  const legacyWords = Boolean(timing?.words.length && (!fullSourceEvidence || !acceptedCues.length || acceptedCues.some((cue) => cue.timing_status !== 'ready')));
  const aligned = Boolean(!legacyWords && hasCurrentTiming && timing && sourceRange
    && selected?.resource.media_hash === timing.media_hash
    && timing.words.every((word) => word.start_us >= sourceRange.sourceInUs && word.end_us <= sourceRange.sourceOutUs));
  const activeReview = review && review.text === turn.text && review.placementId === selectedId
    && review.mediaHash === selected?.resource.media_hash
    && review.sourceInUs === 0 && review.sourceOutUs === selected?.resource.duration_us ? review : null;
  const reviewMessage = correctedSrtReady || aligned && activeReview?.status === 'duration_mismatch' ? null : activeReview?.message;
  const state = pending ? '校准中…' : correctedSrtReady ? '已确认素材 SRT' : legacyWords ? `旧识别 ${timing!.words.length} 词 · 需完整校准` : aligned ? `已校准 ${timing!.words.length} 个词`
    : activeReview ? activeReview.status === 'no_speech' ? '无语音 · 可跳过' : activeReview.status === 'erhua_variant' ? '儿化差异 · 待试听' : activeReview.status === 'duration_mismatch' ? '台词超出素材剪辑范围 · 需复核' : '台词与录音不一致 · 需复核'
    : wrongBeat ? '原选素材不在本段 · 需重选' : plannedSelection && !selected ? '来源待准备真实素材' : hasCurrentTiming ? '台词已识别 · 素材剪辑范围待复核' : timing ? '台词或发声素材已变化，需重新校准' : selected && sharedSource && !groupLeader ? '随同源视频一起校准' : selected ? '待校准' : turn.speech_placement_id === null ? '无发声' : '尚未选择发声来源';

  useEffect(() => {
    // Every turn in a shared take stores the same operation. Only the turn that
    // submitted it polls; its result updates the other turns in one plan edit.
    if (!pending || !project || pending.turnId !== turn.id) return;
    let cancelled = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const operation = await fetchRemixOperation(project, pending.operationId);
        if (cancelled) return;
        if (['failed', 'cancelled', 'stale'].includes(operation.status)) throw new Error('台词时间校准失败，请在任务中心查看详情');
        if (operation.status !== 'needs_confirmation' && operation.status !== 'completed') {
          if (operation.task_type && operation.task_scope) {
            const task = await fetchRemixTaskStatus(project, operation.task_type, operation.task_scope);
            if (task?.status === 'failed') throw new Error(task.error || '台词时间校准失败');
          }
          if (!cancelled) timer = window.setTimeout(poll, 1500);
          return;
        }
        const output = operation.output_refs.find((item) => item.artifact_id && item.revision);
        if (!output?.artifact_id || !output.revision) throw new Error('校准任务没有返回逐词产物');
        const artifact = await fetchRemixArtifact(project, { artifactId: output.artifact_id, revision: output.revision });
        if (cancelled) return;
        const payload = artifact.payload;
        const source = useCanvasStore.getState().nodes.find((node) => node.id === nodeId)?.data?.productionPlanV2Draft as ProductionPlanV2 | undefined;
        const turnIds = pending.turnIds ?? [pending.turnId];
        const turnTexts = pending.turnTexts ?? { [pending.turnId]: pending.text };
        const currentTake = source?.speech_takes?.find((item) => item.id === pending.takeId);
        const currentTurns = source ? currentTake
          ? source.beats.flatMap((beat) => beat.turns).filter((item) => currentTake.turn_ids.includes(item.id))
          : speechTurnsForPlacement(source, pending.placementId) : [];
        const currentPlacement = source?.placements.find((item) => item.id === pending.placementId);
        const currentResource = source?.resources.find((item) => item.id === (currentTake?.resource_id ?? currentPlacement?.resource_id));
        const currentRange = currentTake && currentResource ? { sourceInUs: currentTake.source_in_us,
          sourceOutUs: currentTake.source_out_us ?? currentResource.duration_us }
          : source ? sourceRangeForPlacement(source, pending.placementId) : null;
        const linked = artifact.upstream_refs?.some((ref) => ref.artifact_id === pending.planRef.artifactId && ref.revision === pending.planRef.revision);
        if (artifact.schema !== 'word_timeline.v1' || !linked || payload.turn_id !== pending.turnId
          || payload.placement_id !== pending.placementId || payload.media_hash !== `sha256:${pending.mediaHash}`
          || payload.source_in_us !== pending.sourceInUs || payload.source_out_us !== pending.sourceOutUs
          || currentTurns.length !== turnIds.length || currentTurns.some((item, index) => item.id !== turnIds[index] || item.text !== turnTexts[item.id])
          || Boolean(pending.takeId) !== Boolean(currentTake)
          || currentTake && currentTake.origin_placement_id !== pending.placementId
          || currentResource?.media_hash !== pending.mediaHash
          || currentResource.duration_us !== pending.sourceOutUs
          || turnIds.length > 1 && (JSON.stringify(payload.group_turn_ids) !== JSON.stringify(turnIds))) throw new Error('台词或发声素材已变化，请重新校准');
        if (payload.status !== 'completed') {
          acceptRef.current(Object.fromEntries(turnIds.map((id) => [id, null])));
          const mismatch = payload.mismatch as { report?: { normalized_asr?: unknown; omissions?: Array<{ script_range?: { text?: unknown } }> } } | undefined;
          const heard = typeof mismatch?.report?.normalized_asr === 'string' ? `识别到：${mismatch.report.normalized_asr.slice(0, 180)}。` : '';
          const missing = mismatch?.report?.omissions?.map((item) => item.script_range?.text).filter((text): text is string => typeof text === 'string' && Boolean(text.trim())) ?? [];
          const missingHint = missing.length ? `识别缺少：${missing.slice(0, 3).join('、')}。` : '';
          const unusedTail = currentRange && currentRange.sourceOutUs + 100_000 < pending.sourceOutUs
            ? `素材全长 ${(pending.sourceOutUs / 1_000_000).toFixed(2)} 秒，当前编排播放到 ${(currentRange.sourceOutUs / 1_000_000).toFixed(2)} 秒。` : '';
          const nextReview: AlignmentReview = {
            status: payload.status === 'no_speech' ? 'no_speech' : 'script_mismatch',
            artifactRef: { artifactId: artifact.id, revision: artifact.revision },
            placementId: pending.placementId, mediaHash: pending.mediaHash, text: pending.text,
            sourceInUs: pending.sourceInUs, sourceOutUs: pending.sourceOutUs,
            message: payload.status === 'no_speech' ? '这段素材没有识别到语音；可继续按画面和时间编排。'
              : `${turnIds.length > 1 ? '同一视频的完整语音与这几句台词不一致。' : '完整素材的识别结果与台词不一致。'}${heard}${missingHint}${unusedTail}请试听核对台词或发声素材。`,
          };
          saveReviews(nodeId, Object.fromEntries(turnIds.map((id) => [id, { ...nextReview, text: turnTexts[id] }])));
          setReview({ ...nextReview, text: turnTexts[pending.turnId] });
          setMessage(null);
        } else {
          const groupRefs = payload.group_refs as Array<{ turn_id?: unknown; artifact_id?: unknown; revision?: unknown }> | undefined;
          if (turnIds.length > 1 && (!Array.isArray(groupRefs) || groupRefs.length !== turnIds.length - 1
            || groupRefs.some((ref, index) => ref.turn_id !== turnIds.filter((id) => id !== pending.turnId)[index]
              || typeof ref.artifact_id !== 'string' || !Number.isInteger(ref.revision)))) throw new Error('同源台词产物不完整，请重新校准');
          const artifacts = [artifact, ...await Promise.all((groupRefs ?? []).map((ref) => fetchRemixArtifact(project, {
            artifactId: ref.artifact_id as string, revision: ref.revision as number,
          })))];
          if (cancelled) return;
          const timings: Record<string, PlanSpeechTimingV2 | null> = {};
          const reviews: Record<string, AlignmentReview | null> = {};
          for (const [index, id] of turnIds.entries()) {
            const timedArtifact = artifacts.find((item) => item.payload.turn_id === id);
            const timed = timedArtifact?.payload;
            const words = timed?.words;
            const linkedPlan = timedArtifact?.upstream_refs?.some((ref) => ref.artifact_id === pending.planRef.artifactId && ref.revision === pending.planRef.revision);
            if (!timedArtifact || timedArtifact.schema !== 'word_timeline.v1' || !linkedPlan || timed?.status !== 'completed'
              || timed.placement_id !== pending.placementId || timed.media_hash !== `sha256:${pending.mediaHash}`
              || timed.source_in_us !== pending.sourceInUs || timed.source_out_us !== pending.sourceOutUs
              || timed.transcript !== undefined && timed.transcript !== turnTexts[id]
              || typeof timed.text_hash !== 'string' || !Array.isArray(words) || !words.length
              || artifacts.filter((item) => item.payload.turn_id === id).length !== 1) throw new Error(`第 ${index + 1} 句的逐词时间产物无效`);
            timings[id] = { placement_id: pending.placementId, media_hash: pending.mediaHash,
              text: turnTexts[id], text_hash: timed.text_hash, source_in_us: pending.sourceInUs, source_out_us: pending.sourceOutUs,
              evidence_ref: { artifact_id: timedArtifact.id, revision: timedArtifact.revision },
              words: identifyRecognizedWords(id, timedArtifact.id, timedArtifact.revision,
                words.map((word) => ({ text: String(word.text), start_us: Number(word.start_us), end_us: Number(word.end_us),
                  ...(Number.isInteger(word.start_char) && Number.isInteger(word.end_char) ? { start_char: word.start_char as number, end_char: word.end_char as number } : {}) })),
                source?.beats.flatMap((beat) => beat.turns).find((item) => item.id === id)?.speech_timing?.words ?? []) };
            const matchReport = timed.match_report as { omissions?: Array<{ script_range?: { text?: string } }> } | undefined;
            const erhuaCount = matchReport?.omissions?.filter((item) => item.script_range?.text === '儿').length ?? 0;
            const wordStart = Math.min(...words.map((word) => Number(word.start_us)));
            const wordEnd = Math.max(...words.map((word) => Number(word.end_us)));
            const exceedsEdit = !currentRange || wordStart < currentRange.sourceInUs || wordEnd > currentRange.sourceOutUs;
            reviews[id] = exceedsEdit || erhuaCount ? {
              status: exceedsEdit ? 'duration_mismatch' : 'erhua_variant', artifactRef: { artifactId: timedArtifact.id, revision: timedArtifact.revision },
              placementId: pending.placementId, mediaHash: pending.mediaHash, text: turnTexts[id],
              sourceInUs: pending.sourceInUs, sourceOutUs: pending.sourceOutUs,
              message: exceedsEdit ? currentRange
                ? `完整素材已识别到这句台词，结尾在 ${(wordEnd / 1_000_000).toFixed(2)} 秒；当前剪辑只播放到 ${(currentRange.sourceOutUs / 1_000_000).toFixed(2)} 秒。请检查源素材入出点，识别结果会保留。`
                : '完整素材已识别到这句台词，但当前剪辑范围无效；请检查源素材入出点，识别结果会保留。'
                : `识别文本有 ${erhuaCount} 处儿化差异，已归入相邻词的时间；请试听确认。`,
            } : null;
          }
          saveReviews(nodeId, reviews);
          acceptRef.current(timings);
          setReview(reviews[pending.turnId]);
          setMessage(null);
        }
        savePending(nodeId, turnIds, null);
        setPending(null);
      } catch (cause) {
        if (cancelled) return;
        setMessage(cause instanceof Error ? cause.message : String(cause));
        savePending(nodeId, pending.turnIds ?? [pending.turnId], null);
        setPending(null);
      }
    };
    void poll();
    return () => { cancelled = true; if (timer !== undefined) window.clearTimeout(timer); };
  }, [pending, project, nodeId]);

  const start = async () => {
    if (!project || !selected || pending || sharedSource && !groupLeader) return;
    setMessage(null);
    try {
      const planRef = await flushProductionPlanV2AutoSave(project, nodeId);
      if (!planRef) throw new Error('请先保存作品方案');
      const task = await submitRemixProcessAction(project, {
        action: 'align_production_speech', mediaPath: selected.resource.media_path,
        planRef, turnId: turn.id, ...(take ? { takeId: take.id } : { placementId: selected.placement.id }),
        idempotencyKey: crypto.randomUUID(), nodeRevision: planRef.revision,
      });
      const turnIds = group.map((item) => item.id);
      const turnTexts = Object.fromEntries(group.map((item) => [item.id, item.text]));
      const next = { operationId: task.operation_id, planRef, turnId: turn.id, turnIds, turnTexts,
        placementId: selected.placement.id, takeId: take?.id, mediaHash: selected.resource.media_hash, text: turn.text,
        sourceInUs: 0, sourceOutUs: selected.resource.duration_us };
      savePending(nodeId, turnIds, next);
      saveReviews(nodeId, Object.fromEntries(turnIds.map((id) => [id, null])));
      setReview(null);
      setPending(next);
    } catch (cause) { setMessage(cause instanceof Error ? cause.message : String(cause)); }
  };

  const openRecognizedSrt = async () => {
    if (!project || !selected || !activeReview || !onRecognizedSrt || preparingSrt) return;
    setPreparingSrt(true);
    setMessage(null);
    try {
      const artifact = await fetchRemixArtifact(project, activeReview.artifactRef);
      const payload = artifact.payload;
      if (artifact.schema !== 'word_timeline.v1' || artifact.id !== activeReview.artifactRef.artifactId
        || artifact.revision !== activeReview.artifactRef.revision || payload.status !== 'script_mismatch'
        || payload.media_hash !== `sha256:${selected.resource.media_hash}`
        || payload.placement_id !== selected.placement.id || payload.source_in_us !== 0
        || payload.source_out_us !== selected.resource.duration_us) throw new Error('识别产物与当前发声素材不一致，请重新校准');
      const words = payload.recognized_words;
      if (!Array.isArray(words)) throw new Error('这次识别没有保留词时间，请重新校准后编辑 SRT');
      const transcript = typeof payload.recognized_transcript === 'string' ? payload.recognized_transcript : '';
      const timedWords = words.map((word) => ({ text: String(word.text), start_us: Number(word.start_us), end_us: Number(word.end_us), start_char: Number(word.start_char), end_char: Number(word.end_char) }));
      const srt = recognizedWordsToSourceSrt(timedWords, selected.resource.duration_us, transcript);
      onRecognizedSrt(srt, activeReview.artifactRef, timedWords, transcript);
    } catch (cause) {
      setMessage(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setPreparingSrt(false);
    }
  };

  const changeSource = async (placementId: string | null) => {
    if (changingSource || lockedSharedTake) return;
    setChangingSource(true);
    try {
      const saved = await onBind(placementId);
      const latest = useCanvasStore.getState().nodes.find((node) => node.id === nodeId)?.data.productionPlanV2Draft as ProductionPlanV2 | undefined;
      const persisted = latest?.beats.flatMap((beat) => beat.turns).find((item) => item.id === turn.id)?.speech_placement_id;
      if (!saved || persisted !== placementId || persisted === turn.speech_placement_id) {
        if (!saved) setMessage('声音来源未保存，请查看方案错误提示。');
        return;
      }
      saveReviews(nodeId, Object.fromEntries(group.map((item) => [item.id, null])));
      setReview(null);
      setMessage(null);
    } catch (cause) { setMessage(cause instanceof Error ? cause.message : String(cause)); }
    finally { setChangingSource(false); }
  };

  return <div className="col-span-2 flex flex-wrap items-center gap-2 text-xs">
    {sharedSource && !groupLeader ? <span className="min-w-[220px] flex-1 text-text-muted">发声素材 · 与同源台词共用</span> : <label className="min-w-[220px] flex-1">发声素材
      <select aria-label={`${label} 发声素材`} className="tap-field w-full"
        value={turn.speech_placement_id === null ? '__none__' : selected || plannedSelection ? selectedId : ''}
        disabled={changingSource || lockedSharedTake}
        onChange={(event) => { void changeSource(event.target.value === '__none__' ? null : event.target.value); }}>
        <option value="" disabled>尚未选择</option>
        <option value="__none__">无发声</option>
        {takeChoice && !plannedCandidates.some((item) => item.id === takeChoice.placement.id) && <option value={takeChoice.placement.id}>{takeChoice.placement.description || takeChoice.resource.media_path.split('/').pop()} · 已绑定发声实例</option>}
        {plannedCandidates.map((placement) => {
          const resource = plan.resources.find((item) => item.id === placement.resource_id);
          return <option key={placement.id} value={placement.id}>{placement.description || resource?.media_path.split('/').pop() || placement.id} · {placement.kind === 'visual' ? '视频原声' : placement.role === 'narration' ? '旁白' : '人声轨'}{resource ? '' : ' · 待准备素材'}</option>;
        })}
      </select>
    </label>}
    <span className={aligned || correctedSrtReady ? 'text-emerald-300' : 'text-amber-300'}>{state}</span>
    {(!sharedSource || groupLeader) && <button className="tap-button" aria-label={sharedSource ? `校准 ${label.split(' 台词')[0]} 同源 ${group.length} 句时间` : `校准 ${label} 时间`} disabled={!selected || !project || Boolean(pending)} onClick={() => void start()}>{sharedSource ? `校准同源 ${group.length} 句台词` : '校准台词时间'}</button>}
    {activeReview?.status === 'script_mismatch' && onRecognizedSrt && <button className="tap-button" disabled={preparingSrt} onClick={() => void openRecognizedSrt()}>{preparingSrt ? '读取识别时间…' : '校正识别词'}</button>}
    {sharedSource && !groupLeader && <span className="text-text-muted">与同源台词共用素材，请在第一句统一校准</span>}
    {sharedSource && groupLeader && sharedTakeBlocker && <span className="basis-full text-amber-300">{sharedTakeBlocker}</span>}
    {wrongBeat && <span className="basis-full text-amber-300">原选素材不在本段，请重新选择本段发声素材。</span>}
    {wrongBeat && <button className="tap-button" aria-label={`清除 ${label} 跨段绑定`} disabled={changingSource || lockedSharedTake} onClick={() => { void changeSource(null); }}>清除原选素材</button>}
    {selected && !sourceRange && <span className="basis-full text-amber-300">当前编排范围与素材时长不符；仍可先识别完整素材，再调整编排时间。</span>}
    {!candidates.length && <span className="basis-full text-text-muted">本段画面没有音轨或尚未绑定发声素材。可先指定画面或旁白来源，再绑定带声音的视频或音频；无语音作品可清除绑定后继续。</span>}
    {(message || reviewMessage) && <span role="alert" className="basis-full text-amber-300">{message || reviewMessage}</span>}
    {hasCurrentTiming && timing && <details className="basis-full rounded-lg border border-[var(--ui-border-soft)] p-2">
      <summary className="cursor-pointer text-text-muted">查看逐词时间 · 源片 {formatSourceTime(timing.words[0].start_us)}–{formatSourceTime(timing.words[timing.words.length - 1].end_us)} 秒</summary>
      <div className="mt-2 max-h-40 space-y-1 overflow-y-auto" aria-label={`${label} 逐词时间`}>
        {timing.words.map((word) => <div key={word.id} className="flex justify-between gap-3 border-b border-[var(--ui-border-soft)] py-1 last:border-b-0">
          <span>{word.text}</span><span className="shrink-0 font-mono text-text-muted">{formatSourceTime(word.start_us)}–{formatSourceTime(word.end_us)} 秒</span>
        </div>)}
      </div>
    </details>}
  </div>;
}
