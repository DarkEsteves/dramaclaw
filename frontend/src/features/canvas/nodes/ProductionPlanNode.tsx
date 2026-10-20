import { memo, useEffect, useMemo, useRef, useState } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { FileText } from 'lucide-react';
import { CANVAS_NODE_TYPES, type ScriptNodeData } from '@/features/canvas/domain/canvasNodes';
import { NodeHeader, NODE_HEADER_FLOATING_POSITION_CLASS } from '@/features/canvas/ui/NodeHeader';
import { CANVAS_NODE_PANEL_SURFACE_CLASS } from '@/features/canvas/ui/nodeFrameStyles';
import { useCanvasStore } from '@/stores/canvasStore';
import type { RemixArtifactRef } from '@/api/remix';
import { readUrl } from '@/lib/url-params';
import { canvasEventBus } from '@/features/canvas/application/canvasServices';
import { planMediaCandidates } from '@/features/canvas/remix/productionPlanModel';
import { bindProductionCanvasCandidate } from '@/features/canvas/remix/productionCanvasCommand';
import { flushProductionPlanV2AutoSave, scheduleProductionPlanV2AutoSave } from '@/features/canvas/remix/productionPlanV2AutoSave';
import { projectProductionTimeline } from '@/features/canvas/compose/productionComposeModel';
import { emptyProductionPlanV2, type ProductionPlanV2 } from '@/features/canvas/remix/productionPlanV2Model';
import { ProductionPlanV2Editor } from './ProductionPlanV2Editor';

type Props = NodeProps & { id: string; data: ScriptNodeData; selected?: boolean };

