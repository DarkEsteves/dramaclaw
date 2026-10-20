import { useMemo, useState } from 'react';
import { Film, Image as ImageIcon, Search, Volume2, X } from 'lucide-react';
import type { PlanCueKind, PlanMediaCandidate } from '@/features/canvas/remix/productionPlanReadiness';

export type PlanMaterialMode = 'image' | 'video' | 'upload' | 'audio';

interface Props {
  kind: PlanCueKind;
  description: string;
  candidates: PlanMediaCandidate[];
  recentNodeId?: string;
  busy?: boolean;
  error?: string | null;
  onSelect: (nodeId: string) => void;
  onCreate: (mode: PlanMaterialMode) => void;
  onClose: () => void;
}

function sourceUrl(candidate: PlanMediaCandidate): string | null {
  const data = candidate.node.data;
  const value = candidate.kind === 'image' ? data.imageUrl ?? data.previewImageUrl
    : candidate.kind === 'video' ? data.videoUrl : data.audioUrl;
  return typeof value === 'string' && value ? value : candidate.media?.url ?? null;
}

const quietButton = 'rounded-full border border-[var(--ui-border-strong)] px-3 py-1.5 text-xs text-text-dark hover:bg-surface-field disabled:opacity-40';

export function ProductionPlanMediaPicker({ kind, description, candidates, recentNodeId, busy, error, onSelect, onCreate, onClose }: Props) {
  const [query, setQuery] = useState('');
  const shown = useMemo(() => candidates.filter((candidate) =>
    (kind === 'visuals' ? candidate.kind !== 'audio' : candidate.kind !== 'image')
      && candidate.label.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()))
    .sort((left, right) => Number(right.node.id === recentNodeId) - Number(left.node.id === recentNodeId)), [candidates, kind, query, recentNodeId]);
  const eligible = candidates.some((candidate) => kind === 'visuals' ? candidate.kind !== 'audio' : candidate.kind !== 'image');

  return <div className="absolute inset-0 z-10 flex justify-end bg-black/60" onClick={onClose}>
    <aside aria-label={kind === 'visuals' ? '选择画面素材' : '选择声音素材'}
      className="flex h-full w-full max-w-[480px] flex-col border-l border-[var(--ui-border-strong)] bg-[var(--ui-surface-modal)] shadow-2xl"
      onClick={(event) => event.stopPropagation()} onKeyDown={(event) => { if (event.key === 'Escape') onClose(); }}>
      <header className="flex items-start justify-between gap-4 border-b border-[var(--ui-border-soft)] px-5 py-4">
        <div className="min-w-0"><h3 className="text-sm font-semibold">{kind === 'visuals' ? '选择画面素材' : '选择声音素材'}</h3>
          <p className="mt-1 line-clamp-2 text-xs leading-5 text-text-muted">用于：{description || '当前条目'}</p></div>
        <button type="button" aria-label="关闭素材选择" className={quietButton} onClick={onClose}><X className="h-4 w-4" /></button>
      </header>
      <div className="relative mx-5 my-4"><Search aria-hidden="true" className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-text-muted" />
        <input autoFocus aria-label="搜索画布素材" placeholder="搜索画布素材" value={query} onChange={(event) => setQuery(event.target.value)}
          className="w-full rounded-lg border border-[var(--ui-border-soft)] bg-surface-field py-2 pl-9 pr-3 text-sm text-text-dark outline-none focus:border-primary/60" /></div>
      <div className="min-h-0 flex-1 space-y-2 overflow-y-auto px-5 pb-5">
        {shown.map((candidate) => {
          const url = sourceUrl(candidate);
          const stale = candidate.node.data.remixVisualOutputCurrent === false || candidate.node.data.workflowResultStale === true;
          return <div key={candidate.node.id} className="flex min-w-0 items-center gap-3 rounded-xl border border-[var(--ui-border-soft)] bg-white/[0.035] p-2">
            <div className="flex h-20 w-28 shrink-0 items-center justify-center overflow-hidden rounded-lg bg-surface-field text-text-muted">
              {candidate.kind === 'image' && url ? <img src={url} alt={candidate.label} className="h-full w-full object-cover" />
                : candidate.kind === 'video' && url ? <video src={url} controls preload="metadata" aria-label={candidate.label} className="h-full w-full object-cover" />
                  : candidate.kind === 'video' ? <Film className="h-5 w-5" /> : candidate.kind === 'audio' ? <Volume2 className="h-5 w-5" /> : <ImageIcon className="h-5 w-5" />}
            </div>
            <div className="min-w-0 flex-1"><div className="truncate text-sm text-text-dark" title={candidate.label}>{candidate.label}</div>
              <div className="mt-1 text-xs text-text-muted">{candidate.kind === 'image' ? '图片' : candidate.kind === 'video' ? '视频' : '声音'}{stale ? ' · 需要重新生成' : ''}</div>
              {candidate.kind === 'audio' && url && <audio src={url} controls preload="metadata" aria-label={candidate.label} className="mt-2 h-8 w-full" />}</div>
            <button type="button" className={quietButton} disabled={busy || stale} onClick={() => onSelect(candidate.node.id)} aria-label={`绑定 ${candidate.label}`}>绑定</button>
          </div>;
        })}
        {shown.length === 0 && <p className="rounded-xl border border-dashed border-[var(--ui-border-soft)] p-4 text-sm text-text-muted">
          {eligible && query.trim() ? '没有匹配的画布素材' : kind === 'visuals' ? '画布上还没有可用的画面素材' : '画布上还没有可用的声音素材'}
        </p>}
      </div>
      <footer className="border-t border-[var(--ui-border-soft)] px-5 py-4">{error && <p role="alert" className="mb-2 text-xs text-red-300">{error}</p>}<p className="mb-2 text-xs text-text-muted">没有合适的素材？创建节点后完成生成或上传，回到方案会自动关联。</p>
        <div className="flex flex-wrap gap-2">{kind === 'visuals' ? <>
          <button type="button" className={quietButton} onClick={() => onCreate('image')}>生成图片</button>
          <button type="button" className={quietButton} onClick={() => onCreate('video')}>生成视频</button>
          <button type="button" className={quietButton} onClick={() => onCreate('upload')}>上传图片</button>
        </> : <button type="button" className={quietButton} onClick={() => onCreate('audio')}>创建声音节点</button>}</div>
      </footer>
    </aside>
  </div>;
}
