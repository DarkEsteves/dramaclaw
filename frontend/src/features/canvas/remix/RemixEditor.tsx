import type { NamedSelection } from './model';

export function RemixEditor({ selections, events }: { selections: NamedSelection[]; events: Array<{ selectionId: string; startFrame?: number; endFrame?: number }> }) {
  return <section className="space-y-2 text-[12px] text-text-dark">
    {selections.map((selection) => {
      const event = events.find((item) => item.selectionId === selection.id);
      return <div key={selection.id} className="rounded-md border border-white/[0.08] p-2"><div>{selection.label}</div>{selection.invalidReason ? <div className="text-amber-300">{selection.invalidReason}</div> : <div className="text-text-muted">{event ? `frames ${event.startFrame}–${event.endFrame}` : 'Awaiting timing'}</div>}</div>;
    })}
  </section>;
}
