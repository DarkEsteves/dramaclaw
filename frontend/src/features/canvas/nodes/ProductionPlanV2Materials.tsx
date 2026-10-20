import type { PlanMediaCandidate } from '@/features/canvas/remix/productionPlanReadiness';
import { useState } from 'react';
import type { PlanPlacementV2, ProductionPlanV2 } from '@/features/canvas/remix/productionPlanV2Model';
import { movePlanBeat, removePlanBeat, removePlanPlacement, removePlanTurn, setPlanPlacementRole, setPlanTurnText } from '@/features/canvas/remix/productionPlanV2Edits';
import { projectProductionTimeline } from '@/features/canvas/compose/productionComposeModel';
import { defaultGraphicEffect, GRAPHIC_TEMPLATES, type GraphicTemplate } from '@/features/canvas/remix/nativeGraphicEffects';
import { plannedSpeechSourcePlacementsForTurn, sharedSpeechTakeSourceBlocker } from '@/features/canvas/remix/productionSpeechReadiness';

const field = 'tap-field min-w-0 text-sm';
const button = 'tap-button shrink-0';

interface Props {
  plan: ProductionPlanV2;
  focusPlacementId: string | null;
  candidates: PlanMediaCandidate[];
  pending: Record<string, string>;
  pendingErrors: Record<string, string>;
  change: (edit: (next: ProductionPlanV2) => void) => void;
  choose: (placementId: string) => void;
  onCalibrate: (turnId: string) => void;
  onSourceChange: (turnId: string, placementId: string | null) => void;
  onVideoSoundChange: (placementId: string, enabled: boolean) => void;
}