export const ProductionPlanNode = memo(({ id, data, selected }: Props) => {
  const { project, canvas } = readUrl();
  const nodes = useCanvasStore((state) => state.nodes);
  const updateNodeData = useCanvasStore((state) => state.updateNodeData);
  const setSelectedNode = useCanvasStore((state) => state.setSelectedNode);
  const [open, setOpen] = useState(false);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const candidates = useMemo(() => project ? planMediaCandidates(project, nodes, id) : [], [project, nodes, id]);
  const attemptedBindings = useRef<Map<string, string>>(new Map());
  const bindingsInFlight = useRef<Set<string>>(new Set());
  const draft = data.productionPlanV2Draft as ProductionPlanV2 | undefined;
  const planRef = data.productionPlanV2Ref as RemixArtifactRef | undefined;

  useEffect(() => canvasEventBus.subscribe('remix-node/action', ({ nodeId, action }) => {
    if (nodeId === id && action === 'edit_production_plan') setOpen(true);
  }), [id]);

  useEffect(() => {
    if (!data.productionPlanOpenOnCreate) return;
    setOpen(true);
    updateNodeData(id, { productionPlanOpenOnCreate: false });
  }, [data.productionPlanOpenOnCreate, id, updateNodeData]);

  useEffect(() => {
    if (draft || planRef) return;
    const topic = (data.processingPayload as { topic?: unknown } | undefined)?.topic;
    updateNodeData(id, { productionPlanV2Draft: emptyProductionPlanV2(typeof topic === 'string' ? topic : ''), productionPlanV2Dirty: false });
  }, [draft, planRef, data.processingPayload, id, updateNodeData]);

  useEffect(() => {
    if (project && draft?.goal.trim() && data.productionPlanV2Dirty && !data.productionPlanV2Conflict) scheduleProductionPlanV2AutoSave(project, id);
  }, [project, id, data.productionPlanV2Dirty, data.productionPlanV2Conflict]);

  useEffect(() => {
    if (!project || !canvas || !draft) return;
    const pending = (data.productionPlanV2PendingBindings as Record<string, string> | undefined) ?? {};
    for (const [placementId, nodeId] of Object.entries(pending)) {
      if (bindingsInFlight.current.has(placementId)) continue;
      const placement = draft.placements.find((item) => item.id === placementId);
      const candidate = candidates.find((item) => item.node.id === nodeId);
      if (!placement || !['visual', 'audio'].includes(placement.kind) || !candidate) continue;
      const signature = `${nodeId}:${candidate.media?.media_hash ?? candidate.currentPath ?? ''}:${candidate.node.data.workflowResultStale}`;
      if (attemptedBindings.current.get(placementId) === signature) continue;
      attemptedBindings.current.set(placementId, signature);
      bindingsInFlight.current.add(placementId);
      void (async () => {
        try {
          await bindProductionCanvasCandidate(project, canvas, id, placementId, nodeId, () => {
            const current = useCanvasStore.getState().nodes.find((node) => node.id === id)?.data;
            return (current?.productionPlanV2PendingBindings as Record<string, string> | undefined)?.[placementId] === nodeId;
          });
          const store = useCanvasStore.getState();
          const current = store.nodes.find((node) => node.id === id)?.data;
          const currentPending = (current?.productionPlanV2PendingBindings as Record<string, string> | undefined) ?? {};
          if (currentPending[placementId] !== nodeId) return;
          const remaining = { ...currentPending }; delete remaining[placementId];
          const errors = { ...((current?.productionPlanV2PendingErrors as Record<string, string> | undefined) ?? {}) }; delete errors[placementId];
          store.updateNodeData(id, { productionPlanV2PendingBindings: remaining, productionPlanV2PendingErrors: errors });
        } catch (cause) {
          const current = useCanvasStore.getState().nodes.find((node) => node.id === id)?.data;
          if ((current?.productionPlanV2PendingBindings as Record<string, string> | undefined)?.[placementId] === nodeId) {
            useCanvasStore.getState().updateNodeData(id, { productionPlanV2PendingErrors: {
              ...((current?.productionPlanV2PendingErrors as Record<string, string> | undefined) ?? {}),
              [placementId]: cause instanceof Error ? cause.message : String(cause),
            } });
          }
        } finally { bindingsInFlight.current.delete(placementId); }
      })();
      break;
    }
  }, [project, canvas, id, draft, data.productionPlanV2PendingBindings, candidates]);

  const close = () => {
    setOpen(false);
    if (project) void flushProductionPlanV2AutoSave(project, id).catch(() => undefined);
  };

  const openCompose = async () => {
    if (working || !project) return;
    setWorking(true); setError(null);
    try {
      const store = useCanvasStore.getState();
      const exactRef = await flushProductionPlanV2AutoSave(project, id);
      if (!exactRef) throw new Error('请先填写并保存作品方案');
      const linked = store.nodes.find((node) => node.type === CANVAS_NODE_TYPES.videoCompose
        && (node.data.productionPlanNodeId === id || store.edges.some((edge) => edge.source === id && edge.target === node.id)));
      const composeData = { displayName: '视频合成', remixRole: 'production_compose', productionPlanNodeId: id,
        productionPlanV2Ref: exactRef, productionComposeOpenOnCreate: true };
      const composeId = linked?.id ?? store.addNode(CANVAS_NODE_TYPES.videoCompose, store.findNodePosition(id, 460, 300), composeData);
      if (linked) store.updateNodeData(composeId, composeData);
      if (!store.edges.some((edge) => edge.source === id && edge.target === composeId) && !store.addEdge(id, composeId)) {
        if (!linked) store.deleteNodes([composeId]);
        throw new Error('无法连接作品方案与视频合成');
      }
      store.setSelectedNode(composeId);
      store.requestFocusNode(composeId);
      setOpen(false);
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setWorking(false); }
  };

  const duration = draft ? projectProductionTimeline(draft).durationFrames * draft.output.fps_den / draft.output.fps_num : 0;
  const saveStatus = data.productionPlanV2SaveError ? '自动保存失败' : data.productionPlanV2Dirty ? '正在自动保存…' : planRef ? '已自动保存' : '填写内容后自动保存';
  return <div className="group relative h-[250px] w-[410px] overflow-visible" onClick={() => setSelectedNode(id)}>
    <Handle type="target" position={Position.Left} id="target" className="!h-2 !w-2 !border-0 !bg-[rgb(148,163,184)]" />
    <NodeHeader className={NODE_HEADER_FLOATING_POSITION_CLASS} icon={<FileText className="h-3 w-3" />} titleText="作品方案" metaText={planRef ? `版本 ${planRef.revision}` : '草稿'} />
    <div className={`h-full rounded-[var(--node-radius)] border p-4 ${CANVAS_NODE_PANEL_SURFACE_CLASS} ${selected ? 'border-primary/55' : 'border-[var(--ui-border-strong)]'}`}>
      <div className="line-clamp-2 text-sm font-medium text-text-dark">{draft?.goal || '填写目标，编排画面、台词与声音'}</div>
      <div className="mt-3 text-xs text-text-muted">{draft?.beats.length ?? 0} 段 · 约 {duration.toFixed(1)} 秒 · {saveStatus}</div>
      <p className="mt-4 line-clamp-3 text-xs leading-5 text-text-dark/65">{draft?.beats.map((beat) => beat.purpose).join(' · ') || '填写作品目标后可直接起草方案'}</p>
      <button type="button" className="nodrag mt-5 rounded-full bg-primary px-4 py-2 text-xs font-medium text-primary-foreground hover:bg-primary/90" onClick={(event) => { event.stopPropagation(); setOpen(true); }}>编辑作品方案</button>
      {error && <p role="alert" className="mt-2 text-xs text-red-300">{error}</p>}
    </div>
    {open && <ProductionPlanV2Editor id={id} project={project} canvasId={canvas} data={data} onClose={close} onCompose={() => void openCompose()} />}
    <Handle type="source" position={Position.Right} id="source" className="!h-2 !w-2 !border-0 !bg-[rgb(148,163,184)]" />
  </div>;
});
