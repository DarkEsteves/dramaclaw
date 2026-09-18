import type { NamedSelection } from './model';

export function RemixEditor({ selections, events }: { selections: NamedSelection[]; events: Array<{ selectionId: string; startFrame?: number; endFrame?: number }> }) {
  return <section className="ui-scrollbar max-h-48 space-y-1 overflow-auto text-[12px] text-text-dark">
    {selections.map((selection) => {
      const event = events.find((item) => item.selectionId === selection.id);
      return <div key={selection.id} className="flex items-center justify-between gap-2 rounded-[8px] px-2 py-1.5 hover:bg-white/[0.04]"><span className="truncate">{selection.label}</span>{selection.invalidReason ? <span className="shrink-0 text-amber-300">待修正</span> : <span className="shrink-0 text-text-muted">{event && event.startFrame !== undefined && event.endFrame !== undefined ? `${event.startFrame}–${event.endFrame} 帧` : '待编排'}</span>}</div>;
    })}
  </section>;
}
