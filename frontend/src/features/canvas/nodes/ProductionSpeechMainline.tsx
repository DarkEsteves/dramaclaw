import { useMemo, useState, type ReactNode } from 'react';
import type { PlanTurnV2, ProductionPlanV2 } from '@/features/canvas/remix/productionPlanV2Model';
import { projectProductionTimeline } from '@/features/canvas/compose/productionComposeModel';
import { defaultGraphicEffect } from '@/features/canvas/remix/nativeGraphicEffects';
import { formatSrtTime, parseSourceSrt, sourceSrtDialogue, exportProgramSrt, recognizedWordsToPlanCues, type RecognizedTimedWord, type SourceSrtRow } from '@/features/canvas/remix/productionSrt';
import { setPlanTurnText } from '@/features/canvas/remix/productionPlanV2Edits';
import type { RemixArtifactRef } from '@/api/remix';
export interface ProductionSpeechMainlineProps {
 plan: ProductionPlanV2;
 change: (edit: (next: ProductionPlanV2) => void) => void;
 renderCalibration?: (turn: PlanTurnV2, label: string, onRecognizedSrt?: (srt: string, ref: RemixArtifactRef, words: RecognizedTimedWord[], transcript: string) => void) => ReactNode;
 onChoosePlacement?: (id: string) => void;
 onImportSrt?: (takeId: string, originalSrt: string, rows: Array<SourceSrtRow & { turn_id: string; cue_id?: string | null }>, basis?: 'srt_import' | 'manual', recognitionRef?: RemixArtifactRef, correctedWords?: RecognizedTimedWord[]) => Promise<void>;
}
interface RecognizedWordDraft extends RecognizedTimedWord { startSeconds: string; endSeconds: string }
export function ProductionSpeechMainline({ plan, change, renderCalibration, onChoosePlacement, onImportSrt }: ProductionSpeechMainlineProps) {
 const [manualCue, setManualCue] = useState(''); const [manualStart, setManualStart] = useState(''); const [manualEnd, setManualEnd] = useState('');
 const [selectedTake, setSelectedTake] = useState(''); const [srt, setSrt] = useState(''); const [rows, setRows] = useState<SourceSrtRow[]>([]); const [mapping, setMapping] = useState<string[]>([]); const [cueMapping, setCueMapping] = useState<string[]>([]); const [error, setError] = useState(''); const [busy, setBusy] = useState(false);
 const [recognitionRef, setRecognitionRef] = useState<RemixArtifactRef | null>(null);
 const [wordDrafts, setWordDrafts] = useState<RecognizedWordDraft[]>([]);
 const timeline = useMemo(() => projectProductionTimeline(plan), [plan]);
 const takes = plan.speech_takes ?? []; const cues = plan.speech_cues ?? [];
 const take = takes.find((item) => item.id === selectedTake);
 const turns = plan.beats.flatMap((beat) => beat.turns);
 const cueTimes = new Map(timeline.speechCues.map((item) => [item.id, item]));
 const cuesByTurn = new Map<string, typeof cues>();
 const cueNumber = new Map<string, number>();
 cues.forEach((cue, index) => { const rows = cuesByTurn.get(cue.turn_id) ?? []; rows.push(cue); cuesByTurn.set(cue.turn_id, rows); cueNumber.set(cue.id, index + 1); });
 const takeByTurn = new Map(takes.flatMap((item) => item.turn_ids.map((turnId) => [turnId, item] as const)));
 const placementsByAnchor = new Map<string, typeof plan.placements>();
 for (const item of plan.placements) {
  for (const refId of new Set([item.start.ref_id, item.end.ref_id])) if (refId) {
   const rows = placementsByAnchor.get(refId) ?? []; rows.push(item); placementsByAnchor.set(refId, rows);
  }
 }
 const cueTime = (cue: (typeof cues)[number]) => {
  const projected = cueTimes.get(cue.id);
  return projected && projected.startFrame != null && projected.endFrame != null ? `${formatSrtTime(projected.startFrame * plan.output.fps_den * 1e6 / plan.output.fps_num)} → ${formatSrtTime(projected.endFrame * plan.output.fps_den * 1e6 / plan.output.fps_num)}` : '待校准';
 };
 const add = (cueId: string, turnId: string, kind: 'visual' | 'subtitle' | 'graphic', primary = false) => {
  if (kind === 'subtitle') {
   // A migrated caption still follows the whole turn after real cues arrive.
   // Reuse it without changing its authored range, style, or stable ID.
   const matches = (refId: string) => plan.placements.find((item) => item.kind === 'subtitle'
    && [item.start, item.end].some((anchor) => anchor.ref_id === refId));
   const existing = (cueId ? matches(cueId) : undefined) ?? matches(turnId);
   if (existing) { onChoosePlacement?.(existing.id); return; }
  }
  const id = crypto.randomUUID();
  change((next) => { const cue = next.speech_cues?.find((item) => item.id === cueId); const turn = turns.find((item) => item.id === turnId);
   if (primary && cue) {
    // Split display windows; the shared take keeps continuous source sampling and original sound.
    for (const visual of next.placements) {
     if (visual.kind !== 'visual' || visual.speech_take_id) continue;
     const source = next.speech_takes?.find((take) => take.origin_placement_id === visual.id);
     if (!source) continue;
     visual.speech_take_id = source.id; visual.resource_id = source.resource_id;
     visual.start = { kind: 'take', ref_id: source.id, edge: 'start' };
     visual.end = { kind: 'take', ref_id: source.id, edge: 'end' };
     delete visual.source_in_us; delete visual.source_out_us; delete visual.speed_num; delete visual.speed_den;
    }
    const projection = projectProductionTimeline(next);
    const window = projection.speechCues.find((item) => item.id === cue.id);
    if (window?.startFrame != null && window.endFrame != null) {
     for (const old of [...next.placements]) {
      if (old.kind !== 'visual' || old.role !== 'primary') continue;
      const projected = projection.placements.find((item) => item.id === old.id);
      if (projected?.startFrame == null || projected.endFrame == null || projected.startFrame >= window.endFrame || projected.endFrame <= window.startFrame) continue;
      const priorEnd = old.end;
      const sourceAt = (frame: number) => (old.source_in_us ?? 0) + Math.round((frame - projected.startFrame!) * next.output.fps_den * 1e6 / next.output.fps_num * (old.speed_num ?? 1) / (old.speed_den ?? 1));
      const leftOut = sourceAt(window.startFrame); const rightIn = sourceAt(window.endFrame);
      const independentVideo = !old.speech_take_id && next.resources.some((resource) => resource.id === old.resource_id && resource.media_kind === 'video');
      if (projected.startFrame < window.startFrame && projected.endFrame > window.endFrame) { next.placements.push({ ...structuredClone(old), id: crypto.randomUUID(), start: { kind: 'cue', ref_id: cue.id, edge: 'end' }, end: priorEnd, ...(independentVideo ? { source_in_us: rightIn } : {}) }); if (independentVideo) old.source_out_us = leftOut; old.end = { kind: 'cue', ref_id: cue.id, edge: 'start' }; }
      else if (projected.startFrame < window.startFrame) { if (independentVideo) old.source_out_us = leftOut; old.end = { kind: 'cue', ref_id: cue.id, edge: 'start' }; }
      else if (projected.endFrame > window.endFrame) { if (independentVideo) old.source_in_us = rightIn; old.start = { kind: 'cue', ref_id: cue.id, edge: 'end' }; }
      else next.placements = next.placements.filter((item) => item.id !== old.id);
     }
    }
   }
   next.placements.push({ id, kind, role: kind === 'visual' ? primary ? 'primary' : 'overlay' : kind === 'graphic' ? 'effect' : 'dialogue', track_id: kind === 'subtitle' ? 'subtitles' : `${kind}:${kind === 'visual' ? primary ? 'primary' : 'overlay' : 'effect'}`, start: { kind: cue ? 'cue' : 'turn', ref_id: cue?.id ?? turnId, edge: 'start' }, end: { kind: cue ? 'cue' : 'turn', ref_id: cue?.id ?? turnId, edge: 'end' }, ...(kind === 'subtitle' ? { turn_id: turnId, text: cue?.text ?? turn?.text ?? '' } : {}), ...(kind === 'graphic' ? { graphic: defaultGraphicEffect() } : { description: '' }) });
  });
  if (kind === 'visual') onChoosePlacement?.(id);
 };
 const exportSrt = () => {
  const ready = timeline.speechCues ?? [];
  if (!ready.length || ready.some((cue) => cue.startFrame == null || cue.endFrame == null || cue.issue)) { setError('请先校准并复核全部台词时间，再导出节目 SRT'); return; }
  const text = exportProgramSrt(ready.map((cue) => ({ text: cue.text, start_us: cue.startFrame! * plan.output.fps_den * 1e6 / plan.output.fps_num, end_us: cue.endFrame! * plan.output.fps_den * 1e6 / plan.output.fps_num })));
  const url = URL.createObjectURL(new Blob([text], { type: 'application/x-subrip;charset=utf-8' })); const link = document.createElement('a'); link.href = url; link.download = 'production-program.srt'; link.click(); URL.revokeObjectURL(url);
 };
 const correctedWords = () => wordDrafts.map((word) => ({ text: word.text.trim(), start_us: Math.round(Number(word.startSeconds) * 1e6), end_us: Math.round(Number(word.endSeconds) * 1e6) }));
 const updateWordDraft = (index: number, patch: Partial<RecognizedWordDraft>) => {
  setWordDrafts((previous) => previous.map((word, i) => i === index ? { ...word, ...patch } : word));
  setError('');
 };
 const checkAndSaveRecognizedWords = async () => {
  if (!take || !recognitionRef || !onImportSrt || busy) return;
  setBusy(true); setError('');
  try {
   if (wordDrafts.some((word) => !word.startSeconds.trim() || !word.endSeconds.trim())) throw new Error('逐词时间不能为空');
   const resource = plan.resources.find((item) => item.id === take.resource_id);
   if (!resource) throw new Error('发声素材已变化，请重新打开校正');
   const spokenTurns = take.turn_ids.map((id) => turns.find((turn) => turn.id === id));
   if (spokenTurns.some((turn) => !turn)) throw new Error('作品方案台词已变化，请重新打开校正');
   const matched = recognizedWordsToPlanCues(correctedWords(), spokenTurns as PlanTurnV2[], resource.duration_us, take.source_in_us, take.source_out_us ?? resource.duration_us);
   const accepted = matched.rows.map((row) => {
    const existing = cues.filter((cue) => cue.take_id === take.id && cue.turn_id === row.turn_id);
    if (existing.length > 1) throw new Error('这句已有多条编排，请使用素材 SRT 导入逐条调整');
    return { ...row, cue_id: existing[0]?.id ?? null };
   });
   await onImportSrt(take.id, matched.srt, accepted, 'srt_import', recognitionRef, correctedWords());
   setSelectedTake(''); setRecognitionRef(null);
  } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
  finally { setBusy(false); }
 };
 return <section aria-label="台词主线" className="space-y-3 text-sm">
  <div className="flex flex-wrap items-center gap-2"><p className="flex-1">节目时间随发声素材编排；每份视频只校准一次。</p><button className="tap-button" onClick={exportSrt}>导出节目 SRT</button></div>
  {plan.beats.map((beat, beatIndex) => {
   const beatTakes = takes.filter((item) => item.beat_id === beat.id); const calibrated = new Set<string>();
   return <section key={beat.id} className="space-y-2 rounded-xl border border-[var(--ui-border-soft)] p-3"><div className="flex justify-between gap-2"><h3>段落 {beatIndex + 1} · {beat.purpose}</h3><span>{beat.timing_mode === 'fixed' ? '按时间编排' : beat.turns.length ? '随台词编排' : '无台词 · 按画面与时长编排'}</span></div>
    {!beat.turns.length && <p>本段没有台词，无需 SRT 或语音校准。</p>}
    {beat.turns.map((turn, index) => {
     const source = takeByTurn.get(turn.id) ?? beatTakes.find((item) => item.turn_ids.includes(turn.id)); const sourceKey = source?.id ?? turn.speech_placement_id ?? turn.id;
     const first = !calibrated.has(sourceKey); calibrated.add(sourceKey);
     const turnCues = cuesByTurn.get(turn.id) ?? [];
     return <div key={turn.id} data-speech-turn-id={turn.id} className="space-y-2">
      {first && <div className="space-y-2 rounded-lg border border-[var(--ui-border-soft)] p-2"><p>{source ? `发声素材 · ${source.turn_ids.length} 句 · 统一校准` : '绑定发声素材并校准'}</p>{renderCalibration?.(turn, `段落 ${beatIndex + 1} 台词 ${index + 1}`, source ? (draft, ref, words) => { setSelectedTake(source.id); setSrt(draft); setRows([]); setMapping([]); setCueMapping([]); setRecognitionRef(ref); setWordDrafts(words.map((word) => ({ ...word, startSeconds: (word.start_us / 1e6).toFixed(6), endSeconds: (word.end_us / 1e6).toFixed(6) }))); setError(''); } : undefined)}{source && <button className="tap-button" onClick={() => { setSelectedTake(source.id); setSrt(''); setRows([]); setMapping([]); setRecognitionRef(null); setWordDrafts([]); }}>导入素材 SRT</button>}</div>}
      <div className="flex gap-2"><input className="tap-field w-28" aria-label={`段落 ${beatIndex + 1} 说话者 ${index + 1}`} value={turn.speaker} onChange={(event) => change((next) => { const item = next.beats.flatMap((item) => item.turns).find((item) => item.id === turn.id); if (item) item.speaker = event.target.value; })} /><input className="tap-field min-w-0 flex-1" aria-label={`段落 ${beatIndex + 1} 台词 ${index + 1}`} value={turn.text} onChange={(event) => { try { change((next) => { setPlanTurnText(next, turn.id, event.target.value); }); setError(''); } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); } }} /></div>
      {(turnCues.length ? turnCues : [null]).map((cue) => {
       const related = [...new Map([...(cue ? placementsByAnchor.get(cue.id) ?? [] : []), ...(placementsByAnchor.get(turn.id) ?? [])].map((item) => [item.id, item])).values()];
       return <div key={cue?.id ?? turn.id} data-speech-cue-id={cue?.id} className="space-y-2 rounded-lg border border-[var(--ui-border-soft)] p-3" style={{ contentVisibility: 'auto', containIntrinsicSize: '0 220px' }}>
       <div className="flex flex-wrap gap-2"><span>{String(cue ? cueNumber.get(cue.id) ?? 0 : index + 1).padStart(2, '0')}</span><span className="font-mono text-xs">节目时间：{cue ? cueTime(cue) : '待校准'}</span><span>{cue?.timing_source === 'asr' ? '识别校准' : cue?.timing_source === 'manual' ? '人工校准' : cue?.timing_source === 'srt_import' ? 'SRT 导入' : '台词草稿'}{cue && cue.timing_status !== 'ready' ? ` · ${cue.timing_status === 'stale' ? '需重新校准' : '待复核'}` : ''}</span></div>
       {cue?.timing_status === 'draft' && turn.speech_timing && <p>旧词时间保留供参考；请按完整素材重新校准后使用 SRT 主线。</p>}{cue && <p className="font-mono text-xs">源片时间：{cue.source_start_us != null && cue.source_end_us != null ? `${formatSrtTime(cue.source_start_us)} → ${formatSrtTime(cue.source_end_us)}` : '待校准'}</p>}<p className="whitespace-pre-wrap">{cue?.text ?? turn.text}</p>
       <p className="text-xs">{related.map((item) => item.kind === 'subtitle' ? `字幕：${item.text ?? turn.text}` : item.kind === 'visual' ? '叠加画面' : '图形效果').join(' · ') || '尚未安排镜头与字幕'}</p>
       <div className="flex flex-wrap gap-2">{cue && <button className="tap-button" disabled={cue.timing_status !== 'ready'} onClick={() => add(cue.id, turn.id, 'visual', true)}>切换主镜头</button>}{cue && <button className="tap-button" onClick={() => { setManualCue(cue.id); setManualStart(String((cue.source_start_us ?? 0) / 1e6)); setManualEnd(String((cue.source_end_us ?? 0) / 1e6)); }}>修改台词源时间</button>}{(['visual', 'subtitle', 'graphic'] as const).map((kind) => <button key={kind} className="tap-button" onClick={() => add(cue?.id ?? '', turn.id, kind)}>{kind === 'visual' ? '添加叠加镜头' : kind === 'subtitle' ? related.some((item) => item.kind === 'subtitle') ? '编辑字幕' : '添加字幕' : '添加图形效果'}</button>)}{cue?.timing_status === 'ready' && cue.word_ids.length && turn.speech_timing?.text === turn.text && cue.evidence_ref?.artifact_id === turn.speech_timing.evidence_ref.artifact_id ? <details><summary className="tap-button cursor-pointer">展开词时间</summary><div className="space-y-1">{turn.speech_timing.words.filter((word) => cue.word_ids.includes(word.id)).map((word) => <p key={word.id} className="font-mono text-xs">{word.text} · 源片 {formatSrtTime(word.start_us)} → {formatSrtTime(word.end_us)}</p>)}</div></details> : <button className="tap-button" disabled title="需要真实逐词识别证据">展开词时间</button>}</div>
      </div>; })}
     </div>;
    })}
   </section>;
  })}
  {take && <section aria-label={recognitionRef ? '逐词校正' : '导入素材 SRT'} className="space-y-2 rounded-xl border border-[var(--ui-border-soft)] p-3"><p>{recognitionRef ? '按作品方案台词核对逐词文字和源片时间，确认后一次保存。' : '仅导入选中发声素材的源片时间；全片节目 SRT 不能直接分配到素材。'}</p><p>当前素材：{plan.resources.find((item) => item.id === take.resource_id)?.media_path ?? take.id}</p>
   {recognitionRef ? <><section className="space-y-2 rounded-lg border border-[var(--ui-border-soft)] p-2"><div className="flex flex-wrap items-center justify-between gap-2"><h4>逐词校正 · {wordDrafts.length} 个识别词</h4><span className="text-xs text-text-muted">时间为发声素材的源片秒数</span></div><div className="max-h-64 space-y-1 overflow-y-auto pr-1">{wordDrafts.map((word, index) => <div key={index} className="grid grid-cols-[2rem_minmax(0,1fr)_7rem_7rem] items-center gap-2"><span className="text-xs text-text-muted">{index + 1}</span><input aria-label={`识别词 ${index + 1} 文字`} className="tap-field min-w-0" value={word.text} onChange={(event) => updateWordDraft(index, { text: event.target.value })} /><input aria-label={`识别词 ${index + 1} 开始秒`} className="tap-field w-full" type="number" step="0.000001" min="0" value={word.startSeconds} onChange={(event) => updateWordDraft(index, { startSeconds: event.target.value })} /><input aria-label={`识别词 ${index + 1} 结束秒`} className="tap-field w-full" type="number" step="0.000001" min="0" value={word.endSeconds} onChange={(event) => updateWordDraft(index, { endSeconds: event.target.value })} /></div>)}</div></section><button className="tap-button" disabled={!onImportSrt || busy} onClick={() => void checkAndSaveRecognizedWords()}>{busy ? '核对保存中…' : '核对并保存台词时间'}</button></> : <>
   <textarea aria-label="素材 SRT 内容" className="tap-field w-full" value={srt} onChange={(event) => { setSrt(event.target.value); setRows([]); }} /><button className="tap-button" onClick={() => { try { const parsed = parseSourceSrt(srt); const duration = plan.resources.find((item) => item.id === take.resource_id)?.duration_us; if (!duration || parsed.some((row) => row.source_end_us > duration || row.source_start_us < take.source_in_us || row.source_end_us > (take.source_out_us ?? duration))) throw new Error('SRT 时间超出选中素材播放范围'); setRows(parsed); setCueMapping(parsed.map((row) => { const matches = cues.filter((cue) => cue.take_id === take.id && cue.text === row.text); return matches.length === 1 && parsed.filter((item) => item.text === row.text).length === 1 ? matches[0].id : '__new__'; })); setMapping(parsed.map((row) => { const matches = turns.filter((turn) => take.turn_ids.includes(turn.id) && (turn.text === row.text || row.text === `${turn.speaker}: ${turn.text}` || row.text === `${turn.speaker}：${turn.text}`)); return matches.length === 1 ? matches[0].id : ''; })); setError(''); } catch (cause) { setError(String(cause)); } }}>预览并匹配台词</button>
   {rows.map((row, index) => <label key={index} className="block">{formatSrtTime(row.source_start_us)} · {row.text}<select aria-label={`SRT ${index + 1} 对应台词`} className="tap-field w-full" value={mapping[index] ?? ''} onChange={(event) => { setMapping((previous) => previous.map((id, i) => i === index ? event.target.value : id)); setCueMapping((previous) => previous.map((id, i) => i === index ? '__new__' : id)); }}><option value="">请选择对应台词</option>{turns.filter((turn) => take.turn_ids.includes(turn.id)).map((turn) => <option key={turn.id} value={turn.id}>{turn.speaker} · {turn.text}</option>)}</select><select aria-label={`SRT ${index + 1} 对应主线行`} className="tap-field w-full" value={cueMapping[index] ?? '__new__'} onChange={(event) => setCueMapping((previous) => previous.map((id, i) => i === index ? event.target.value : id))}><option value="__new__">创建新的台词行</option>{cues.filter((cue) => cue.take_id === take.id && cue.turn_id === mapping[index]).map((cue) => <option key={cue.id} value={cue.id}>{cue.text} · {cue.source_start_us == null ? '草稿' : formatSrtTime(cue.source_start_us)}</option>)}</select></label>)}
   <button className="tap-button" disabled={!onImportSrt || busy || !rows.length || mapping.some((id) => !id)} onClick={async () => { setBusy(true); try {
    const accepted = rows.map((row, index) => { const turn = turns.find((turn) => turn.id === mapping[index])!; return { ...row, text: sourceSrtDialogue(row.text, turn.speaker, turn.text), turn_id: turn.id, cue_id: cueMapping[index] === '__new__' ? null : cueMapping[index] }; });
    await onImportSrt?.(take.id, srt, accepted, 'srt_import'); setSelectedTake(''); setRecognitionRef(null);
   } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); } finally { setBusy(false); } }}>确认导入并保存证据</button></>}
   <button className="tap-button" onClick={() => { setSelectedTake(''); setRecognitionRef(null); }}>取消</button>
  </section>}
  {manualCue && <section aria-label="修改台词源时间" className="space-y-2 rounded-xl border border-[var(--ui-border-soft)] p-3"><p>人工确认源片秒数；关联镜头、字幕与图形将随本句移动。逐词锚点需重新识别。</p><input aria-label="台词源开始秒" className="tap-field" type="number" step="0.001" value={manualStart} onChange={(event) => setManualStart(event.target.value)} /><input aria-label="台词源结束秒" className="tap-field" type="number" step="0.001" value={manualEnd} onChange={(event) => setManualEnd(event.target.value)} /><button className="tap-button" disabled={busy || !onImportSrt} onClick={async () => { const cue = cues.find((item) => item.id === manualCue); const source = takes.find((item) => item.id === cue?.take_id); const duration = plan.resources.find((item) => item.id === source?.resource_id)?.duration_us; const start = Math.round(Number(manualStart) * 1e6); const end = Math.round(Number(manualEnd) * 1e6); if (!cue || !source || !duration || !Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < source.source_in_us || end <= start || end > (source.source_out_us ?? duration)) { setError('源片时间超出素材播放范围或无效'); return; } setBusy(true); try { await onImportSrt?.(source.id, '', [{ text: cue.text, turn_id: cue.turn_id, cue_id: cue.id, source_start_us: start, source_end_us: end }], 'manual'); setManualCue(''); } catch (cause) { setError(String(cause)); } finally { setBusy(false); } }}>确认人工时间并保存证据</button><button className="tap-button" onClick={() => setManualCue('')}>取消修改</button></section>}
  {error && <p role="alert">{error}</p>}
 </section>;
}
