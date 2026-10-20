import { useRef, useState } from 'react';
import type { ProductionPlanV2 } from './productionPlanV2Model';
import { readProductionTextSelection, type ProductionTextRange } from './productionTextSelection';

type MaterialKind = 'visual' | 'audio' | 'subtitle';

interface Props {
  plan: ProductionPlanV2;
  onAddRange: (kind: MaterialKind, range: ProductionTextRange, text: string) => void | Promise<void>;
  onSelectPlacement?: (placementId: string) => void;
  previewUrls?: Record<string, string>;
  onReplaceRange?: (range: ProductionTextRange, text: string) => void | Promise<void>;
}

export function selectedAuthorText(plan: ProductionPlanV2, range: ProductionTextRange): string {
  const turns = plan.beats.flatMap((beat) => beat.turns);
  const first = turns.findIndex((turn) => turn.id === range.start.turn_id);
  const last = turns.findIndex((turn) => turn.id === range.end.turn_id);
  if (first < 0 || last < first) return '';
  return turns.slice(first, last + 1).map((turn, index, rows) => {
    const points = Array.from(turn.text);
    return points.slice(index === 0 ? range.start.char_offset : 0,
      index === rows.length - 1 ? range.end.char_offset : points.length).join('');
  }).join('\n');
}

/** Selectable author text. The parent saves the action through the plan editor. */
export function ProductionScriptContent({ plan, onAddRange, onSelectPlacement, previewUrls = {}, onReplaceRange }: Props) {
  const root = useRef<HTMLElement | null>(null);
  const [selection, setSelection] = useState<{ range: ProductionTextRange; text: string } | null>(null);
  const readSelection = () => {
    if (!root.current) return;
    const range = readProductionTextSelection(root.current, window.getSelection());
    const text = range ? selectedAuthorText(plan, range) : '';
    setSelection(range && text ? { range, text } : null);
  };
  const add = (kind: MaterialKind) => {
    if (!selection) return;
    void onAddRange(kind, selection.range, selection.text);
    setSelection(null);
  };
  return <section ref={root} aria-label="可选取台词范围"
    onMouseUp={(event) => { if (!(event.target instanceof Element && event.target.closest('button'))) readSelection(); }}
    onKeyUp={(event) => { if (!(event.target instanceof Element && event.target.closest('button'))) readSelection(); }}
    className="space-y-3 rounded-xl border border-[var(--ui-border-soft)] bg-white/[0.025] p-4">
    <div className="flex flex-wrap items-baseline justify-between gap-2">
      <h3 className="text-sm font-semibold text-text-dark">图文脚本</h3>
      <span className="text-xs text-text-muted">选取台词，可让画面、声音或字幕跟随这段内容</span>
    </div>
    {plan.beats.map((beat, index) => {
      const ownTurns = new Set(beat.turns.map((turn) => turn.id));
      const placed = plan.placements.filter((item) => {
        const start = item.start.kind === 'selection'
          ? plan.selections?.find((selection) => selection.id === item.start.ref_id)?.start : item.start;
        return start?.kind === 'beat' && start.ref_id === beat.id
          || Boolean(start && ['text', 'turn'].includes(start.kind) && ownTurns.has(start.ref_id ?? ''))
          || Boolean(item.turn_id && ownTurns.has(item.turn_id));
      });
      const visual = placed.filter((item) => item.kind === 'visual').length;
      const audio = placed.filter((item) => item.kind === 'audio').length;
      return <div key={beat.id} className="space-y-2 border-t border-[var(--ui-border-soft)] pt-3 first:border-0 first:pt-0">
        <div className="flex flex-wrap items-baseline gap-2"><span className="text-xs text-primary">{String(index + 1).padStart(2, '0')}</span>
          <strong className="text-xs text-text-dark">{beat.purpose}</strong>
          <span className="text-xs text-text-muted">{visual ? `${visual} 项画面` : '未添加画面'} · {audio ? `${audio} 项声音` : '未添加声音'}</span>
        </div>
        {beat.turns.length ? beat.turns.map((turn) => <p key={turn.id} className="text-sm leading-7 text-text-dark">
          <span className="mr-2 select-none text-xs text-text-muted">{turn.speaker || '台词'}：</span>
          <span data-turn-id={turn.id} className="cursor-text select-text rounded-sm selection:bg-primary/35">{turn.text}</span>
        </p>) : <p className="text-xs text-text-muted">无台词 · 按段落和素材时长安排画面</p>}
        {placed.length > 0 && <div className="grid gap-2 sm:grid-cols-2">
          {placed.map((placement) => {
            const resource = plan.resources.find((item) => item.id === placement.resource_id);
            const preview = previewUrls[placement.id];
            const description = placement.description?.trim() || placement.text?.trim() ||
              (placement.kind === 'visual' ? '画面' : placement.kind === 'audio' ? '声音' : '字幕与图形');
            const status = placement.kind === 'subtitle' || placement.kind === 'graphic'
              ? '跟随内容' : preview ? '已找到素材' : resource ? '已绑定素材' : '待添加素材';
            return <button type="button" key={placement.id} data-script-placement-id={placement.id}
              onClick={() => onSelectPlacement?.(placement.id)}
              className="flex min-w-0 items-center gap-2 rounded-lg border border-[var(--ui-border-soft)] bg-white/[0.025] p-2 text-left hover:border-primary/55">
              {preview && placement.kind === 'visual'
                ? <img src={preview} alt={description} className="h-14 w-12 shrink-0 rounded object-cover" />
                : <span aria-hidden="true" className="flex h-14 w-12 shrink-0 items-center justify-center rounded bg-white/[0.04] text-xs text-text-muted">
                  {placement.kind === 'visual' ? '画面' : placement.kind === 'audio' ? '声音' : '文字'}</span>}
              <span className="min-w-0"><span className="block truncate text-xs text-text-dark">{description}</span>
                <span className="block text-xs text-text-muted">{status}</span></span>
            </button>;
          })}
        </div>}
      </div>;
    })}
    {selection && <div className="flex flex-wrap items-center gap-2 border-t border-[var(--ui-border-soft)] pt-3 text-xs">
      <span className="max-w-48 truncate text-text-muted" title={selection.text}>{selection.text}</span>
      <button type="button" className="tap-button" onClick={() => add('visual')} aria-label="选中台词添加画面">添加画面</button>
      <button type="button" className="tap-button" onClick={() => add('audio')} aria-label="选中台词添加声音">添加声音</button>
      <button type="button" className="tap-button" onClick={() => add('subtitle')} aria-label="选中台词添加字幕">强调字幕</button>
      {onReplaceRange && <button type="button" className="tap-button tap-button-quiet-primary"
        onClick={() => { void onReplaceRange(selection.range, selection.text); setSelection(null); }}>更新选中素材范围</button>}
    </div>}
  </section>;
}
