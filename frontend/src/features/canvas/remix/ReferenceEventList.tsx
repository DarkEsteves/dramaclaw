import { useState } from 'react';

export interface ReferenceEvent {
  event_id?: string;
  start_us: number;
  end_us: number;
  frame_url?: string | null;
  frame_pts?: number[];
  frame_times_us?: number[];
  analysis_frame_time_us?: number | null;
  spoken_text?: string;
  visible_text?: string;
  transcript_warnings?: Array<{ transcript_word: string; caption_word: string }>;
  word_spans?: Array<{ text: string; start_us: number; end_us: number }>;
  visual_observation?: string;
  user_visual_observation?: string;
  user_visible_text?: string;
  narrative_inference?: string;
  note_status?: string;
  observed_frame?: { pts?: number | null; time_base?: string | null; time_us: number; url?: string | null } | null;
  spoken_range?: { text: string; start_us: number; end_us: number; start_char?: number | null; end_char?: number | null } | null;
  user_narrative_role?: string;
  review_state?: 'needs_review' | 'reviewed' | 'reviewed_uncertain';
}

export interface ReferenceEventPatch {
  visualObservation?: string;
  visibleText?: string;
  narrativeRole?: string;
  reviewState?: ReferenceEvent['review_state'];
}

function seconds(timeUs: number): string {
  return (timeUs / 1_000_000).toFixed(1).padStart(4, '0');
}

interface Props {
  events: ReferenceEvent[];
  videoUrl?: string | null;
  onOverrideChange?: (eventId: string, patch: ReferenceEventPatch) => void;
}

