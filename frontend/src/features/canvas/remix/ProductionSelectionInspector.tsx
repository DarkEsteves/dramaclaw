import type { PlanAnchorV2, ProductionPlanV2 } from './productionPlanV2Model';
import { selectedAuthorText } from './ProductionScriptContent';

interface Props {
  plan: ProductionPlanV2;
  placementId: string;
  anchorOptions: Array<{ label: string; anchor: PlanAnchorV2 }>;
  change: (edit: (next: ProductionPlanV2) => void) => void;
  onChooseMedia: (placementId: string) => void;
}

function anchorLabel(plan: ProductionPlanV2, anchor: PlanAnchorV2): string {
  if (anchor.kind === 'text') {
    const turn = plan.beats.flatMap((beat) => beat.turns).find((row) => row.id === anchor.ref_id);
    return `台词文字「${turn?.text.slice(0, 24) ?? '已变化'}」第 ${anchor.char_offset ?? 0} 字`;
  }
  if (anchor.kind === 'selection') return `命名范围「${plan.selections?.find((row) => row.id === anchor.ref_id)?.label ?? '已变化'}」`;
  if (anchor.kind === 'program_frame') return `固定画面时间 · ${anchor.frame ?? 0} 帧`;
  return `${anchor.kind === 'beat' ? '段落' : anchor.kind === 'turn' ? '台词' : '素材'} · ${anchor.edge === 'start' ? '开始' : '结束'}`;
}

/** Settings for one use of a resource; the resource itself is never mutated. */
export function ProductionSelectionInspector({ plan, placementId, anchorOptions, change, onChooseMedia }: Props) {
  const placement = plan.placements.find((row) => row.id === placementId);
  if (!placement) return null;
  const textRange = placement.start.kind === 'text' && placement.end.kind === 'text'
    && placement.start.ref_id && placement.end.ref_id
    && placement.start.char_offset != null && placement.end.char_offset != null
      ? selectedAuthorText(plan, { start: { turn_id: placement.start.ref_id, char_offset: placement.start.char_offset },
        end: { turn_id: placement.end.ref_id, char_offset: placement.end.char_offset } }) : '';
  const updateAnchor = (edge: 'start' | 'end', anchor: PlanAnchorV2) => change((next) => {
    const row = next.placements.find((item) => item.id === placementId);
    if (row) row[edge] = anchor;
  });
  return <aside aria-label="选中素材设置" className="space-y-3 rounded-xl border border-primary/40 bg-white/[0.035] p-4">
    <div className="flex flex-wrap items-baseline justify-between gap-2">
      <h3 className="text-sm font-semibold">选中素材 · {placement.description || placement.text || '未命名内容'}</h3>
      <span className="text-xs text-text-muted">调整的是这一次使用，不影响同一素材的其他位置</span>
    </div>
    {textRange && <p className="text-xs text-text-muted">出现于台词：{textRange}</p>}
    <div className="grid gap-3 sm:grid-cols-2">
      {(['start', 'end'] as const).map((edge) => <label key={edge} className="min-w-0 text-xs text-text-muted">
        {edge === 'start' ? '出现于 · 开始' : '出现于 · 结束'}
        <select className="tap-field mt-1 w-full text-sm" aria-label={`选中素材${edge === 'start' ? '开始' : '结束'}范围`}
          value={JSON.stringify(placement[edge])}
          onChange={(event) => updateAnchor(edge, JSON.parse(event.target.value) as PlanAnchorV2)}>
          {!anchorOptions.some(({ anchor }) => JSON.stringify(anchor) === JSON.stringify(placement[edge]))
            && <option value={JSON.stringify(placement[edge])}>{anchorLabel(plan, placement[edge])}</option>}
          {anchorOptions.map(({ label, anchor }) => <option key={`${anchor.kind}:${anchor.ref_id}:${anchor.edge}`}
            value={JSON.stringify(anchor)}>{label}</option>)}
        </select>
      </label>)}
    </div>
    {(placement.kind === 'visual' || placement.kind === 'audio') && <button type="button" className="tap-button"
      onClick={() => onChooseMedia(placementId)}>{placement.resource_id ? '更换这次使用的素材' : '为这次使用选择素材'}</button>}
  </aside>;
}
