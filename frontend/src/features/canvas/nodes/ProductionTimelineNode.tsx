import { memo, useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { Clock3, X } from 'lucide-react';
import { CANVAS_NODE_TYPES, type SemanticTimelineNodeData } from '@/features/canvas/domain/canvasNodes';
import { NodeHeader, NODE_HEADER_FLOATING_POSITION_CLASS } from '@/features/canvas/ui/NodeHeader';
import { CANVAS_NODE_PANEL_SURFACE_CLASS, canvasNodeFrameClass } from '@/features/canvas/ui/nodeFrameStyles';
import { compileProductionPlan, createRemixArtifact, type RemixArtifactRef } from '@/api/remix';
import { flushProductionPlanAutoSave, scheduleProductionPlanAutoSave } from '@/features/canvas/remix/productionPlanAutoSave';
import { planDuration, planMediaCandidates, validateProductionPlanBindings, type PlanBeat, type PlanCue, type PlanTurn, type ProductionPlan } from '@/features/canvas/remix/productionPlanModel';
import { inspectProductionPlan } from '@/features/canvas/remix/productionPlanReadiness';
import { fitBeatToSingleVoice, retimeBeat, voiceTimingHint } from '@/features/canvas/remix/productionTimelineModel';
import { prepareNodeRemix, remixGraphFingerprint } from '@/features/canvas/remix/prepareNodeRemix';
import { applyRemixInvalidation } from '@/features/canvas/remix/spawnRemixDownstream';
import { canvasEventBus } from '@/features/canvas/application/canvasServices';
import { readUrl } from '@/lib/url-params';
import { useCanvasStore } from '@/stores/canvasStore';

type Data = SemanticTimelineNodeData & {
  productionPlanNodeId?: string;
  productionTimelineOpenOnCreate?: boolean;
  compiledProductionPlanRef?: RemixArtifactRef | null;
  productionTimelineDirty?: boolean;
};
type Props = NodeProps & { id: string; data: Data; selected?: boolean };

const quietButton = 'nodrag rounded-full border border-[var(--ui-border-strong)] px-3 py-1.5 text-xs text-text-dark/80 hover:bg-white/[0.06] disabled:opacity-40';
const field = 'nodrag w-20 rounded-lg border border-[var(--ui-border-soft)] bg-surface-field px-2 py-1.5 text-xs text-text-dark outline-none focus:border-primary/60';

function sameRef(left: RemixArtifactRef | null | undefined, right: RemixArtifactRef | null | undefined) {
  return Boolean(left && right && left.artifactId === right.artifactId && left.revision === right.revision);
}

function sourceLabel(nodeId: string, nodes: ReturnType<typeof useCanvasStore.getState>['nodes']) {
  const node = nodes.find((item) => item.id === nodeId);
  return typeof node?.data.displayName === 'string' && node.data.displayName.trim() ? node.data.displayName : '已绑定素材';
}

function timingLabel(start: number | null | undefined, end: number | null | undefined, duration: number, fps: number) {
  const a = (start ?? 0) / fps;
  const b = (end ?? duration) / fps;
  return `${a.toFixed(1)}–${b.toFixed(1)} 秒`;
}

export const ProductionTimelineNode = memo(({ id, data, selected }: Props) => {
  const project = readUrl().project;
  const nodes = useCanvasStore((state) => state.nodes);
  const updateNodeData = useCanvasStore((state) => state.updateNodeData);
  const setSelectedNode = useCanvasStore((state) => state.setSelectedNode);
  const [open, setOpen] = useState(Boolean(data.productionTimelineOpenOnCreate));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const planNode = nodes.find((node) => node.id === data.productionPlanNodeId);
  const plan = planNode?.data.productionPlanDraft as ProductionPlan | undefined;
  const planRef = planNode?.data.productionPlanRef as RemixArtifactRef | undefined;
  const manifestRef = data.artifactRef as RemixArtifactRef | undefined;
  const current = sameRef(data.compiledProductionPlanRef, planRef)
    && planNode?.data.productionPlanDirty !== true && data.productionTimelineDirty !== true;
  const ready = Boolean(manifestRef && current);

  useEffect(() => {
    if (!data.productionTimelineOpenOnCreate) return;
    setOpen(true);
    updateNodeData(id, { productionTimelineOpenOnCreate: false });
  }, [data.productionTimelineOpenOnCreate, id, updateNodeData]);

  const changeBeat = (index: number, edit: (beat: PlanBeat) => void) => {
    if (!plan || !data.productionPlanNodeId) return;
    const next = structuredClone(plan);
    edit(next.beats[index]);
    updateNodeData(data.productionPlanNodeId, { productionPlanDraft: next, productionPlanDirty: true, productionPlanSaveError: null });
    updateNodeData(id, { productionTimelineDirty: true });
    applyRemixInvalidation(id, 'media_binding');
    if (project) scheduleProductionPlanAutoSave(project, data.productionPlanNodeId);
    setError(null);
  };

  const updateRange = (beatIndex: number, kind: 'visuals' | 'audio' | 'turns', rowIndex: number,
    edge: 'start_frame' | 'end_frame', value: string) => changeBeat(beatIndex, (beat) => {
    const row = beat[kind][rowIndex] as PlanCue | PlanTurn;
    if (value === '') { row.start_frame = null; row.end_frame = null; return; }
    const frame = Math.round(Number(value) * (plan?.output.fps ?? 30));
    if (edge === 'start_frame') {
      row.start_frame = Math.max(0, Math.min(beat.duration_frames - 1, frame));
      row.end_frame = Math.max(row.start_frame + 1, row.end_frame ?? beat.duration_frames);
    } else {
      row.start_frame ??= 0;
      row.end_frame = Math.max(row.start_frame + 1, Math.min(beat.duration_frames, frame));
    }
  });

  const compile = async () => {
    if (!project || !plan || !data.productionPlanNodeId || busy) return;
    setBusy(true); setError(null);
    try {
      const saved = await flushProductionPlanAutoSave(project, data.productionPlanNodeId);
      if (!saved) throw new Error('作品方案尚未保存');
      const state = useCanvasStore.getState();
      const latest = state.nodes.find((node) => node.id === data.productionPlanNodeId)?.data.productionPlanDraft as ProductionPlan | undefined;
      if (!latest) throw new Error('作品方案不可用');
      const candidates = planMediaCandidates(project, state.nodes, data.productionPlanNodeId);
      const issues = inspectProductionPlan(latest, candidates, {}, state.nodes, data.productionPlanNodeId, state.edges).issues;
      if (issues.length) throw new Error(issues[0].reason);
      validateProductionPlanBindings(project, latest, state.nodes, state.edges, data.productionPlanNodeId);
      const payload = await compileProductionPlan(project, saved);
      const artifact = await createRemixArtifact(project, 'composition_manifest.v1', payload, [saved]);
      updateNodeData(id, { artifactRef: { artifactId: artifact.id, revision: artifact.revision },
        compiledProductionPlanRef: saved, productionTimelineDirty: false, remixTimingSchema: 'composition_manifest.v1' });
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(false); }
  };

  const enterCompose = async () => {
    if (!project || !ready || !manifestRef || busy) return;
    setBusy(true); setError(null);
    let createdComposeId: string | null = null;
    let migratedEdge: { id: string; source: string; target: string } | null = null;
    let newEdgeId: string | null = null;
    try {
      const store = useCanvasStore.getState();
      let composeId = store.nodes.find((node) => node.type === CANVAS_NODE_TYPES.videoCompose
        && store.edges.some((edge) => edge.source === id && edge.target === node.id))?.id;
      if (!composeId) {
        const legacy = store.edges.find((edge) => edge.source === data.productionPlanNodeId
          && store.nodes.some((node) => node.id === edge.target && node.type === CANVAS_NODE_TYPES.videoCompose
            && node.data.remixRole === 'production_compose'));
        if (legacy) {
          composeId = legacy.target;
          migratedEdge = { id: legacy.id, source: legacy.source, target: legacy.target };
        } else {
          composeId = store.addNode(CANVAS_NODE_TYPES.videoCompose, store.findNodePosition(id, 460, 280), {
            displayName: '视频合成', remixRole: 'production_compose', resultVideoUrl: null,
          });
          createdComposeId = composeId;
        }
        newEdgeId = store.addEdge(id, composeId);
        if (!newEdgeId) throw new Error('无法连接成片编排与视频合成');
        if (migratedEdge) store.deleteEdge(migratedEdge.id);
      }
      const latest = useCanvasStore.getState();
      const previous = latest.nodes.find((node) => node.id === composeId)?.data.remixManifestRef as RemixArtifactRef | undefined;
      const prepared = await prepareNodeRemix(project, latest.nodes, latest.edges, composeId,
        previous && !sameRef(previous, manifestRef) ? previous : undefined);
      const refreshed = useCanvasStore.getState();
      refreshed.updateNodeData(composeId, {
        displayName: '视频合成', remixRole: 'production_compose',
        remixManifestRef: prepared.manifestRef, remixManifestLoadState: 'loading',
        remixManifestConflicts: prepared.conflicts ?? [],
        remixInputFingerprint: remixGraphFingerprint(refreshed.nodes, refreshed.edges, composeId),
        remixInputsCurrent: true, remixNodeRevision: ((refreshed.nodes.find((node) => node.id === composeId)?.data.remixNodeRevision as number | undefined) ?? 0) + 1,
        unresolvedRequiredEvents: prepared.unresolvedRequiredEvents,
      });
      refreshed.setSelectedNode(composeId);
      refreshed.requestFocusNode(composeId);
      setOpen(false);
    } catch (cause) {
      const store = useCanvasStore.getState();
      if (createdComposeId) store.deleteNodes([createdComposeId]);
      else if (migratedEdge && newEdgeId) {
        store.addEdge(migratedEdge.source, migratedEdge.target);
        store.deleteEdge(newEdgeId);
      }
      setError(cause instanceof Error ? cause.message : String(cause));
    }
    finally { setBusy(false); }
  };

  useEffect(() => canvasEventBus.subscribe('remix-node/action', ({ nodeId, action }) => {
    if (nodeId !== id) return;
    if (action === 'open_production_timeline') setOpen(true);
    if (action === 'compile') { setOpen(true); void compile(); }
    if (action === 'create_compose') {
      if (ready) void enterCompose();
      else { setOpen(true); setError('请先核对时间并生成合成清单'); }
    }
  }), [id, ready, compile, enterCompose]);

  const row = (beat: PlanBeat, beatIndex: number, kind: 'visuals' | 'audio' | 'turns', rowIndex: number,
    item: PlanCue | PlanTurn) => {
    const fps = plan?.output.fps ?? 30;
    const label = kind === 'turns' ? (item as PlanTurn).text : (item as PlanCue).description || sourceLabel((item as PlanCue).media?.node_id ?? '', nodes);
    const color = kind === 'visuals' ? 'bg-primary/25 border-primary/40' : kind === 'audio' ? 'bg-emerald-400/20 border-emerald-400/40' : 'bg-amber-400/20 border-amber-400/40';
    const start = item.start_frame ?? 0;
    const end = item.end_frame ?? beat.duration_frames;
    return <div key={kind === 'turns' ? `${kind}-${rowIndex}` : (item as PlanCue).id} className="space-y-1.5">
      <div className="flex min-w-0 items-center justify-between gap-3 text-xs"><span className="truncate text-text-dark">{label || '未填写'}</span><span className="shrink-0 text-text-muted">{timingLabel(item.start_frame, item.end_frame, beat.duration_frames, fps)}</span></div>
      <div className="relative h-6 overflow-hidden rounded-md bg-white/[0.045]">
        <div className={`absolute top-0 h-full truncate rounded-md border px-2 text-[11px] leading-6 text-text-dark ${color}`}
          style={{ left: `${100 * start / beat.duration_frames}%`, width: `${100 * (end - start) / beat.duration_frames}%` }}>{label}</div>
      </div>
      <div className="flex items-center gap-2 text-[11px] text-text-muted">
        <label>开始秒 <input aria-label={`${kind} ${beatIndex + 1}-${rowIndex + 1} 开始秒`} type="number" min="0" step="0.1" className={field} value={item.start_frame == null ? '' : item.start_frame / fps} placeholder="0" onChange={(event) => updateRange(beatIndex, kind, rowIndex, 'start_frame', event.target.value)} /></label>
        <label>结束秒 <input aria-label={`${kind} ${beatIndex + 1}-${rowIndex + 1} 结束秒`} type="number" min="0" step="0.1" className={field} value={item.end_frame == null ? '' : item.end_frame / fps} placeholder={(beat.duration_frames / fps).toFixed(1)} onChange={(event) => updateRange(beatIndex, kind, rowIndex, 'end_frame', event.target.value)} /></label>
        {(item.start_frame != null || item.end_frame != null) && <button type="button" className={quietButton} onClick={() => updateRange(beatIndex, kind, rowIndex, 'start_frame', '')}>整段</button>}
      </div>
      {kind === 'audio' && (item as PlanCue).media && ['speech', 'narration'].includes((item as PlanCue).role) && (() => {
        const cue = item as PlanCue;
        const node = nodes.find((candidate) => candidate.id === cue.media?.node_id);
        const url = cue.media?.media_kind === 'video' ? node?.data.videoUrl : node?.data.audioUrl;
        return typeof url === 'string' ? <audio controls preload="none" className="nodrag h-8 w-full" src={url} /> : null;
      })()}
    </div>;
  };

  return <div className="group relative w-[420px]" onClick={() => setSelectedNode(id)}>
    <Handle type="target" position={Position.Left} id="target" className="!h-2 !w-2 !border-0 !bg-[rgb(148,163,184)]" />
    <Handle type="source" position={Position.Right} id="source" className="!h-2 !w-2 !border-0 !bg-[rgb(45,212,191)]" />
    <NodeHeader className={NODE_HEADER_FLOATING_POSITION_CLASS} icon={<Clock3 className="h-4 w-4" />} titleText="成片编排" editable={false} />
    <div className={`rounded-[var(--node-radius)] border p-4 ${CANVAS_NODE_PANEL_SURFACE_CLASS} ${canvasNodeFrameClass({ selected })}`}>
      <div className="text-sm font-medium text-text-dark">{plan?.goal || '作品方案尚未就绪'}</div>
      <div className="mt-2 text-xs text-text-muted">{plan ? `${plan.beats.length} 段 · 计划 ${planDuration(plan).toFixed(1)} 秒` : '等待方案'} · {ready ? '时间清单已生成' : manifestRef ? '方案已变化，需重新编排' : '待检查时间'}</div>
      <div className="mt-4 flex gap-2"><button type="button" className={quietButton} onClick={(event) => { event.stopPropagation(); setOpen(true); }}>查看时间线</button>
        {ready && <button type="button" className="nodrag rounded-full bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground" onClick={(event) => { event.stopPropagation(); void enterCompose(); }}>进入视频合成</button>}</div>
      {error && <p role="alert" className="mt-2 text-xs text-red-300">{error}</p>}
    </div>
    {open && createPortal(<div role="dialog" aria-label="成片编排" className="fixed inset-0 z-[1000] flex items-center justify-center bg-black/75 p-4" onClick={() => setOpen(false)}>
      <div className="nodrag nowheel flex max-h-[90vh] w-full max-w-[1060px] flex-col overflow-hidden rounded-2xl border border-[var(--ui-border-strong)] bg-[var(--ui-surface-modal)] text-text-dark shadow-2xl" onClick={(event) => event.stopPropagation()}>
        <header className="flex items-center justify-between border-b border-[var(--ui-border-soft)] px-6 py-4"><div><h2 className="text-base font-semibold">成片编排</h2><p className="mt-1 text-xs text-text-muted">先按最终素材核对时长，再检查画面、声音与字幕的位置。</p></div><button type="button" className={quietButton} aria-label="关闭" onClick={() => setOpen(false)}><X className="h-4 w-4" /></button></header>
        <main className="ui-scrollbar min-h-0 space-y-4 overflow-y-auto px-6 py-5">
          {!plan && <p className="text-sm text-amber-300">连接的作品方案不可用。</p>}
          {plan?.beats.map((beat, beatIndex) => {
            const hint = voiceTimingHint(beat, plan.output.fps);
            return <section key={beat.id} className="rounded-xl border border-[var(--ui-border-soft)] bg-white/[0.03] p-4">
              <div className="flex flex-wrap items-center gap-3"><span className="rounded-full bg-primary/15 px-2 py-1 text-xs text-primary">{String(beatIndex + 1).padStart(2, '0')}</span><h3 className="min-w-0 flex-1 text-sm font-medium">{beat.purpose}</h3><label className="text-xs text-text-muted">段落秒数 <input aria-label={`段落 ${beatIndex + 1} 编排时长`} type="number" min="0.1" max="60" step="0.1" className={field} value={beat.duration_frames / plan.output.fps} onChange={(event) => changeBeat(beatIndex, (draft) => Object.assign(draft, retimeBeat(draft, Math.max(1, Math.round(Number(event.target.value) * plan.output.fps)))))} /></label></div>
              {hint && <div className={`mt-3 flex flex-wrap items-center gap-3 rounded-lg px-3 py-2 text-xs ${hint.differs ? 'bg-amber-400/[0.07] text-amber-200' : 'bg-emerald-400/[0.06] text-emerald-200'}`}><span>人声 {hint.sourceSeconds.toFixed(1)} 秒 · 计划 {hint.plannedSeconds.toFixed(1)} 秒</span>{hint.differs && <button type="button" className={quietButton} onClick={() => { const fitted = fitBeatToSingleVoice(beat, plan.output.fps); if (fitted) changeBeat(beatIndex, (draft) => Object.assign(draft, fitted)); }}>按人声时长调整</button>}</div>}
              {beat.audio.filter((cue) => cue.role === 'speech' || cue.role === 'narration').length > 1 && <p className="mt-3 text-xs text-amber-200">这段有多个人声素材，请试听并逐项核对位置。</p>}
              {beat.turns.length > 1 && beat.turns.some((turn) => turn.start_frame == null) && <p className="mt-3 text-xs text-amber-200">多句字幕尚未按实际说话时间定位；当前会平均分配，请试听后填写起止时间。</p>}
              <div className="mt-4 grid gap-4 lg:grid-cols-3">{(['visuals', 'audio', 'turns'] as const).map((kind) => <div key={kind} className="min-w-0 space-y-3"><h4 className="border-b border-white/[0.07] pb-1 text-xs font-medium text-text-muted">{kind === 'visuals' ? '画面' : kind === 'audio' ? '声音' : '字幕'}</h4>{beat[kind].length ? beat[kind].map((item, index) => row(beat, beatIndex, kind, index, item)) : <p className="text-xs text-text-muted">无</p>}</div>)}</div>
            </section>;
          })}
          {error && <p role="alert" className="rounded-lg border border-red-400/25 bg-red-500/10 px-3 py-2 text-sm text-red-300">{error}</p>}
        </main>
        <footer className="flex items-center justify-between gap-3 border-t border-[var(--ui-border-soft)] px-6 py-4"><span className="text-xs text-text-muted">{ready ? '编排清单已生成' : '修改会自动保存到作品方案；编排时长需核对'}</span><div className="flex gap-2"><button type="button" disabled={busy || !plan} className={quietButton} onClick={() => void compile()}>{busy ? '处理中…' : ready ? '重新生成清单' : '生成合成清单'}</button><button type="button" disabled={busy || !ready} className="nodrag rounded-full bg-primary px-4 py-2 text-xs font-medium text-primary-foreground disabled:opacity-40" onClick={() => void enterCompose()}>进入视频合成</button></div></footer>
      </div>
    </div>, document.body)}
  </div>;
});

ProductionTimelineNode.displayName = 'ProductionTimelineNode';
