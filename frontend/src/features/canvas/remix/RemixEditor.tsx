import type { NamedSelection } from './model';
import { applyManifestEdit, projectManifestTimeline, type ManifestPayload } from './manifestEditor';

export function RemixEditor({ selections, events, manifest, onManifestChange, disabled = false }: { selections: NamedSelection[]; events: Array<{ selectionId: string; startFrame?: number; endFrame?: number }>; manifest?: ManifestPayload | null; onManifestChange?: (manifest: ManifestPayload) => void; disabled?: boolean }) {
  const projected = manifest ? projectManifestTimeline(manifest) : null;
  return <section className="ui-scrollbar max-h-72 space-y-2 overflow-auto text-[12px] text-text-dark">
    {selections.map((selection) => {
      const event = events.find((item) => item.selectionId === selection.id);
      return <div key={selection.id} className="flex items-center justify-between gap-2 rounded-[8px] px-2 py-1.5 hover:bg-white/[0.04]"><span className="truncate">{selection.label}</span>{selection.invalidReason ? <span className="shrink-0 text-amber-300">待修正</span> : <span className="shrink-0 text-text-muted">{event && event.startFrame !== undefined && event.endFrame !== undefined ? `${event.startFrame}–${event.endFrame} 帧` : '待编排'}</span>}</div>;
    })}
    {projected ? <div className="space-y-2 border-t border-white/10 pt-2">
      <div className="text-text-muted">画面层 · {projected.layers.length}</div>
      {projected.layers.map((layer, index) => <div key={`${layer.mediaId}-${index}`} className="grid grid-cols-[1fr_auto_auto] items-center gap-2 rounded-[8px] bg-white/[0.03] px-2 py-1.5">
        <span className="truncate">{layer.mediaId}</span><span>{layer.startFrame}–{layer.endFrame} 帧</span>
        <input aria-label={`${layer.mediaId} 结束帧`} disabled={disabled} className="nodrag w-14 rounded-[6px] border border-white/10 bg-black/20 px-1 py-0.5 disabled:opacity-50" type="number" value={layer.endFrame} onChange={(event) => manifest && onManifestChange?.(applyManifestEdit(manifest, { collection: 'layers', index, field: 'end_frame', value: Number(event.target.value) }))} />
      </div>)}
      <div className="text-text-muted">字幕 · {projected.subtitles.length}</div>
      {projected.subtitles.map((subtitle, index) => <input key={`${subtitle.startFrame}-${index}`} aria-label={`字幕 ${index + 1}`} disabled={disabled} className="nodrag w-full rounded-[6px] border border-white/10 bg-black/20 px-2 py-1 disabled:opacity-50" value={subtitle.text} onChange={(event) => manifest && onManifestChange?.(applyManifestEdit(manifest, { collection: 'subtitle_events', index, field: 'text', value: event.target.value }))} />)}
      <div className="text-text-muted">音频 · {projected.audio.length}</div>
      {projected.audio.map((track, index) => <label key={`${track.mediaId}-${index}`} className="grid grid-cols-[1fr_auto] items-center gap-2 rounded-[8px] bg-white/[0.03] px-2 py-1.5">
        <span className="truncate">{track.role} · {track.mediaId}</span>
        <input aria-label={`${track.mediaId} 音量`} disabled={disabled} className="nodrag w-16 rounded-[6px] border border-white/10 bg-black/20 px-1 py-0.5 disabled:opacity-50" type="number" min="0" max="2" step="0.05" value={track.volume} onChange={(event) => manifest && onManifestChange?.(applyManifestEdit(manifest, { collection: 'audio_events', index, field: 'volume', value: Number(event.target.value) }))} />
      </label>)}
    </div> : null}
  </section>;
}