export function ReferenceEventList({ events, videoUrl, onOverrideChange }: Props) {
  const [selected, setSelected] = useState<number | null>(null);

  return <div className="ui-scrollbar nowheel h-full overflow-auto text-xs text-text-dark">
    <div className="border-b border-white/[0.08] px-3 py-2 text-text-muted">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span>按原片时间排列 · {events.length} 个事件</span>
      </div>
      <div className="mt-1">展开事件可修正画面描述和字幕，也可修改叙事作用；台词错词请在转写节点保存校字后重新分析原片。</div>
    </div>
    {events.map((event, index) => {
      const eventId = event.event_id ?? `event-${index + 1}`;
      const observedFrame = event.observed_frame;
      const frameUrl = event.frame_url ?? observedFrame?.url;
      const spokenRange = event.spoken_range;
      const spokenText = event.spoken_text ?? spokenRange?.text;
      const visualText = event.user_visual_observation ?? event.visual_observation;
      const captionText = event.user_visible_text ?? event.visible_text;
      const narrativeText = event.user_narrative_role ?? event.narrative_inference;
      return <div key={eventId} className="border-b border-white/[0.06]">
      <button type="button" className="nodrag flex w-full items-start gap-3 px-3 py-2 text-left hover:bg-white/[0.04]" onClick={() => setSelected((current) => current === index ? null : index)}>
        <span className="w-20 shrink-0 font-mono text-[11px] text-text-muted">{seconds(event.start_us)}–{seconds(event.end_us)} 秒</span>
        {frameUrl && <img src={frameUrl} alt={`事件画面 ${index + 1}`} className="h-14 w-10 shrink-0 rounded-[4px] bg-surface-field object-cover" />}
        <span className="grid min-w-0 flex-1 gap-1">
          <span className={selected === index ? '' : 'line-clamp-2'}><span className="mr-2 text-text-muted">画面{event.user_visual_observation !== undefined ? ' · 已修正' : ''}</span>{event.note_status === 'missing_frame' ? '画面未核实' : visualText || '暂无画面描述'}</span>
          <span className={selected === index ? '' : 'line-clamp-1'}><span className="mr-2 text-text-muted">同期台词</span>{spokenText || '无同期台词'}</span>
          {event.user_visible_text !== undefined && <span className={selected === index ? '' : 'line-clamp-1'}><span className="mr-2 text-text-muted">画面字幕 · 已修正</span>{captionText || '无可见字幕'}</span>}
          {event.user_visible_text === undefined && (event.transcript_warnings ?? []).map((warning, warningIndex) => <span key={`${warning.transcript_word}-${warning.caption_word}-${warningIndex}`} className="text-amber-300">疑似转写错词：{warning.transcript_word} → 画面字幕 {warning.caption_word}</span>)}
          <span className={selected === index ? '' : 'line-clamp-1'}><span className="mr-2 text-text-muted">叙事作用{event.user_narrative_role !== undefined ? ' · 已修正' : ''}</span>{narrativeText || '待补充'}</span>
          {observedFrame && <span className="font-mono text-[11px] text-text-muted">画面 {seconds(observedFrame.time_us)} 秒 · PTS {observedFrame.pts ?? '未知'} · {observedFrame.time_base ?? '未知 time_base'}</span>}
        </span>
        <span className="grid shrink-0 justify-items-end gap-1 text-[11px]">{event.note_status === 'missing_frame' ? <span className="text-warning">缺少画面</span> : null}<span className="text-text-muted">{selected === index ? '收起编辑' : '展开编辑'}</span></span>
      </button>
      {onOverrideChange && <div className="nodrag grid gap-2 px-3 pb-2">
        <label className="text-[11px] text-text-muted">叙事作用 · 你的判断
          <input aria-label={`事件 ${index + 1} 叙事作用`} value={event.user_narrative_role ?? event.narrative_inference ?? ''} onChange={(change) => onOverrideChange(eventId, { narrativeRole: change.target.value })} className="mt-1 w-full rounded-[12px] border border-border-dark bg-surface-field px-2 py-1 text-text-dark" />
        </label>
      </div>}
      {selected === index && <div className="border-t border-white/[0.06] bg-surface-field/40 px-3 py-2">
        {videoUrl && <video key={index} aria-label="原片事件预览" src={videoUrl} controls className="mb-2 max-h-32 w-full" ref={(element) => { if (element) element.currentTime = event.start_us / 1_000_000; }} onTimeUpdate={(playback) => { if (playback.currentTarget.currentTime >= event.end_us / 1_000_000) playback.currentTarget.pause(); }} />}
        {onOverrideChange && <div className="nodrag mb-2 grid gap-2">
          <label className="text-[11px] text-text-muted">画面描述 · 可修正
            <textarea aria-label={`事件 ${index + 1} 画面描述`} value={visualText ?? ''} onChange={(change) => onOverrideChange(eventId, { visualObservation: change.target.value })} rows={2} className="ui-field mt-1 w-full resize-y px-2 py-1.5 text-xs text-text-dark" />
          </label>
          <label className="text-[11px] text-text-muted">画面字幕 · 只写实际可见的字
            <input aria-label={`事件 ${index + 1} 画面字幕`} value={captionText ?? ''} onChange={(change) => onOverrideChange(eventId, { visibleText: change.target.value })} className="ui-field mt-1 w-full px-2 py-1.5 text-xs text-text-dark" />
          </label>
          <p className="text-[11px] text-text-muted">修改会保存在原片分析节点；同期台词请到转写节点校字。</p>
        </div>}
        <details className="nodrag text-[11px] text-text-muted"><summary className="cursor-pointer">查看时间与证据</summary>
          <p className="mt-1">分析画面时间：{observedFrame ? `${seconds(observedFrame.time_us)} 秒` : event.analysis_frame_time_us == null ? '暂无' : `${seconds(event.analysis_frame_time_us)} 秒`}</p>
          <p>原片代表帧时间：{(event.frame_times_us ?? []).map(seconds).join('、') || '暂无代表帧'} 秒</p>
          <p>原始帧 PTS：{(event.frame_pts ?? []).join('、') || '暂无'}</p>
          <p>模型画面描述：{event.visual_observation || '未识别'}</p>
          <p>模型画面字幕：{event.visible_text || '未识别'}</p>
          <p>模型叙事判断：{event.narrative_inference || '未识别'}</p>
          <p>词语时间：{spokenRange ? `${spokenRange.text} ${seconds(spokenRange.start_us)}–${seconds(spokenRange.end_us)} 秒` : (event.word_spans ?? []).map((word) => `${word.text} ${seconds(word.start_us)}–${seconds(word.end_us)} 秒`).join('；') || '无人声'}</p>
        </details>
      </div>}
    </div>})}
  </div>;
}
