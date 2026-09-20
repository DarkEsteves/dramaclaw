import { useState } from 'react';

export interface ReferenceEvent {
  start_us: number;
  end_us: number;
  frame_url?: string | null;
  frame_pts?: number[];
  frame_times_us?: number[];
  analysis_frame_time_us?: number | null;
  spoken_text?: string;
  word_spans?: Array<{ text: string; start_us: number; end_us: number }>;
  visual_observation?: string;
  narrative_inference?: string;
  note_status?: string;
}

function seconds(timeUs: number): string {
  return (timeUs / 1_000_000).toFixed(1).padStart(4, '0');
}

export function ReferenceEventList({ events, videoUrl }: { events: ReferenceEvent[]; videoUrl?: string | null }) {
  const [selected, setSelected] = useState<number | null>(null);

  return <div className="ui-scrollbar h-full overflow-auto text-xs text-text-dark">
    <div className="sticky top-0 z-10 border-b border-white/[0.08] bg-surface-panel px-3 py-2 text-text-muted">按原片时间排列 · {events.length} 个事件 · 模型判断待核对<div className="mt-1">台词有错词时，请到相连的转写节点校对并保存，再重新分析原片。</div></div>
    {events.map((event, index) => <div key={`${event.start_us}-${event.end_us}-${index}`} className="border-b border-white/[0.06]">
      <button type="button" className="nodrag flex w-full items-start gap-3 px-3 py-2 text-left hover:bg-white/[0.04]" onClick={() => setSelected((current) => current === index ? null : index)}>
        <span className="w-20 shrink-0 font-mono text-[11px] text-text-muted">{seconds(event.start_us)}–{seconds(event.end_us)} 秒</span>
        {event.frame_url && <img src={event.frame_url} alt={`事件画面 ${index + 1}`} className="h-14 w-10 shrink-0 rounded-[4px] bg-surface-field object-cover" />}
        <span className="grid min-w-0 flex-1 gap-1">
          <span><span className="mr-2 text-text-muted">画面</span>{event.note_status === 'missing_frame' ? '画面未核实' : event.visual_observation || '待核对画面'}</span>
          <span><span className="mr-2 text-text-muted">同期台词</span>{event.spoken_text || '无同期台词'}</span>
          <span><span className="mr-2 text-text-muted">叙事作用</span>{event.narrative_inference || '待补充'}</span>
        </span>
        <span className="shrink-0 text-[11px] text-amber-300">{event.note_status === 'missing_frame' ? '缺少画面' : '待核对'}</span>
      </button>
      {selected === index && <div className="border-t border-white/[0.06] bg-surface-field/40 px-3 py-2">
        {videoUrl && <video key={index} aria-label="原片事件预览" src={videoUrl} controls className="mb-2 max-h-32 w-full" ref={(element) => { if (element) element.currentTime = event.start_us / 1_000_000; }} onTimeUpdate={(playback) => { if (playback.currentTarget.currentTime >= event.end_us / 1_000_000) playback.currentTarget.pause(); }} />}
        <details className="nodrag text-[11px] text-text-muted"><summary className="cursor-pointer">查看时间与证据</summary>
          <p className="mt-1">分析画面时间：{event.analysis_frame_time_us == null ? '暂无' : `${seconds(event.analysis_frame_time_us)} 秒`}</p>
          <p>原片代表帧时间：{(event.frame_times_us ?? []).map(seconds).join('、') || '暂无代表帧'} 秒</p>
          <p>原始帧 PTS：{(event.frame_pts ?? []).join('、') || '暂无'}</p>
          <p>词语时间：{(event.word_spans ?? []).map((word) => `${word.text} ${seconds(word.start_us)}–${seconds(word.end_us)} 秒`).join('；') || '无人声'}</p>
        </details>
      </div>}
    </div>)}
  </div>;
}