export function ProductionPlanV2Materials({ plan, focusPlacementId, candidates, pending, pendingErrors, change, choose, onCalibrate, onSourceChange, onVideoSoundChange }: Props) {
  const [editError, setEditError] = useState<string | null>(null);
  const timeline = projectProductionTimeline(plan);
  const indexed = timeline.placements.map((placement, index) => ({ placement, index }));
  const used = new Set<number>();
  const groups = timeline.beats.map((beat) => indexed.filter(({ placement, index }) => {
    if (used.has(index)) return false;
    const turnIds = new Set(plan.beats.find((row) => row.id === beat.id)?.turns.map((turn) => turn.id) ?? []);
    const inBeat = placement.start?.kind === 'text' ? turnIds.has(placement.start.ref_id ?? '')
      : placement.startFrame != null ? placement.startFrame >= beat.startFrame && placement.startFrame < beat.endFrame
        : placement.start?.kind === 'beat' && placement.start.ref_id === beat.id;
    if (inBeat) used.add(index);
    return inBeat;
  }));
  const unplaced = indexed.filter(({ index }) => !used.has(index));
  const fps = plan.output.fps_num / plan.output.fps_den;

  const addPlacement = (beatIndex: number, kind: 'visual' | 'audio' | 'subtitle' | 'graphic') => change((next) => {
    const beat = next.beats[beatIndex];
    const current = groups[beatIndex] ?? [];
    const role = kind === 'visual' ? current.some(({ placement }) => placement.kind === 'visual' && placement.role === 'primary') ? 'overlay' : 'primary'
      : kind === 'audio' ? beat.turns.length ? 'speech' : 'narration' : kind === 'graphic' ? 'effect' : 'dialogue';
    next.placements.push({ id: crypto.randomUUID(), kind, role, track_id: kind === 'subtitle' ? 'subtitles' : `${kind}:${role}`,
      description: '', ...(kind === 'subtitle' ? { text: '字幕文字' } : {}),
      ...(kind === 'graphic' ? { graphic: { ...defaultGraphicEffect(),
        enter_frames: Math.min(6, Math.floor(beat.duration_frames / 2)),
        exit_frames: Math.min(6, Math.floor(beat.duration_frames / 2)) } } : {}),
      start: { kind: 'beat', ref_id: beat.id, edge: 'start' },
      end: { kind: 'beat', ref_id: beat.id, edge: 'end' } });
  });

  const renderPlacement = ({ placement, index }: (typeof indexed)[number]) => {
    const resource = plan.resources.find((item) => item.id === placement.resource_id);
    const candidate = candidates.find((item) => item.node.id === resource?.node_id);
    const current = Boolean(resource && candidate?.media?.media_hash === resource.media_hash && candidate.media.media_path === resource.media_path);
    const pendingNode = candidates.find((item) => item.node.id === pending[placement.id]);
    const visualNumber = plan.placements.slice(0, index + 1).filter((item) => item.kind === 'visual').length;
    const audioNumber = plan.placements.slice(0, index + 1).filter((item) => item.kind === 'audio').length;
    return <div key={placement.id} data-plan-placement-id={placement.id}
      className={`min-w-0 space-y-2 rounded-xl border bg-white/[0.025] p-3 text-xs ${focusPlacementId === placement.id ? 'border-primary' : 'border-[var(--ui-border-soft)]'}`}>
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        {placement.kind === 'visual' || placement.kind === 'audio' ? <select aria-label={`条目 ${index + 1} 类型`} className={`${field} w-28 shrink-0`} value={placement.role}
          onChange={(event) => change((next) => { setPlanPlacementRole(next, placement.id, event.target.value as PlanPlacementV2['role']); })}>{placement.kind === 'visual' ? <><option value="primary">主画面</option><option value="overlay">叠加画面</option></>
            : <><option value="speech">同期人声</option><option value="narration">旁白</option><option value="bgm">配乐</option><option value="sfx">音效</option></>}</select>
          : <span className="w-20 shrink-0 text-text-muted">{placement.kind === 'graphic' ? '图形效果' : '字幕'}</span>}
        <input aria-label={`条目 ${index + 1} 内容`} className={`${field} flex-1`} value={placement.kind === 'graphic' ? placement.graphic?.text ?? '' : placement.kind === 'subtitle' ? placement.text ?? '' : placement.description ?? ''}
          onChange={(event) => change((next) => {
            const row = next.placements[index];
            if (row.kind === 'graphic' && row.graphic) {
              row.graphic.text = event.target.value;
              if (row.graphic.emphasis && !row.graphic.text.includes(row.graphic.emphasis)) row.graphic.emphasis = null;
            } else if (row.kind === 'subtitle') row.text = event.target.value;
            else row.description = event.target.value;
          })} placeholder={placement.kind === 'subtitle' ? '字幕文字' : '画面或声音内容'} />
        <button type="button" className={button} aria-label={`移除条目 ${index + 1}`} onClick={() => change((next) => { removePlanPlacement(next, placement.id); })}>移除</button>
      </div>
      {(placement.kind === 'visual' || placement.kind === 'audio') && <>
        <button type="button" className={`${field} flex w-full items-center justify-between gap-2 text-left`}
          aria-label={`选择${placement.kind === 'visual' ? `画面 ${visualNumber}` : `声音 ${audioNumber}`} 素材`}
          onClick={() => choose(placement.id)}>
          <span className="truncate">{pendingNode ? `${pendingNode.label} · 制作中` : candidate?.label ?? (resource ? '已绑定素材，画布节点不可用' : '选择画布素材')}</span>
          <span className="shrink-0 text-primary">{resource ? '更换' : '选择'}</span>
        </button>
        {resource && <p className={current ? 'text-emerald-300' : 'text-amber-300'}>{current ? '素材版本已绑定' : '素材版本已变化或未连接，请重新绑定'}</p>}
        {pendingErrors[placement.id] && <p role="alert" className="text-red-300">{pendingErrors[placement.id]}</p>}
      </>}
      {placement.kind === 'visual' && (!resource || resource.media_kind === 'video')
        && !plan.speech_takes?.some((take) => take.origin_placement_id === placement.id)
        && !plan.beats.some((beat) => beat.turns.some((turn) => turn.speech_placement_id === placement.id))
        && <label className="flex items-center gap-2 text-xs text-text-muted"><input type="checkbox" aria-label={`画面 ${visualNumber} 使用视频声音`}
          checked={placement.source_audio === true} onChange={(event) => onVideoSoundChange(placement.id, event.target.checked)} />使用视频声音
          <span>用于同期声音或视频自带音效</span></label>}
      {placement.kind === 'graphic' && placement.graphic && <div className="grid grid-cols-2 gap-2">
        <select aria-label={`条目 ${index + 1} 图形模板`} className={field} value={placement.graphic.template}
          onChange={(event) => change((next) => { const row = next.placements[index]; if (row.graphic) row.graphic.template = event.target.value as GraphicTemplate; })}>
          {GRAPHIC_TEMPLATES.map((template) => <option key={template.id} value={template.id}>{template.label}</option>)}
        </select>
        <input aria-label={`条目 ${index + 1} 重点词`} className={field} value={placement.graphic.emphasis ?? ''}
          onChange={(event) => change((next) => { const row = next.placements[index]; if (row.graphic) row.graphic.emphasis = event.target.value || null; })} placeholder="重点词，可留空" />
      </div>}
    </div>;
  };

  return <section aria-label="方案与素材" className="space-y-4">
    {editError && <p role="alert" className="text-xs text-destructive">{editError}</p>}
    <div className="flex items-baseline justify-between gap-3"><h3 className="text-sm font-semibold">叙事段落</h3><span className="text-xs text-text-muted">{plan.beats.length} 段 · {plan.placements.length} 项画面、声音与字幕</span></div>
    {plan.beats.map((beat, index) => <section key={beat.id} aria-label={`段落 ${index + 1}`} className="space-y-4 rounded-xl border border-[var(--ui-border-soft)] bg-white/[0.035] p-4">
      <div className="flex min-w-0 flex-wrap items-center gap-2"><span className="rounded-full bg-primary/15 px-2 py-1 text-xs text-primary">{String(index + 1).padStart(2, '0')}</span>
        <input aria-label={`段落 ${index + 1} 叙事作用`} className={`${field} min-w-48 flex-1`} value={beat.purpose}
          onChange={(event) => change((next) => { next.beats[index].purpose = event.target.value; })} placeholder="这段要表达什么" />
        <label className="flex shrink-0 items-center gap-2 text-xs text-text-muted">秒
          <input aria-label={`段落 ${index + 1} 时长`} className={`${field} w-20`} type="number" min="0.1" step="0.1" value={Number((beat.duration_frames / fps).toFixed(3))}
            onChange={(event) => change((next) => { next.beats[index].duration_frames = Math.max(1, Math.round(Number(event.target.value) * fps)); })} />
        </label>
        <select aria-label={`段落 ${index + 1} 时间方式`} className={`${field} w-24 text-xs`} value={beat.timing_mode ?? 'auto'}
          onChange={(event) => change((next) => { next.beats[index].timing_mode = event.target.value as 'auto' | 'fixed'; if (event.target.value === 'fixed') next.beats[index].duration_source_placement_id = null; })}>
          <option value="auto">{beat.turns.length ? '随台词' : beat.duration_source_placement_id ? '随素材' : '预计时长'}</option><option value="fixed">固定时长</option>
        </select>
        <button type="button" className={button} aria-label={`段落 ${index + 1} 上移`} disabled={index === 0} onClick={() => change((next) => { movePlanBeat(next, index, -1); })}>上移</button>
        <button type="button" className={button} aria-label={`段落 ${index + 1} 下移`} disabled={index === plan.beats.length - 1} onClick={() => change((next) => { movePlanBeat(next, index, 1); })}>下移</button>
        <button type="button" className={button} aria-label={`段落 ${index + 1} 删除`} disabled={plan.beats.length === 1} onClick={() => change((next) => { removePlanBeat(next, beat.id); })}>删除段落</button>
      </div>
      {!beat.turns.length && (beat.timing_mode ?? 'auto') === 'auto' && <label className="flex items-center gap-2 text-xs text-text-muted">时长依据
        <select aria-label={`段落 ${index + 1} 时长依据`} className={`${field} max-w-64`} value={beat.duration_source_placement_id ?? ''}
          onChange={(event) => change((next) => { next.beats[index].duration_source_placement_id = event.target.value || null; })}>
          <option value="">使用预计时长</option>
          {(groups[index] ?? []).filter(({ placement }) => !placement.loop && (placement.kind === 'visual' || placement.kind === 'audio')
            && placement.start.kind === 'beat' && placement.start.ref_id === beat.id && placement.start.edge === 'start'
            && placement.end.kind === 'beat' && placement.end.ref_id === beat.id && placement.end.edge === 'end'
            && !placement.start.offset_frames && !placement.end.offset_frames
            && (placement.kind === 'audio' || !placement.resource_id || plan.resources.find((resource) => resource.id === placement.resource_id)?.media_kind === 'video'))
            .map(({ placement }) => <option key={placement.id} value={placement.id}>{placement.description || (placement.kind === 'audio' ? '声音素材' : '视频素材')}</option>)}
        </select>
      </label>}
      {timeline.beats[index] && timeline.beats[index].endFrame - timeline.beats[index].startFrame !== beat.duration_frames &&
        <p className="text-xs text-primary">发声素材决定实际时长：{((timeline.beats[index].endFrame - timeline.beats[index].startFrame) / fps).toFixed(1)} 秒 · 当前预计 {beat.duration_frames / fps} 秒</p>}
      <div className="grid min-w-0 gap-4 lg:grid-cols-2">
        <div className="min-w-0 space-y-2"><div className="flex items-center justify-between gap-2"><span className="text-xs text-text-muted">台词 · 可留空</span><button type="button" className={button} onClick={() => change((next) => { next.beats[index].duration_source_placement_id = null; next.beats[index].turns.push({ id: crypto.randomUUID(), speaker: '', text: '新台词', timing_basis: 'estimated', words: [] }); })}>添加说话者</button></div>
          {beat.turns.map((turn, turnIndex) => <div key={turn.id} className="space-y-2 rounded-lg border border-[var(--ui-border-soft)] p-2">
            <div className="flex min-w-0 items-center gap-2"><input aria-label={`段落 ${index + 1} 说话者 ${turnIndex + 1}`} className={`${field} w-28 shrink-0`} value={turn.speaker} onChange={(event) => change((next) => { next.beats[index].turns[turnIndex].speaker = event.target.value; })} placeholder="说话者" />
              <input aria-label={`段落 ${index + 1} 台词 ${turnIndex + 1}`} className={`${field} flex-1`} value={turn.text} onChange={(event) => { try { change((next) => { setPlanTurnText(next, turn.id, event.target.value); }); setEditError(null); } catch (cause) { setEditError(cause instanceof Error ? cause.message : String(cause)); } }} placeholder="这句说什么" />
              <button type="button" className={button} aria-label={`移除段落 ${index + 1} 台词 ${turnIndex + 1}`} onClick={() => change((next) => { removePlanTurn(next, turn.id); })}>移除</button></div>
            {(() => {
              const selected = turn.speech_placement_id ?? turn.speech_timing?.placement_id ?? '';
              const source = plan.placements.find((row) => row.id === selected);
              const owner = source ? `${source.kind === 'visual' ? '画面' : source.role === 'narration' ? '旁白' : '声音'} ${plan.placements.filter((row) => row.kind === source.kind).findIndex((row) => row.id === source.id) + 1}` : '';
              const peers = beat.turns.filter((row) => row.speech_placement_id === selected && Boolean(selected));
              const leader = !peers.length || peers[0].id === turn.id;
              const sharedTake = plan.speech_takes?.find((take) => take.turn_ids.length > 1 && (peers.length ? peers : [turn]).some((row) => take.turn_ids.includes(row.id)));
              const blocker = sharedTake ? sharedSpeechTakeSourceBlocker(plan, sharedTake.id) : null;
              return <div className="flex flex-wrap items-center gap-2 text-xs">
                <span className="text-text-muted">{source ? `由${owner} 发声` : turn.speech_placement_id === null ? '无发声' : '尚未选择发声来源'}</span>
                {leader ? <select aria-label={`段落 ${index + 1} 台词 ${turnIndex + 1} 声音来源`} className={`${field} min-w-36 max-w-full flex-1`} value={turn.speech_placement_id === null ? '__none__' : selected}
                  disabled={Boolean(blocker)}
                  onChange={(event) => onSourceChange(turn.id, event.target.value === '__none__' ? null : event.target.value)}>
                  <option value="" disabled>尚未选择</option>
                  <option value="__none__">无发声</option>
                  {selected && !plannedSpeechSourcePlacementsForTurn(plan, turn.id).some((row) => row.id === selected) && <option value={selected}>原来源不可用</option>}
                  {plannedSpeechSourcePlacementsForTurn(plan, turn.id).map((row) => {
                    const number = plan.placements.filter((item) => item.kind === row.kind).findIndex((item) => item.id === row.id) + 1;
                    return <option key={row.id} value={row.id}>{row.kind === 'visual' ? `画面 ${number}` : `${row.role === 'narration' ? '旁白' : '声音'} ${number}`} · {row.description || (row.resource_id ? '已绑定' : '待准备素材')}</option>;
                  })}
                </select> : <span className="text-text-muted">与同源台词共用来源</span>}
                {leader && blocker && <span className="basis-full text-amber-300">{blocker}</span>}
                {leader && <button type="button" className="text-primary hover:underline" onClick={() => onCalibrate(turn.id)}>{peers.length > 1 ? `校准同源 ${peers.length} 句台词` : '校准台词时间'}</button>}
              </div>;
            })()}
            {turn.speech_timing && <p className="text-xs text-text-muted">改台词后需重新校准；已使用的词锚点需要先调整。</p>}
          </div>)}
          {!beat.turns.length && <p className="rounded-lg border border-dashed border-[var(--ui-border-soft)] p-3 text-xs text-text-muted">无台词段落也可以直接制作画面</p>}
        </div>
        <div className="min-w-0 space-y-2"><div className="flex flex-wrap items-center justify-between gap-2"><span className="text-xs text-text-muted">画面</span><div className="flex flex-wrap gap-1"><button type="button" className={button} onClick={() => addPlacement(index, 'visual')}>添加画面</button><button type="button" className={button} onClick={() => addPlacement(index, 'subtitle')}>添加字幕</button><button type="button" className={button} onClick={() => addPlacement(index, 'graphic')}>添加图形效果</button></div></div>
          {groups[index].filter(({ placement }) => placement.kind !== 'audio').map(renderPlacement)}
          {!groups[index].some(({ placement }) => placement.kind !== 'audio') && <p className="rounded-lg border border-dashed border-[var(--ui-border-soft)] p-3 text-xs text-text-muted">这一段还没有画面</p>}
        </div>
      </div>
      <div className="space-y-2"><div className="flex items-center justify-between"><span className="text-xs text-text-muted">独立声音 · 用于额外旁白、配乐或音效</span><button type="button" className={button} onClick={() => addPlacement(index, 'audio')}>添加声音</button></div>
        {groups[index].filter(({ placement }) => placement.kind === 'audio').map(renderPlacement)}
        {!groups[index].some(({ placement }) => placement.kind === 'audio') && <p className="rounded-lg border border-dashed border-[var(--ui-border-soft)] p-3 text-xs text-text-muted">这一段还没有声音素材</p>}
      </div>
    </section>)}
    {unplaced.length > 0 && <section className="space-y-2 rounded-xl border border-[var(--ui-border-soft)] p-4"><h4 className="text-xs text-text-muted">未定位到段落的条目</h4>{unplaced.map(renderPlacement)}</section>}
  </section>;
}
