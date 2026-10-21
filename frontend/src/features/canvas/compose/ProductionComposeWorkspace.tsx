import { editPlanSpeechTakeSource } from '@/features/canvas/remix/productionPlanV2Edits';
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent } from 'react';
import { createPortal } from 'react-dom';
import { ChevronDown, ChevronRight, Film, Music2, Pause, Play, Plus, RefreshCcw, Scissors, Trash2, Undo2, Redo2, X } from 'lucide-react';
import { CANVAS_NODE_TYPES } from '@/features/canvas/domain/canvasNodes';
import { useCanvasStore } from '@/stores/canvasStore';
import { useViewerImmersiveBody } from '@/features/viewer-kit/useViewerImmersiveBody';
import { createRemixArtifact, fetchLatestRemixArtifact, fetchRemixArtifact, listRemixOperations, previewProductionFrame, projectProductionPlan, type ProductionPlanProjection, type RemixArtifactRef, type RemixOperation } from '@/api/remix';
import { planMediaCandidates } from '@/features/canvas/remix/productionPlanModel';
import type { PlanPlacementV2, PlanSpeechTakeV2, ProductionPlanV2 } from '@/features/canvas/remix/productionPlanV2Model';
import { bindProductionCanvasCandidate } from '@/features/canvas/remix/productionCanvasCommand';
import { flushProductionPlanV2AutoSave, productionPlanUpstreamRefs, scheduleProductionPlanV2AutoSave } from '@/features/canvas/remix/productionPlanV2AutoSave';
import { upgradeMigratedPlanAnchors } from '@/features/canvas/remix/productionPlanV2Upgrade';
import { acceptRemoteDifference, planDifferences } from '@/features/canvas/remix/productionPlanV2Conflict';
import { applyProductionPlanHistory, productionPlanHistoryEntry, type ProductionPlanHistoryEntry } from '@/features/canvas/remix/productionPlanEditHistory';
import { cachedProductionReadiness, loadProductionPlanReadiness } from '@/features/canvas/remix/productionWorkflowRuntime';
import { submitProductionComposeRender } from './productionComposeRender';
import { canvasBindingIssues, movePlacement, projectProductionTimeline, resizePlacement, splitPlacement, type ProjectedPlacement } from './productionComposeModel';
import { ProductionTrackMedia } from './ProductionTrackMedia';
import { NativeGraphicOverlay } from './NativeGraphicOverlay';
import { defaultGraphicEffect, GRAPHIC_TEMPLATES, graphicTemplateLabel } from '@/features/canvas/remix/nativeGraphicEffects';

type Candidate = ReturnType<typeof planMediaCandidates>[number];
type TrackRole = PlanPlacementV2['role'];
const TRACKS: Array<{ id: string; label: string; kind: PlanPlacementV2['kind']; role: TrackRole; color: string }> = [
  { id: 'visual:primary', label: '主画面', kind: 'visual', role: 'primary', color: '#449bc4' },
  { id: 'visual:overlay', label: '叠加画面', kind: 'visual', role: 'overlay', color: '#a87be0' },
  { id: 'graphic:effect', label: '图形效果', kind: 'graphic', role: 'effect', color: '#8bb6d7' },
  { id: 'subtitles', label: '字幕', kind: 'subtitle', role: 'dialogue', color: '#d0b365' },
  { id: 'audio:speech', label: '人声', kind: 'audio', role: 'speech', color: '#6bbf9c' },
  { id: 'audio:narration', label: '旁白', kind: 'audio', role: 'narration', color: '#82c4a6' },
  { id: 'audio:bgm', label: '音乐', kind: 'audio', role: 'bgm', color: '#7696c9' },
  { id: 'audio:sfx', label: '音效', kind: 'audio', role: 'sfx', color: '#ca946e' },
];
const panel = 'border border-[var(--ui-border-soft)] bg-[var(--ui-surface-modal)]';
const input = 'min-w-0 rounded-lg border border-[var(--ui-border-soft)] bg-black/20 px-2 py-1.5 text-xs text-text-dark focus:border-primary focus:outline-none';
const button = 'rounded-lg border border-[var(--ui-border-soft)] px-2.5 py-1.5 text-xs text-text-dark hover:bg-white/[0.08] disabled:cursor-not-allowed disabled:opacity-40';
const mediaUrl = (project: string, path: string) => `/api/v1/projects/${encodeURIComponent(project)}/media/${path.split('/').map(encodeURIComponent).join('/')}`;
const seconds = (frames: number, plan: ProductionPlanV2) => frames * plan.output.fps_den / plan.output.fps_num;
const frameLabel = (frame: number, plan: ProductionPlanV2) => `${seconds(frame, plan).toFixed(2)}s`;
const refKey = (ref: RemixArtifactRef | null | undefined) => ref ? `${ref.artifactId}:${ref.revision}` : '';
const resourceFor = (plan: ProductionPlanV2, placement: PlanPlacementV2) => plan.resources.find((item) => item.id === placement.resource_id);

export interface ProductionComposeWorkspaceProps { project: string; canvasId: string; planNodeId: string; composeNodeId: string; onClose: () => void }

export function ProductionComposeWorkspace({ project, canvasId, planNodeId, composeNodeId, onClose }: ProductionComposeWorkspaceProps) {
  useViewerImmersiveBody(true);
  const nodes = useCanvasStore((state) => state.nodes);
  const edges = useCanvasStore((state) => state.edges);
  const updateNodeData = useCanvasStore((state) => state.updateNodeData);
  const planData = nodes.find((node) => node.id === planNodeId)?.data;
  const composeData = nodes.find((node) => node.id === composeNodeId)?.data;
  const plan = planData?.productionPlanV2Draft as ProductionPlanV2 | undefined;
  const savedRef = planData?.productionPlanV2Ref as RemixArtifactRef | undefined;
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [playhead, setPlayhead] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [zoom, setZoom] = useState(32);
  const [timelineHeight, setTimelineHeight] = useState(320);
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const [libraryKind, setLibraryKind] = useState<'all' | 'video' | 'image' | 'audio'>('all');
  const [libraryTab, setLibraryTab] = useState<'canvas' | 'versions' | 'operations'>('canvas');
  const [operations, setOperations] = useState<RemixOperation[]>([]);
  const [operationsError, setOperationsError] = useState<string | null>(null);
  const [projection, setProjection] = useState<ProductionPlanProjection | null>(null);
  const [projectionRef, setProjectionRef] = useState('');
  const [busy, setBusy] = useState<'checking' | 'exporting' | 'binding' | 'reviewing' | null>(null);
  const [reviewFrame, setReviewFrame] = useState<{ url: string; frame: number; revision: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [remotePlan, setRemotePlan] = useState<{ ref: RemixArtifactRef; draft: ProductionPlanV2 } | null>(null);
  const [history, setHistory] = useState<ProductionPlanHistoryEntry[]>([]);
  const [future, setFuture] = useState<ProductionPlanHistoryEntry[]>([]);
  const previewVideoRef = useRef<HTMLVideoElement | null>(null);
  const previewSurfaceRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<{ id: string; originX: number; startFrame: number; edge?: 'start' | 'end' } | null>(null);

  const timeline = useMemo(() => plan ? projectProductionTimeline(plan) : null, [plan]);
  useEffect(() => {
    if (!plan || planData?.productionPlanV2Conflict) return;
    const upgraded = upgradeMigratedPlanAnchors(plan);
    if (upgraded === plan) return;
    updateNodeData(planNodeId, { productionPlanV2Draft: upgraded, productionPlanV2Dirty: true });
    scheduleProductionPlanV2AutoSave(project, planNodeId);
  }, [plan, planData?.productionPlanV2Conflict, planNodeId, project, updateNodeData]);
  const placements = timeline?.placements ?? [];
  const selected = placements.find((item) => item.id === selectedId) ?? null;
  const candidates = useMemo(() => planMediaCandidates(project, nodes, planNodeId), [project, nodes, planNodeId]);
  const visibleCandidates = candidates.filter((item) => libraryKind === 'all' || item.kind === libraryKind);
  const duration = timeline?.durationFrames ?? 0;
  const durationSec = plan ? seconds(duration, plan) : 0;
  const fps = plan ? plan.output.fps_num / plan.output.fps_den : 30;
  const pxPerFrame = zoom / fps;
  const trackWidth = Math.max(650, duration * pxPerFrame + 80);
  const currentRefKey = refKey(savedRef);
  const currentProjection = projectionRef === currentRefKey && !planData?.productionPlanV2Dirty ? projection : null;
  const canvasIssues = plan ? canvasBindingIssues(plan, edges, planNodeId, candidates) : [];
  const issues = [...currentProjection?.issues ?? [], ...canvasIssues];
  const oldVideoUrl = typeof composeData?.resultVideoUrl === 'string' ? composeData.resultVideoUrl
    : typeof composeData?.remixPreviousVideoUrl === 'string' ? composeData.remixPreviousVideoUrl : null;
  const renderRef = composeData?.productionPlanRenderedRef as RemixArtifactRef | undefined;
  const currentVideo = Boolean(oldVideoUrl && savedRef && refKey(renderRef) === refKey(savedRef) && !planData?.productionPlanV2Dirty && !canvasIssues.length);
  const speechPending = useMemo(() => {
    const node = nodes.find((item) => item.id === planNodeId);
    const readiness = node ? cachedProductionReadiness(node) : null;
    return Boolean(readiness && (readiness.speech_cues_pending.length || readiness.speech_turns_pending.length));
  }, [nodes, planNodeId, planData?.productionPlanV2Ref, planData?.productionPlanReadiness]);
  const conflictDifferences = useMemo(() => plan && remotePlan ? planDifferences(plan, remotePlan.draft) : [], [plan, remotePlan]);
  useEffect(() => () => { if (reviewFrame) URL.revokeObjectURL?.(reviewFrame.url); }, [reviewFrame]);
  useEffect(() => { setReviewFrame(null); }, [playhead, currentRefKey, planData?.productionPlanV2Dirty]);
  const relevantOperations = operations.filter((operation) => operation.node_id === composeNodeId || operation.operation_id === composeData?.remixOperationId
    || operation.input_refs?.some((ref) => savedRef && ref.artifact_id === savedRef.artifactId && ref.revision === savedRef.revision));

  useEffect(() => {
    if (libraryTab !== 'operations') return;
    let cancelled = false;
    void listRemixOperations(project).then((result) => { if (!cancelled) { setOperations(result.items); setOperationsError(null); } })
      .catch((cause) => { if (!cancelled) setOperationsError(cause instanceof Error ? cause.message : String(cause)); });
    return () => { cancelled = true; };
  }, [libraryTab, project]);

  useEffect(() => {
    if (plan || !savedRef) return;
    let cancelled = false;
    void fetchRemixArtifact(project, savedRef).then((artifact) => {
      if (cancelled) return;
      if (artifact.schema !== 'production_plan.v2') throw new Error('作品方案版本不正确');
      const current = useCanvasStore.getState().nodes.find((node) => node.id === planNodeId)?.data;
      if (!current?.productionPlanV2Dirty && refKey(current?.productionPlanV2Ref as RemixArtifactRef) === refKey(savedRef)) {
        updateNodeData(planNodeId, { productionPlanV2Draft: artifact.payload });
      }
    }).catch((cause) => { if (!cancelled) setError(cause instanceof Error ? cause.message : String(cause)); });
    return () => { cancelled = true; };
  }, [project, planNodeId, plan, savedRef?.artifactId, savedRef?.revision, updateNodeData]);

  useEffect(() => {
    if (!planData?.productionPlanV2Conflict || !savedRef) { setRemotePlan(null); return; }
    let cancelled = false;
    void fetchLatestRemixArtifact(project, savedRef.artifactId).then((artifact) => {
      if (!cancelled && artifact.schema === 'production_plan.v2') {
        setRemotePlan({ ref: { artifactId: artifact.id, revision: artifact.revision }, draft: artifact.payload as unknown as ProductionPlanV2 });
      }
    }).catch((cause) => { if (!cancelled) setError(cause instanceof Error ? cause.message : String(cause)); });
    return () => { cancelled = true; };
  }, [project, savedRef?.artifactId, planData?.productionPlanV2Conflict]);

  useEffect(() => {
    if (!savedRef || planData?.productionPlanV2Dirty || planData?.productionPlanV2Conflict) return;
    if (projectionRef === refKey(savedRef)) return;
    let cancelled = false;
    void projectProductionPlan(project, savedRef).then((result) => {
      if (cancelled) return;
      setProjection(result); setProjectionRef(refKey(savedRef));
      // Cache the server's compile verdict so the Agent repeats it, not a guess.
      void loadProductionPlanReadiness(project, planNodeId, savedRef);
    }).catch((cause) => { if (!cancelled) setError(cause instanceof Error ? cause.message : String(cause)); });
    return () => { cancelled = true; };
  }, [project, currentRefKey, projectionRef, planData?.productionPlanV2Dirty, planData?.productionPlanV2Conflict]);

  useEffect(() => {
    if (!playing || !plan || duration <= 0 || currentVideo) return;
    let last = performance.now();
    const timer = window.setInterval(() => {
      const now = performance.now(); const elapsed = now - last; last = now;
      setPlayhead((current) => {
        const next = current + elapsed * fps / 1000;
        if (next >= duration) { setPlaying(false); return 0; }
        return next;
      });
    }, 40);
    return () => window.clearInterval(timer);
  }, [playing, fps, duration, plan, currentVideo]);

  useEffect(() => {
    if (!plan || currentVideo) return;
    for (const element of previewSurfaceRef.current?.querySelectorAll<HTMLVideoElement | HTMLAudioElement>('[data-placement-id]') ?? []) {
      const item = placements.find((row) => row.id === element.dataset.placementId);
      if (!item || item.startFrame == null) continue;
      const resource = plan.resources.find((row) => row.id === item.resource_id);
      const take = timeline?.speechTakes.find((row) => row.id === item.speech_take_id);
      const rate = take ? take.speed_num / take.speed_den : (item.speed_num ?? 1) / (item.speed_den ?? 1);
      const inSeconds = take ? take.source_in_us / 1_000_000
        + Math.max(0, item.startFrame - take.startFrame) / fps * rate : (item.source_in_us ?? 0) / 1_000_000;
      const sourceOffset = Math.max(0, playhead - item.startFrame) / fps * rate;
      const available = Math.max(0, (resource?.duration_us ?? 0) / 1_000_000 - inSeconds);
      const sourceSeconds = inSeconds + (item.loop && available > 0 ? sourceOffset % available : sourceOffset);
      element.playbackRate = rate;
      if (element instanceof HTMLAudioElement) element.volume = Math.max(0, Math.min(1, item.volume ?? 1));
      if (Number.isFinite(sourceSeconds) && Math.abs(element.currentTime - sourceSeconds) > 0.3) {
        try { element.currentTime = sourceSeconds; } catch { /* metadata may not be ready yet */ }
      }
      if (playing) void element.play().catch(() => undefined);
      else element.pause();
    }
    for (const element of previewSurfaceRef.current?.querySelectorAll<HTMLAudioElement>('[data-speech-take-id]') ?? []) {
      const take = timeline?.speechTakes.find((item) => item.id === element.dataset.speechTakeId);
      if (!take) continue;
      const withinTake = take.startFrame <= playhead && playhead < take.endFrame;
      const rate = take.speed_num / take.speed_den;
      const sourceSeconds = take.source_in_us / 1_000_000 + Math.max(0, playhead - take.startFrame) / fps * rate;
      element.playbackRate = rate;
      element.volume = Math.max(0, Math.min(1, take.volume ?? 1));
      if (Number.isFinite(sourceSeconds) && Math.abs(element.currentTime - sourceSeconds) > 0.3) {
        try { element.currentTime = sourceSeconds; } catch { /* metadata may not be ready yet */ }
      }
      if (playing && withinTake) void element.play().catch(() => undefined);
      else element.pause();
    }
  }, [plan, currentVideo, placements, timeline, playhead, fps, playing]);

  useEffect(() => {
    const video = previewVideoRef.current;
    if (!video || !currentVideo) return;
    if (playing) void video.play().catch(() => undefined);
    else video.pause();
  }, [playing, currentVideo]);

  const changePlan = useCallback((edit: (next: ProductionPlanV2) => ProductionPlanV2 | void) => {
    const data = useCanvasStore.getState().nodes.find((node) => node.id === planNodeId)?.data;
    const current = data?.productionPlanV2Draft as ProductionPlanV2 | undefined;
    if (!current) return;
    const next = structuredClone(current);
    const result = edit(next) ?? next;
    const entry = productionPlanHistoryEntry(current, result);
    if (!entry.changes.length) return;
    setHistory((items) => [...items.slice(-49), entry]); setFuture([]);
    updateNodeData(planNodeId, { productionPlanV2Draft: result, productionPlanV2Dirty: true, productionPlanV2SaveError: null });
    if (!data?.productionPlanV2Conflict) scheduleProductionPlanV2AutoSave(project, planNodeId);
    setProjection(null); setProjectionRef(''); setError(null);
  }, [project, planNodeId, updateNodeData]);

  const undo = () => {
    if (!plan || !history.length) return;
    const current = useCanvasStore.getState().nodes.find((node) => node.id === planNodeId)?.data.productionPlanV2Draft as ProductionPlanV2 | undefined;
    if (!current) return;
    try {
      const previous = applyProductionPlanHistory(current, history[history.length - 1], 'undo');
      setHistory(history.slice(0, -1)); setFuture((items) => [...items, history[history.length - 1]]);
      updateNodeData(planNodeId, { productionPlanV2Draft: previous, productionPlanV2Dirty: true });
      if (!planData?.productionPlanV2Conflict) scheduleProductionPlanV2AutoSave(project, planNodeId);
      setError(null);
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
  };
  const redo = () => {
    if (!plan || !future.length) return;
    const current = useCanvasStore.getState().nodes.find((node) => node.id === planNodeId)?.data.productionPlanV2Draft as ProductionPlanV2 | undefined;
    if (!current) return;
    try {
      const next = applyProductionPlanHistory(current, future[future.length - 1], 'redo');
      setFuture(future.slice(0, -1)); setHistory((items) => [...items, future[future.length - 1]]);
      updateNodeData(planNodeId, { productionPlanV2Draft: next, productionPlanV2Dirty: true });
      if (!planData?.productionPlanV2Conflict) scheduleProductionPlanV2AutoSave(project, planNodeId);
      setError(null);
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
  };

  const acceptRemotePlan = () => {
    if (!remotePlan) return;
    updateNodeData(planNodeId, { productionPlanV2Ref: remotePlan.ref, productionPlanV2Draft: remotePlan.draft,
      productionPlanV2Dirty: false, productionPlanV2Conflict: false, productionPlanV2SaveError: null });
    updateNodeData(composeNodeId, { productionPlanV2Ref: remotePlan.ref });
    setHistory([]); setFuture([]); setRemotePlan(null);
  };

  const branchLocalPlan = async () => {
    if (!plan || !remotePlan) return;
    setBusy('checking'); setError(null);
    try {
      const snapshot = structuredClone(plan);
      snapshot.provenance.branch_source_ref = { artifact_id: (savedRef ?? remotePlan.ref).artifactId,
        revision: (savedRef ?? remotePlan.ref).revision };
      const lineage = productionPlanUpstreamRefs(snapshot);
      const artifact = await createRemixArtifact(project, 'production_plan.v2', snapshot as unknown as Record<string, unknown>, [remotePlan.ref, ...lineage]);
      const nextRef = { artifactId: artifact.id, revision: artifact.revision };
      const current = useCanvasStore.getState().nodes.find((node) => node.id === planNodeId)?.data;
      const latest = current?.productionPlanV2Draft as ProductionPlanV2 | undefined;
      const changedDuringBranch = Boolean(latest && JSON.stringify(latest) !== JSON.stringify(snapshot));
      updateNodeData(planNodeId, { productionPlanV2Ref: nextRef, productionPlanV2Draft: changedDuringBranch ? latest : snapshot,
        productionPlanV2Dirty: changedDuringBranch,
        productionPlanV2Conflict: false, productionPlanV2SaveError: null });
      updateNodeData(composeNodeId, { productionPlanV2Ref: nextRef });
      if (changedDuringBranch) scheduleProductionPlanV2AutoSave(project, planNodeId);
      setRemotePlan(null);
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(null); }
  };

  const bindCandidate = async (candidate: Candidate, placementId: string) => {
    const latestPlan = useCanvasStore.getState().nodes.find((node) => node.id === planNodeId)?.data.productionPlanV2Draft as ProductionPlanV2 | undefined;
    const placement = latestPlan?.placements.find((item) => item.id === placementId);
    if (!placement || placement.kind === 'subtitle' || placement.kind === 'graphic') return;
    if (placement.kind === 'visual' && candidate.kind === 'audio' || placement.kind === 'audio' && candidate.kind === 'image') {
      setError('素材类型与该轨道不匹配'); return;
    }
    setBusy('binding'); setError(null);
    try {
      await bindProductionCanvasCandidate(project, canvasId, planNodeId, placementId, candidate.node.id);
      const store = useCanvasStore.getState();
      const current = store.nodes.find((node) => node.id === planNodeId)?.data;
      const pending = { ...((current?.productionPlanV2PendingBindings as Record<string, string> | undefined) ?? {}) };
      const pendingErrors = { ...((current?.productionPlanV2PendingErrors as Record<string, string> | undefined) ?? {}) };
      delete pending[placementId]; delete pendingErrors[placementId];
      store.updateNodeData(planNodeId, { productionPlanV2PendingBindings: pending, productionPlanV2PendingErrors: pendingErrors });
      setProjection(null); setProjectionRef('');
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(null); }
  };

  const addCandidate = (candidate: Candidate, target?: { kind: 'visual' | 'audio'; role: TrackRole; startFrame: number }) => {
    if (!plan) return;
    const kind = target?.kind ?? (candidate.kind === 'audio' ? 'audio' : 'visual');
    const role = target?.role ?? (kind === 'audio' ? 'sfx' : 'overlay');
    if (kind === 'visual' && candidate.kind === 'audio' || kind === 'audio' && candidate.kind === 'image') { setError('素材类型与目标轨道不匹配'); return; }
    const start = Math.min(target?.startFrame ?? Math.floor(playhead), Math.max(0, duration - 1));
    const maxFrames = candidate.media?.duration_us && candidate.kind !== 'image' ? Math.max(1, Math.round(candidate.media.duration_us / 1_000_000 * fps)) : Math.max(1, Math.round(3 * fps));
    const end = Math.min(duration, start + maxFrames);
    if (end <= start) { setError('请先在作品方案中添加有时长的段落'); return; }
    const id = crypto.randomUUID();
    changePlan((next) => { next.placements.push({ id, kind, role, track_id: `${kind}:${role}`, description: candidate.label,
      start: { kind: 'program_frame', frame: start }, end: { kind: 'program_frame', frame: end }, z_order: role === 'overlay' ? 1 : 0 }); });
    setSelectedId(id);
    void bindCandidate(candidate, id);
  };

  const addEmpty = (kind: PlanPlacementV2['kind'], role: TrackRole) => {
    if (!plan) return;
    const start = Math.min(Math.floor(playhead), Math.max(0, duration - 1));
    const end = Math.min(duration, start + Math.max(1, Math.round(3 * fps)));
    if (end <= start) return;
    const id = crypto.randomUUID();
    changePlan((next) => { next.placements.push({ id, kind, role, track_id: kind === 'subtitle' ? 'subtitles' : `${kind}:${role}`, description: '',
      ...(kind === 'subtitle' ? { text: '字幕' } : {}),
      ...(kind === 'graphic' ? { description: '问题卡片', graphic: defaultGraphicEffect() } : {}),
      start: { kind: 'program_frame', frame: start }, end: { kind: 'program_frame', frame: end }, z_order: role === 'overlay' ? 1 : 0 }); });
    setSelectedId(id);
  };

  const createMaterial = (mode: 'upload' | 'image' | 'video' | 'audio') => {
    const store = useCanvasStore.getState();
    const selectedPlacement = plan?.placements.find((item) => item.id === selectedId);
    const name = selectedPlacement?.description || '作品素材';
    const type = mode === 'upload' ? CANVAS_NODE_TYPES.upload : mode === 'image' ? CANVAS_NODE_TYPES.imageGen
      : mode === 'video' ? CANVAS_NODE_TYPES.video : CANVAS_NODE_TYPES.audio;
    const props = mode === 'image' ? { displayName: `生成画面 · ${name}`, prompt: name }
      : mode === 'video' ? { displayName: `生成视频 · ${name}`, prompt: name }
      : mode === 'audio' ? { displayName: `制作声音 · ${name}`, text: name }
      : { displayName: `上传素材 · ${name}` };
    const newId = store.addNode(type, store.findNodePosition(planNodeId, 420, 320), props);
    if (!store.addEdge(newId, planNodeId)) { setError('无法连接新素材到作品方案'); return; }
    if (selectedPlacement && selectedPlacement.kind !== 'subtitle' && selectedPlacement.kind !== 'graphic') {
      const current = store.nodes.find((node) => node.id === planNodeId)?.data;
      store.updateNodeData(planNodeId, { productionPlanV2PendingBindings: {
        ...((current?.productionPlanV2PendingBindings as Record<string, string> | undefined) ?? {}),
        [selectedPlacement.id]: newId,
      } });
    }
    store.setSelectedNode(newId); store.requestFocusNode(newId);
    onClose();
  };

  const check = async (forExport = false): Promise<{ saved: RemixArtifactRef; result: ProductionPlanProjection } | null> => {
    if (!forExport) setBusy('checking');
    setError(null);
    try {
      const saved = await flushProductionPlanV2AutoSave(project, planNodeId);
      if (!saved) throw new Error('作品方案尚未保存');
      const result = await projectProductionPlan(project, saved);
      setProjection(result); setProjectionRef(refKey(saved));
      await loadProductionPlanReadiness(project, planNodeId, saved);
      const store = useCanvasStore.getState();
      const latest = store.nodes.find((node) => node.id === planNodeId)?.data.productionPlanV2Draft as ProductionPlanV2 | undefined;
      const graphIssues = latest ? canvasBindingIssues(latest, store.edges, planNodeId, planMediaCandidates(project, store.nodes, planNodeId)) : [];
      if (!result.ready || graphIssues.length) throw new Error(`有 ${result.issues.length + graphIssues.length} 项需要处理，点击时间线中的缺项可定位`);
      return { saved, result };
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); return null; }
    finally { if (!forExport) setBusy(null); }
  };

  const exportVideo = async () => {
    setBusy('exporting'); setError(null);
    try {
      const result = await submitProductionComposeRender({ project, canvasId, planNodeId, composeNodeId });
      setProjection(result.projection); setProjectionRef(refKey(result.saved));
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(null); }
  };

  const reviewSavedFrame = async () => {
    if (!savedRef || !currentProjection?.ready || canvasIssues.length || planData?.productionPlanV2Dirty || planData?.productionPlanV2Conflict) return;
    const frame = Math.floor(playhead);
    const requestedRef = savedRef;
    setBusy('reviewing'); setPlaying(false); setError(null);
    try {
      const blob = await previewProductionFrame(project, requestedRef, frame);
      const current = useCanvasStore.getState().nodes.find((node) => node.id === planNodeId)?.data;
      if (current?.productionPlanV2Dirty || refKey(current?.productionPlanV2Ref as RemixArtifactRef) !== refKey(requestedRef)) return;
      setReviewFrame({ url: URL.createObjectURL(blob), frame, revision: requestedRef.revision });
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(null); }
  };

  const close = () => { void flushProductionPlanV2AutoSave(project, planNodeId).catch(() => undefined); onClose(); };
  const openPlanEntry = () => {
    updateNodeData(planNodeId, { productionPlanV2FocusPlacementId: selectedId, productionPlanV2FocusSelectionId: null, productionPlanOpenOnCreate: true });
    close();
  };
  const openPlanRange = (selectionId: string) => {
    updateNodeData(planNodeId, { productionPlanV2FocusSelectionId: selectionId, productionPlanV2FocusPlacementId: null, productionPlanOpenOnCreate: true });
    close();
  };
  const issuesFor = (id: string) => issues.filter((item) => item.placement_id === id);
  const activeVisuals = placements.filter((item) => item.kind === 'visual' && item.startFrame !== null && item.endFrame !== null && item.startFrame <= playhead && playhead < item.endFrame)
    .sort((left, right) => (left.z_order ?? 0) - (right.z_order ?? 0));
  const activeSubtitles = placements.filter((item) => item.kind === 'subtitle' && item.startFrame !== null && item.endFrame !== null && item.startFrame <= playhead && playhead < item.endFrame);
  const activeGraphics = placements.filter((item) => item.kind === 'graphic' && item.startFrame !== null && item.endFrame !== null && item.startFrame <= playhead && playhead < item.endFrame);
  const previewStyle: CSSProperties = { aspectRatio: `${plan?.output.width ?? 16}/${plan?.output.height ?? 9}`, background: plan?.output.background ?? '#000000', height: '100%', maxWidth: reviewFrame ? '48%' : '100%', minWidth: 120 };
  const activeAudio = placements.filter((item) => item.kind === 'audio' && item.startFrame !== null && item.endFrame !== null && item.startFrame <= playhead && playhead < item.endFrame);
  const toggleTrack = (id: string) => setCollapsed((current) => { const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  const dragEnd = (event: ReactPointerEvent<HTMLElement>) => {
    const drag = dragRef.current; dragRef.current = null;
    if (!drag || !plan) return;
    const delta = Math.round((event.clientX - drag.originX) / pxPerFrame);
    if (!delta) return;
    changePlan((next) => drag.edge ? resizePlacement(next, drag.id, drag.edge, delta) : movePlacement(next, drag.id, delta));
  };
  const previewGesture = useRef<{ id: string; x: number; y: number; originX: number; originY: number; originW: number; originH: number; resize: boolean } | null>(null);
  const previewPointerUp = (event: ReactPointerEvent<HTMLDivElement>) => {
    const gesture = previewGesture.current; previewGesture.current = null;
    if (!gesture || !plan) return;
    const box = event.currentTarget.getBoundingClientRect();
    const dx = Math.round((event.clientX - gesture.x) * plan.output.width / box.width);
    const dy = Math.round((event.clientY - gesture.y) * plan.output.height / box.height);
    changePlan((next) => { const item = next.placements.find((row) => row.id === gesture.id); if (!item) return;
      if (item.kind === 'subtitle') {
        if (gesture.resize) item.font_size = Math.max(1, Math.min(plan.output.height, gesture.originW + dx));
        else { item.subtitle_x = Math.max(0, Math.min(plan.output.width, gesture.originX + dx)); item.subtitle_y = Math.max(0, Math.min(plan.output.height, gesture.originY + dy)); }
      } else if (gesture.resize) { item.width = Math.max(1, gesture.originW + dx); item.height = Math.max(1, gesture.originH + dy); }
      else { item.x = Math.max(0, gesture.originX + dx); item.y = Math.max(0, gesture.originY + dy); }
    });
  };

  return createPortal(<div role="dialog" aria-label="视频合成" aria-modal="true" className="nodrag nowheel fixed inset-0 z-[1000] flex flex-col overflow-hidden bg-background text-text-dark" onPointerUp={dragEnd}>
    <header className="flex h-14 shrink-0 items-center gap-3 border-b border-[var(--ui-border-soft)] px-4">
      <Film className="h-4 w-4 text-primary" /><strong className="text-sm">视频合成</strong><span className="truncate text-xs text-text-muted">{plan?.goal ?? '正在读取作品方案'}</span>
      {plan && <span className="rounded-full border border-[var(--ui-border-soft)] px-2 py-0.5 text-[11px] text-text-muted">{plan.output.width} × {plan.output.height} · {fps.toFixed(2)} fps · {durationSec.toFixed(1)} 秒</span>}
      <div className="ml-auto flex items-center gap-2"><button className={button} title="撤销" disabled={!history.length} onClick={undo}><Undo2 className="h-4 w-4" /></button><button className={button} title="重做" disabled={!future.length} onClick={redo}><Redo2 className="h-4 w-4" /></button>
      <button className={button} onClick={openPlanEntry}>编辑作品方案</button>
      <button className={button} disabled={Boolean(busy)} onClick={() => void check()}><RefreshCcw className="mr-1 inline h-3.5 w-3.5" />检查素材</button>
      <button className={button} disabled={Boolean(busy) || !savedRef || !currentProjection?.ready || Boolean(canvasIssues.length) || Boolean(planData?.productionPlanV2Dirty || planData?.productionPlanV2Conflict)} onClick={() => void reviewSavedFrame()}>核对导出画面</button>
      <button className="rounded-full bg-primary px-4 py-1.5 text-xs font-semibold text-primary-foreground disabled:opacity-40" disabled={Boolean(busy) || Boolean(planData?.productionPlanV2Conflict)} onClick={() => void exportVideo()}>{busy === 'exporting' ? '正在提交…' : '导出 MP4'}</button>
      <button className={button} aria-label="关闭视频合成" onClick={close}><X className="h-4 w-4" /></button></div>
    </header>
    <div className="flex min-h-0 flex-1">
      <aside className={`flex w-[244px] shrink-0 flex-col ${panel}`}><div className="flex gap-1 border-b border-[var(--ui-border-soft)] p-2">{([['canvas', '画布素材'], ['versions', '素材版本'], ['operations', '任务记录']] as const).map(([tab, label]) => <button key={tab} className={`${button} ${libraryTab === tab ? 'border-primary text-primary' : ''}`} onClick={() => setLibraryTab(tab)}>{label}</button>)}</div>
        {libraryTab === 'canvas' && <><div className="border-b border-[var(--ui-border-soft)] p-3"><div className="flex flex-wrap gap-1">{(['all', 'video', 'image', 'audio'] as const).map((kind) => <button key={kind} className={`${button} ${libraryKind === kind ? 'border-primary text-primary' : ''}`} onClick={() => setLibraryKind(kind)}>{({ all: '全部', video: '视频', image: '图片', audio: '声音' })[kind]}</button>)}</div></div>
        <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-3">{visibleCandidates.map((candidate) => <div key={candidate.node.id} draggable onDragStart={(event) => event.dataTransfer.setData('application/x-production-candidate-node', candidate.node.id)} className="rounded-xl border border-[var(--ui-border-soft)] bg-white/[0.025] p-2"><div className="flex items-start gap-2"><div className="flex h-12 w-12 shrink-0 items-center justify-center overflow-hidden rounded-lg bg-black/30">{candidate.kind === 'image' ? <img src={String(candidate.node.data.imageUrl ?? candidate.node.data.previewImageUrl)} alt="" className="h-full w-full object-cover" /> : candidate.kind === 'video' ? <Film className="h-4 w-4" /> : <Music2 className="h-4 w-4" />}</div><div className="min-w-0"><div className="truncate text-xs">{candidate.label}</div><div className={`text-[10px] ${candidate.media ? 'text-emerald-300' : 'text-amber-300'}`}>{candidate.media ? '素材版本已绑定' : candidate.currentPath ? '可绑定' : '待准备'}</div></div></div><div className="mt-2 flex gap-1"><button className={button} disabled={!selected || selected.kind === 'subtitle' || selected.kind === 'graphic' || Boolean(busy)} onClick={() => selected && void bindCandidate(candidate, selected.id)}>绑定所选</button><button className={button} disabled={!plan || Boolean(busy)} onClick={() => addCandidate(candidate)}>加入轨道</button></div></div>)}{!visibleCandidates.length && <p className="text-xs text-text-muted">画布中还没有这类素材。</p>}</div>
        <div className="space-y-2 border-t border-[var(--ui-border-soft)] p-3"><div className="text-[11px] text-text-muted">在画布创建素材节点</div><div className="grid grid-cols-2 gap-1"><button className={button} onClick={() => createMaterial('upload')}>上传</button><button className={button} onClick={() => createMaterial('image')}>生成图片</button><button className={button} onClick={() => createMaterial('video')}>生成视频</button><button className={button} onClick={() => createMaterial('audio')}>制作声音</button></div></div></>}
        {libraryTab === 'versions' && <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-3">{plan?.resources.map((resource) => { const sourceNode = nodes.find((node) => node.id === resource.node_id); const linked = placements.filter((item) => item.resource_id === resource.id); return <div key={resource.id} className="rounded-lg border border-[var(--ui-border-soft)] p-2 text-xs"><div className="truncate font-medium">{String(sourceNode?.data.displayName ?? sourceNode?.data.name ?? resource.node_id)}</div><div className="mt-1 text-text-muted">{resource.media_kind} · 节点版本 {resource.node_revision ?? '未记录'}</div><div className="truncate text-text-muted" title={resource.media_path}>{resource.media_path}</div><div className="truncate font-mono text-[10px] text-text-muted" title={resource.media_hash}>SHA-256 {resource.media_hash.slice(0, 12)}…</div><div className="mt-2 flex flex-wrap gap-1">{linked.map((item) => <button key={item.id} className={button} onClick={() => setSelectedId(item.id)}>{item.description || item.role}</button>)}</div></div>; })}{!plan?.resources.length && <p className="text-xs text-text-muted">尚未绑定素材版本。</p>}</div>}
        {libraryTab === 'operations' && <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-3">{operationsError && <p role="alert" className="text-xs text-red-300">{operationsError}</p>}{relevantOperations.map((operation) => <div key={operation.operation_id} className="rounded-lg border border-[var(--ui-border-soft)] p-2 text-xs"><div className="truncate font-mono" title={operation.operation_id}>{operation.operation_id}</div><div className="mt-1 text-text-muted">{operation.task_type ?? '合成任务'} · {operation.status}</div>{operation.output_urls?.[0]?.download_url && <a className="mt-2 block text-primary underline" href={operation.output_urls[0].download_url}>下载产物</a>}</div>)}{!operationsError && !relevantOperations.length && <p className="text-xs text-text-muted">当前方案还没有相关任务。</p>}</div>}
      </aside>
      <main className="flex min-w-0 flex-1 flex-col items-center justify-center gap-3 bg-black/35 p-4"><div className="text-[11px] text-text-muted">{currentVideo ? '当前版本成片' : oldVideoUrl ? '旧版成片 · 方案已变化' : '轨道预览 · 正式导出按保存版本合成'}</div><div className="flex min-h-0 w-full flex-1 items-center justify-center gap-3"><div ref={previewSurfaceRef} className="relative flex max-w-full items-center justify-center overflow-hidden rounded-xl shadow-2xl" style={previewStyle} onPointerUp={previewPointerUp}>
        {currentVideo && oldVideoUrl ? <video ref={previewVideoRef} src={oldVideoUrl} onTimeUpdate={(event) => setPlayhead(event.currentTarget.currentTime * fps)} onEnded={() => setPlaying(false)} className="h-full w-full object-contain" /> : <>
          {activeVisuals.map((item) => { const resource = plan ? resourceFor(plan, item) : undefined; const source = resource && mediaUrl(project, resource.media_path); const position: CSSProperties = { position: 'absolute', left: `${(item.x ?? 0) / (plan?.output.width ?? 1) * 100}%`, top: `${(item.y ?? 0) / (plan?.output.height ?? 1) * 100}%`, width: `${(item.width ?? plan?.output.width ?? 1) / (plan?.output.width ?? 1) * 100}%`, height: `${(item.height ?? plan?.output.height ?? 1) / (plan?.output.height ?? 1) * 100}%`, zIndex: item.z_order ?? 0, opacity: item.opacity ?? 1 };
            return <div key={item.id} className={`overflow-hidden ${selectedId === item.id ? 'outline outline-2 outline-primary' : ''}`} style={position} onPointerDown={(event) => { event.stopPropagation(); setSelectedId(item.id); previewGesture.current = { id: item.id, x: event.clientX, y: event.clientY, originX: item.x ?? 0, originY: item.y ?? 0, originW: item.width ?? plan?.output.width ?? 1, originH: item.height ?? plan?.output.height ?? 1, resize: (event.target as HTMLElement).dataset.resize === 'true' }; event.currentTarget.setPointerCapture(event.pointerId); }}>
              {source ? resource?.media_kind === 'image' ? <img src={source} alt={item.description} className="h-full w-full" style={{ objectFit: item.fit === 'stretch' ? 'fill' : item.fit ?? 'cover' }} /> : <video data-placement-id={item.id} src={source} muted playsInline className="h-full w-full" style={{ objectFit: item.fit === 'stretch' ? 'fill' : item.fit ?? 'cover' }} /> : <div className="flex h-full w-full items-center justify-center bg-white/10 p-2 text-center text-xs">{item.description || '待绑定画面'}</div>}
              {selectedId === item.id && <span data-resize="true" className="absolute bottom-0 right-0 h-3 w-3 cursor-nwse-resize bg-primary" />}
            </div>; })}
          {activeGraphics.map((item) => <NativeGraphicOverlay key={item.id} placement={item} frame={playhead}
            width={plan?.output.width ?? 1} height={plan?.output.height ?? 1}
            previewHeight={previewSurfaceRef.current?.clientHeight || 360}
            selected={selectedId === item.id} onSelect={() => setSelectedId(item.id)} />)}
          {activeSubtitles.map((item) => {
            const outputWidth = plan?.output.width ?? 1;
            const outputHeight = plan?.output.height ?? 1;
            const positioned = item.subtitle_x != null && item.subtitle_y != null;
            const style: CSSProperties = positioned
              ? { left: `${item.subtitle_x! / outputWidth * 100}%`, top: `${item.subtitle_y! / outputHeight * 100}%`, transform: 'translate(-50%, -100%)', maxWidth: '90%' }
              : { left: '5%', right: '5%', bottom: '7%' };
            style.fontSize = `${(item.font_size ?? Math.round(outputHeight * 0.052)) * (previewSurfaceRef.current?.clientHeight || 360) / outputHeight}px`;
            return <div key={item.id} className={`absolute z-50 cursor-move text-center font-semibold text-white [text-shadow:0_2px_4px_#000,0_0_3px_#000] ${selectedId === item.id ? 'outline outline-2 outline-primary' : ''}`} style={style}
              onPointerDown={(event) => { event.stopPropagation(); setSelectedId(item.id); previewGesture.current = { id: item.id, x: event.clientX, y: event.clientY,
                originX: item.subtitle_x ?? Math.round(outputWidth / 2), originY: item.subtitle_y ?? Math.round(outputHeight * 0.93),
                originW: item.font_size ?? Math.round(outputHeight * 0.052), originH: 0, resize: (event.target as HTMLElement).dataset.resize === 'true' };
                event.currentTarget.setPointerCapture(event.pointerId); }}>
              {item.text || plan?.beats.flatMap((beat) => beat.turns).find((turn) => turn.id === item.turn_id)?.text}
              {selectedId === item.id && <span data-resize="true" className="absolute bottom-0 right-0 h-3 w-3 cursor-nwse-resize bg-primary" />}
            </div>;
          })}
          {timeline?.speechTakes.map((take) => { const resource = plan?.resources.find((item) => item.id === take.resource_id); return resource ? <audio key={take.id} data-speech-take-id={take.id} src={mediaUrl(project, resource.media_path)} preload="metadata" style={{ display: 'none' }} /> : null; })}
          {activeAudio.filter((item) => !plan?.speech_takes?.some((take) => take.origin_placement_id === item.id || item.speech_take_id === take.id)).map((item) => { const resource = plan && resourceFor(plan, item); return resource ? <audio key={item.id} data-placement-id={item.id} src={mediaUrl(project, resource.media_path)} loop={item.loop} preload="metadata" style={{ display: 'none' }} /> : null; })}
          {!activeVisuals.length && !activeGraphics.length && <span className="text-xs text-text-muted">此处没有画面 · 添加或绑定素材后可预览</span>}
        </>}
      </div>{reviewFrame && <div className="flex h-full max-w-[48%] flex-col items-center justify-center gap-2"><img src={reviewFrame.url} alt="导出渲染帧" className="min-h-0 max-h-[90%] max-w-full rounded-xl object-contain shadow-2xl" /><span className="text-xs text-text-muted">方案版本 {reviewFrame.revision} · 第 {reviewFrame.frame} 帧 · 导出渲染</span></div>}</div><div className="flex w-full max-w-md items-center gap-3"><button className={button} aria-label={playing ? '暂停' : '播放'} onClick={() => setPlaying(!playing)}>{playing ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}</button><span className="font-mono text-xs text-text-muted">{plan ? frameLabel(Math.floor(playhead), plan) : '0s'}</span><input aria-label="播放位置" type="range" min="0" max={Math.max(1, duration - 1)} value={Math.floor(playhead)} onChange={(event) => { const frame = Number(event.target.value); setPlayhead(frame); if (currentVideo && previewVideoRef.current) previewVideoRef.current.currentTime = frame / fps; }} className="min-w-0 flex-1 accent-primary" /><span className="font-mono text-xs text-text-muted">{durationSec.toFixed(2)}s</span></div></main>
      <aside className={`w-[280px] shrink-0 overflow-y-auto p-4 ${panel}`}><div className="mb-3 flex items-center justify-between gap-2"><strong className="text-sm">{selected ? '条目属性' : '作品设置'}</strong>{selected && <button className={button} onClick={openPlanEntry}>在作品方案中查看</button>}</div>{plan && selected ? <Inspector plan={plan} placement={selected} issues={issuesFor(selected.id)} onChange={(edit) => changePlan((next) => { const row = next.placements.find((item) => item.id === selected.id); if (row) edit(row); })} onChangeTake={(takeId, edit) => changePlan((next) => { editPlanSpeechTakeSource(next, takeId, edit); })} onDelete={() => { changePlan((next) => { next.placements = next.placements.filter((item) => item.id !== selected.id); }); setSelectedId(null); }} onSplit={() => changePlan((next) => splitPlacement(next, selected.id, Math.round(playhead), crypto.randomUUID()))} /> : plan ? <div className="space-y-3 text-xs"><label className="block">目标<input className={`${input} mt-1 w-full`} value={plan.goal} onChange={(event) => changePlan((next) => { next.goal = event.target.value; })} /></label><div className="grid grid-cols-2 gap-2">{(['width', 'height', 'fps_num', 'fps_den'] as const).map((key) => <label key={key}>{({ width: '宽', height: '高', fps_num: '帧率分子', fps_den: '帧率分母' })[key]}<input className={`${input} mt-1 w-full`} type="number" min="1" value={plan.output[key]} onChange={(event) => changePlan((next) => { next.output[key] = Number(event.target.value); })} /></label>)}</div><p className="text-text-muted">尺寸和帧率由作品方案决定，导出不会自动裁剪时长。</p></div> : <p className="text-xs text-text-muted">读取方案中…</p>}
        {Boolean(planData?.productionPlanV2Conflict) && <div role="alert" className="mt-4 space-y-2 rounded-lg border border-red-400/40 p-3 text-xs text-red-200"><p>方案在另一处更新。当前修改保留在本地，自动保存已暂停；仍可继续调整。</p>{remotePlan ? <><p>远端版本 {remotePlan.ref.revision} · {remotePlan.draft.goal}</p><p>本地版本 {savedRef?.revision ?? '—'} · {plan?.goal}</p><p>远端 {remotePlan.draft.beats.length} 段、{remotePlan.draft.placements.length} 项；本地 {plan?.beats.length ?? 0} 段、{plan?.placements.length ?? 0} 项。</p><div className="max-h-44 space-y-1 overflow-y-auto rounded border border-white/10 p-2">{conflictDifferences.map((difference) => <div key={difference.label} className="border-b border-white/10 pb-1 last:border-0"><div className="flex items-center justify-between gap-2"><strong className="truncate">{difference.label}</strong><button className={button} disabled={Boolean(busy)} onClick={() => changePlan((current) => acceptRemoteDifference(current, remotePlan.draft, difference.path))}>此项采用远端</button></div><p className="truncate">本地：{difference.local}</p><p className="truncate">远端：{difference.remote}</p></div>)}{!conflictDifferences.length && <p>两份内容一致，可接受远端修订。</p>}</div><div className="flex flex-wrap gap-2"><button className={button} disabled={Boolean(busy)} onClick={acceptRemotePlan}>接受远端版本</button><button className={button} disabled={Boolean(busy)} onClick={() => void branchLocalPlan()}>保留本地为新方案</button></div></> : <p>正在读取远端版本…</p>}</div>}
        {speechPending && <div className="mt-4 rounded-lg border border-amber-400/30 p-3 text-xs text-amber-200">有台词还没有确认的发声时间。请先在作品方案里完成语音校准，再回到这里导出。</div>}
        {issues.length > 0 && <div className="mt-4 rounded-lg border border-amber-400/30 p-3 text-xs"><div className="mb-2 text-amber-200">待处理 {issues.length} 项</div>{issues.slice(0, 20).map((issue, index) => { const selectionId = 'selection_id' in issue && typeof issue.selection_id === 'string' ? issue.selection_id : null; const placementId = typeof issue.placement_id === 'string' ? issue.placement_id : null; return <button key={`${selectionId ?? placementId}:${index}`} className="block w-full truncate py-1 text-left text-text-muted hover:text-primary" onClick={() => selectionId ? openPlanRange(selectionId) : placementId && setSelectedId(placementId)}>{selectionId ? `范围 ${selectionId}` : placementId ?? '方案'} · {String(issue.message)}</button>; })}</div>}
        {Boolean(error || planData?.productionPlanV2SaveError) && <p role="alert" className="mt-4 break-words text-xs text-red-300">{error || String(planData?.productionPlanV2SaveError)}</p>}
        <p role="status" className="mt-4 text-[11px] text-text-muted">{planData?.productionPlanV2Conflict ? '版本冲突' : planData?.productionPlanV2Dirty ? '正在自动保存…' : `已保存方案版本 ${savedRef?.revision ?? '—'}`}</p>
      </aside>
    </div>
    <div className="h-1 shrink-0 cursor-ns-resize bg-white/[0.05] hover:bg-primary/30" onPointerDown={(event) => { event.currentTarget.setPointerCapture(event.pointerId); dragRef.current = null; const start = event.clientY; const height = timelineHeight; const target = event.currentTarget; const move = (moveEvent: globalThis.PointerEvent) => setTimelineHeight(Math.max(180, Math.min(650, height + start - moveEvent.clientY))); const up = () => { target.removeEventListener('pointermove', move); target.removeEventListener('pointerup', up); }; target.addEventListener('pointermove', move); target.addEventListener('pointerup', up); }} />
    <section className="flex shrink-0 flex-col border-t border-[var(--ui-border-soft)] bg-[var(--ui-surface-modal)]" style={{ height: timelineHeight }}><div className="flex h-10 shrink-0 items-center gap-2 border-b border-[var(--ui-border-soft)] px-3 text-xs"><strong>时间线</strong><button className={button} onClick={() => addEmpty('visual', 'primary')}><Plus className="mr-1 inline h-3 w-3" />主画面</button><button className={button} onClick={() => addEmpty('visual', 'overlay')}>叠加画面</button><button className={button} onClick={() => addEmpty('graphic', 'effect')}>图形效果</button><button className={button} onClick={() => addEmpty('subtitle', 'dialogue')}>字幕</button><button className={button} onClick={() => addEmpty('audio', 'speech')}>人声</button><button className={button} onClick={() => addEmpty('audio', 'bgm')}>音乐</button><button className={button} onClick={() => addEmpty('audio', 'sfx')}>音效</button><div className="ml-auto flex items-center gap-1">缩放<input aria-label="时间线缩放" type="range" min="12" max="120" value={zoom} onChange={(event) => setZoom(Number(event.target.value))} className="w-24 accent-primary" /></div></div>
      <div className="min-h-0 flex-1 overflow-auto"><div className="flex min-w-full" style={{ width: trackWidth + 145 }}><div className="sticky left-0 z-20 w-[145px] shrink-0 border-r border-[var(--ui-border-soft)] bg-[var(--ui-surface-modal)]"><div className="flex h-7 items-center px-3 text-[10px] text-text-muted">叙事 / 素材轨</div><div className="flex h-10 items-center px-3 text-[11px]">叙事段落</div><div className="flex h-7 items-center px-3 text-[11px]">台词锚点</div>{Boolean(plan?.speech_cues?.length) && <div className="flex h-7 items-center px-3 text-[11px]">SRT 台词</div>}<div className="flex h-7 items-center px-3 text-[11px]">词级定位</div><div className="flex h-7 items-center px-3 text-[11px]">命名范围</div>{TRACKS.map((track) => <button key={track.id} className="flex h-10 w-full items-center gap-1 px-3 text-left text-[11px] hover:bg-white/[0.05]" onClick={() => toggleTrack(track.id)}>{collapsed.has(track.id) ? <ChevronRight className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}{track.label}</button>)}</div>
      <div className="relative shrink-0" style={{ width: trackWidth }}><div className="relative h-7 border-b border-[var(--ui-border-soft)]" onClick={(event) => setPlayhead(Math.max(0, Math.min(duration - 1, Math.round((event.clientX - event.currentTarget.getBoundingClientRect().left) / pxPerFrame))))}>{Array.from({ length: Math.ceil(durationSec / Math.max(5, Math.ceil(durationSec / 120 / 5) * 5)) + 1 }, (_, index) => { const tickStep = Math.max(5, Math.ceil(durationSec / 120 / 5) * 5); return <span key={index} className="absolute top-1 border-l border-white/20 pl-1 font-mono text-[10px] text-text-muted" style={{ left: index * tickStep * zoom }}>{index * tickStep}s</span>; })}</div><div className="relative h-10 border-b border-[var(--ui-border-soft)]">{timeline?.beats.map((beat, index) => <div key={beat.id} className="absolute top-1 h-8 overflow-hidden rounded border border-primary/40 bg-primary/15 px-2 text-[10px] leading-7" style={{ left: beat.startFrame * pxPerFrame, width: (beat.endFrame - beat.startFrame) * pxPerFrame }}>{index + 1}. {beat.purpose}</div>)}</div><div className="relative h-7 border-b border-[var(--ui-border-soft)]">{timeline?.beats.flatMap((beat) => beat.turns).map((turn) => <div key={turn.id} title={`${turn.speaker}：${turn.text}${turn.estimated ? '（估算）' : ''}`} className={`absolute top-1 h-5 truncate rounded px-1 text-[10px] ${turn.estimated ? 'bg-white/10' : 'bg-white/20'}`} style={{ left: turn.startFrame * pxPerFrame, width: Math.max(8, (turn.endFrame - turn.startFrame) * pxPerFrame) }}>{turn.text}</div>)}</div>
      {Boolean(plan?.speech_cues?.length) && <div className="relative h-7 border-b border-[var(--ui-border-soft)]">{timeline?.speechCues.map((cue) => <button key={cue.id} aria-label={`${cue.text} SRT 台词`} title={`${cue.text}${cue.issue ? ` · ${cue.issue}` : ''}`} className={`absolute top-1 h-5 truncate rounded border px-1 text-left text-[10px] ${cue.issue ? 'border-amber-300/70 text-amber-200' : 'border-primary/50 bg-primary/20'}`} style={{ left: (cue.startFrame ?? 0) * pxPerFrame, width: Math.max(24, ((cue.endFrame ?? cue.startFrame ?? 0) - (cue.startFrame ?? 0)) * pxPerFrame) }} onClick={() => { if (cue.startFrame !== null) setPlayhead(cue.startFrame); }}>{cue.text}</button>)}</div>}<div className="relative h-7 border-b border-[var(--ui-border-soft)]">{timeline?.words.map((word) => <button key={word.id} title={`${word.text} · ${word.startFrame}–${word.endFrame} 帧`} className="absolute top-1 h-5 truncate rounded bg-white/15 px-1 text-left text-[10px]" style={{ left: word.startFrame * pxPerFrame, width: Math.max(8, (word.endFrame - word.startFrame) * pxPerFrame) }} onClick={() => setPlayhead(word.startFrame)}>{word.text}</button>)}</div>
      <div className="relative h-7 border-b border-[var(--ui-border-soft)]">{timeline?.selections.map((selection) => <button key={selection.id} title={`${selection.label}${selection.estimated ? ' · 时间估算' : ''}${selection.issue ? ` · ${selection.issue}` : ''}`} className={`absolute top-1 h-5 truncate rounded border px-1 text-left text-[10px] ${selection.issue ? 'border-amber-300/70 text-amber-200' : selection.estimated ? 'border-dashed border-primary/50 bg-primary/10' : 'border-primary/50 bg-primary/20'}`} style={{ left: (selection.startFrame ?? 0) * pxPerFrame, width: Math.max(30, ((selection.endFrame ?? selection.startFrame ?? 0) - (selection.startFrame ?? 0)) * pxPerFrame) }} onClick={() => selection.issue ? openPlanRange(selection.id) : setPlayhead(selection.startFrame ?? 0)}>{selection.label}</button>)}</div>
      {TRACKS.map((track) => <div key={track.id} className="relative h-10 border-b border-[var(--ui-border-soft)] bg-white/[0.015]" onDragOver={(event) => { if ((track.kind === 'visual' || track.kind === 'audio') && event.dataTransfer.types.includes('application/x-production-candidate-node')) event.preventDefault(); }} onDrop={(event) => { const nodeId = event.dataTransfer.getData('application/x-production-candidate-node'); const candidate = candidates.find((item) => item.node.id === nodeId); if (!candidate || track.kind === 'subtitle' || track.kind === 'graphic') return; event.preventDefault(); const frame = Math.max(0, Math.round((event.clientX - event.currentTarget.getBoundingClientRect().left) / pxPerFrame)); addCandidate(candidate, { kind: track.kind, role: track.role, startFrame: frame }); }}>{!collapsed.has(track.id) && placements.filter((item) => item.kind === track.kind && item.role === track.role).map((item) => { const left = (item.startFrame ?? 0) * pxPerFrame; const width = Math.max(22, ((item.endFrame ?? item.startFrame ?? 0) - (item.startFrame ?? 0)) * pxPerFrame); const problems = issuesFor(item.id); const missing = (item.kind === 'visual' || item.kind === 'audio') && !item.resource_id; const resource = plan ? resourceFor(plan, item) : undefined; const sourceInMs = (item.source_in_us ?? 0) / 1000; const sourceOutMs = item.sourceOutUs != null ? item.sourceOutUs / 1000 : item.source_out_us != null ? item.source_out_us / 1000 : sourceInMs + ((item.endFrame ?? 0) - (item.startFrame ?? 0)) / fps * 1000 * (item.speed_num ?? 1) / (item.speed_den ?? 1); return <div key={item.id} title={`${item.description || track.label}${item.issue ? ` · ${item.issue}` : problems.length ? ` · ${String(problems[0].message)}` : missing ? ' · 待绑定素材' : ''}`} className={`absolute top-1 flex h-8 cursor-grab items-center overflow-hidden rounded border px-2 text-[10px] ${selectedId === item.id ? 'ring-2 ring-white/75' : ''} ${missing || item.issue || problems.length ? 'border-dashed border-amber-300/70' : 'border-white/20'}`} style={{ left, width, backgroundColor: `${track.color}55` }} onClick={() => setSelectedId(item.id)} onPointerDown={(event) => { event.stopPropagation(); setSelectedId(item.id); dragRef.current = { id: item.id, originX: event.clientX, startFrame: item.startFrame ?? 0 }; event.currentTarget.setPointerCapture(event.pointerId); }}><span className="absolute left-0 top-0 z-10 h-full w-1 cursor-ew-resize" onPointerDown={(event) => { event.stopPropagation(); dragRef.current = { id: item.id, originX: event.clientX, startFrame: item.startFrame ?? 0, edge: 'start' }; }} />{resource && <ProductionTrackMedia kind={item.kind === 'audio' ? 'audio' : resource.media_kind} url={mediaUrl(project, resource.media_path)} sourceInMs={sourceInMs} sourceOutMs={sourceOutMs} width={width} durationMs={resource.duration_us / 1000} />}<span className="relative z-10 truncate rounded bg-black/55 px-1">{item.description || item.graphic?.text || item.text || (missing ? '待绑定素材' : track.label)}</span><span className="absolute right-0 top-0 z-10 h-full w-1 cursor-ew-resize" onPointerDown={(event) => { event.stopPropagation(); dragRef.current = { id: item.id, originX: event.clientX, startFrame: item.startFrame ?? 0, edge: 'end' }; }} /></div>; })}</div>)}<div className="pointer-events-none absolute bottom-0 top-0 z-10 w-px bg-primary" style={{ left: playhead * pxPerFrame }} /></div></div></div>
    </section>
  </div>, document.body);
}

function Inspector({ plan, placement, issues, onChange, onChangeTake, onDelete, onSplit }: { plan: ProductionPlanV2; placement: ProjectedPlacement; issues: Array<{ message?: unknown }>; onChange: (edit: (row: PlanPlacementV2) => void) => void; onChangeTake: (takeId: string, edit: (take: PlanSpeechTakeV2) => void) => void; onDelete: () => void; onSplit: () => void }) {
  const number = (label: string, value: number, update: (value: number) => void, min?: number, max?: number) => <label className="block text-[11px] text-text-muted">{label}<input className={`${input} mt-1 w-full`} type="number" min={min} max={max} value={value} onChange={(event) => update(Number(event.target.value))} /></label>;
  const roles = TRACKS.filter((track) => track.kind === placement.kind);
  const speechTake = plan.speech_takes?.find((take) => take.id === placement.speech_take_id || take.origin_placement_id === placement.id);
  const takeAudioOrigin = placement.kind === 'audio' && Boolean(speechTake);
  const speakingSource = Boolean(speechTake && speechTake.origin_placement_id === placement.id) || plan.beats.some((beat) => beat.timing_mode !== 'fixed' && beat.turns.some((turn) =>
    (turn.speech_placement_id ?? turn.speech_timing?.placement_id) === placement.id && turn.speech_timing?.placement_id === placement.id));
  return <div className="space-y-3 text-xs"><p className="truncate text-text-muted">{placement.kind} · {placement.role} · {placement.id}</p><label className="block">内容 / 用途<input className={`${input} mt-1 w-full`} value={placement.description ?? ''} onChange={(event) => onChange((row) => { row.description = event.target.value; })} /></label>
    {Boolean(plan.speech_cues?.length) && <label className="block">绑定台词<select aria-label="绑定台词" className={`${input} mt-1 w-full`} value={placement.start.kind === 'cue' && placement.end.kind === 'cue' && placement.start.ref_id === placement.end.ref_id ? placement.start.ref_id ?? '' : ''} onChange={(event) => { const id = event.target.value; if (!id) return; onChange((row) => { row.start = { kind: 'cue', ref_id: id, edge: 'start' }; row.end = { kind: 'cue', ref_id: id, edge: 'end' }; row.source_out_us = null; }); }}><option value="">未绑定 · 保留当前时间</option>{(plan.speech_cues ?? []).filter((cue) => cue.timing_status === 'ready').map((cue) => <option key={cue.id} value={cue.id}>{cue.text}</option>)}</select></label>}
    <label className="block">绑定命名范围<select aria-label="绑定命名范围" className={`${input} mt-1 w-full`} value={placement.start.kind === 'selection' && placement.end.kind === 'selection' && placement.start.ref_id === placement.end.ref_id ? placement.start.ref_id ?? '' : ''} onChange={(event) => { const id = event.target.value; if (!id) return; onChange((row) => { row.start = { kind: 'selection', ref_id: id, edge: 'start' }; row.end = { kind: 'selection', ref_id: id, edge: 'end' }; row.source_out_us = null; }); }}><option value="">未绑定 · 保留当前时间</option>{(plan.selections ?? []).map((selection) => <option key={selection.id} value={selection.id}>{selection.label}</option>)}</select></label>
    {roles.length > 1 && <label className="block">轨道 / 用途<select className={`${input} mt-1 w-full`} value={placement.role} onChange={(event) => onChange((row) => { const track = roles.find((item) => item.role === event.target.value); if (track) { row.role = track.role; row.track_id = track.id; } })}>{roles.map((track) => <option key={track.id} value={track.role}>{track.label}</option>)}</select></label>}
    <div className="grid grid-cols-2 gap-2">{number('开始帧偏移', placement.start.kind === 'program_frame' ? placement.start.frame ?? 0 : placement.start.offset_frames ?? 0, (value) => onChange((row) => { if (row.start.kind === 'program_frame') row.start.frame = Math.max(0, value); else row.start.offset_frames = value; }))}{!speakingSource && number('结束帧偏移', placement.end.kind === 'program_frame' ? placement.end.frame ?? 0 : placement.end.offset_frames ?? 0, (value) => onChange((row) => { if (row.end.kind === 'program_frame') row.end.frame = Math.max(0, value); else row.end.offset_frames = value; }))}</div>
    {speakingSource && <p className="text-[11px] text-primary">这段随发声素材：移动片段调整开场空白，拖边缘或修改源入出点可裁剪，后续段落会顺延。</p>}
    <p className="text-[11px] text-text-muted">{placement.start.kind === 'program_frame' ? '节目帧起点' : `起点锚定 ${placement.start.kind} · ${placement.start.ref_id}`} → {placement.end.kind === 'program_frame' ? '节目帧终点' : `终点锚定 ${placement.end.kind} · ${placement.end.ref_id}`}</p>
    {(placement.start.kind !== 'program_frame' || placement.end.kind !== 'program_frame') && <div className="flex gap-2">{placement.start.kind !== 'program_frame' && <button className={button} onClick={() => onChange((row) => { row.start = { kind: 'program_frame', frame: placement.startFrame ?? 0 }; })}>解除起点锚定</button>}{placement.end.kind !== 'program_frame' && <button className={button} onClick={() => onChange((row) => { row.end = { kind: 'program_frame', frame: placement.endFrame ?? 0 }; })}>解除终点锚定</button>}</div>}
    {(placement.kind === 'visual' || placement.kind === 'audio') && (speechTake ? <>
      <p className="text-[11px] text-primary">这段使用发声素材的源范围和速度；修改后台词需重新校准。</p>
      <div className="grid grid-cols-2 gap-2">
        {number('源入点（秒）', speechTake.source_in_us / 1_000_000, (value) => onChangeTake(speechTake.id, (take) => { take.source_in_us = Math.max(0, Math.round(value * 1_000_000)); }), 0)}
        {number('源出点（秒；0 为完整素材）', (speechTake.source_out_us ?? 0) / 1_000_000, (value) => onChangeTake(speechTake.id, (take) => { take.source_out_us = value > 0 ? Math.round(value * 1_000_000) : null; }), 0)}
        {number('速度分子', speechTake.speed_num, (value) => onChangeTake(speechTake.id, (take) => { take.speed_num = Math.max(1, Math.round(value)); }), 1)}
        {number('速度分母', speechTake.speed_den, (value) => onChangeTake(speechTake.id, (take) => { take.speed_den = Math.max(1, Math.round(value)); }), 1)}
      </div>
    </> : <>
      <div className="grid grid-cols-2 gap-2">{number('源入点（秒）', (placement.source_in_us ?? 0) / 1_000_000, (value) => onChange((row) => { row.source_in_us = Math.max(0, Math.round(value * 1_000_000)); }), 0)}{number('源出点（秒；0 为自动）', (placement.source_out_us ?? 0) / 1_000_000, (value) => onChange((row) => { row.source_out_us = value > 0 ? Math.round(value * 1_000_000) : null; }), 0)}{number('速度分子', placement.speed_num ?? 1, (value) => onChange((row) => { row.speed_num = Math.max(1, value); }), 1)}{number('速度分母', placement.speed_den ?? 1, (value) => onChange((row) => { row.speed_den = Math.max(1, value); }), 1)}</div>
      <label className="flex items-center gap-2"><input type="checkbox" checked={placement.loop ?? false} onChange={(event) => onChange((row) => { row.loop = event.target.checked; })} />循环素材</label>
      {!speakingSource && placement.source_out_us != null && placement.startFrame != null && <button className={button} onClick={() => onChange((row) => { const sourceSpan = (row.source_out_us ?? 0) - (row.source_in_us ?? 0); const frames = Math.round(sourceSpan / 1_000_000 * plan.output.fps_num / plan.output.fps_den * (row.speed_den ?? 1) / (row.speed_num ?? 1)); if (frames > 0) row.end = { kind: 'program_frame', frame: placement.startFrame! + frames }; })}>按源范围调整结束帧</button>}
    </>)}
    {placement.kind === 'graphic' && placement.graphic && <div className="space-y-2 rounded-lg border border-[var(--ui-border-soft)] p-2">
      <label className="block">模板<select aria-label="图形模板" className={`${input} mt-1 w-full`} value={placement.graphic.template} onChange={(event) => onChange((row) => { if (!row.graphic) return; row.graphic.template = event.target.value as NonNullable<PlanPlacementV2['graphic']>['template']; row.description = graphicTemplateLabel(row.graphic.template); })}>{GRAPHIC_TEMPLATES.map((template) => <option key={template.id} value={template.id}>{template.label}</option>)}</select></label>
      <label className="block">图形文字<input aria-label="图形文字" className={`${input} mt-1 w-full`} value={placement.graphic.text} onChange={(event) => onChange((row) => { if (!row.graphic) return; row.graphic.text = event.target.value; if (row.graphic.emphasis && !row.graphic.text.includes(row.graphic.emphasis)) row.graphic.emphasis = null; })} /></label>
      <label className="block">重点词<input aria-label="重点词" className={`${input} mt-1 w-full`} value={placement.graphic.emphasis ?? ''} onChange={(event) => onChange((row) => { if (row.graphic) row.graphic.emphasis = event.target.value || null; })} /></label>
      <label className="block">入场<select aria-label="图形入场" className={`${input} mt-1 w-full`} value={placement.graphic.enter ?? 'fade'} onChange={(event) => onChange((row) => { if (row.graphic) { row.graphic.enter = event.target.value as NonNullable<PlanPlacementV2['graphic']>['enter']; row.graphic.enter_frames = row.graphic.enter === 'none' ? 0 : Math.max(1, row.graphic.enter_frames ?? 6); } })}><option value="none">直接出现</option><option value="fade">淡入</option><option value="pop">缩放出现</option></select></label>
      <div className="grid grid-cols-2 gap-2">{number('入场帧数', placement.graphic.enter_frames ?? 6, (value) => onChange((row) => { if (row.graphic) row.graphic.enter_frames = Math.max(0, Math.min(120, Math.round(value))); }), 0, 120)}{number('退场帧数', placement.graphic.exit_frames ?? 6, (value) => onChange((row) => { if (row.graphic) row.graphic.exit_frames = Math.max(0, Math.min(120, Math.round(value))); }), 0, 120)}</div>
      <div className="grid grid-cols-3 gap-2">{(['foreground', 'background', 'accent'] as const).map((key) => <label key={key}>{({ foreground: '文字', background: '底色', accent: '强调' })[key]}<input type="color" aria-label={`${key} 颜色`} className="mt-1 block w-full" value={placement.graphic?.[key] ?? ({ foreground: '#FFFFFF', background: '#111827', accent: '#00BDCF' })[key]} onChange={(event) => onChange((row) => { if (row.graphic) row.graphic[key] = event.target.value; })} /></label>)}</div>
      <div className="grid grid-cols-2 gap-2">{number('中心 X', placement.graphic.x ?? Math.round(plan.output.width / 2), (value) => onChange((row) => { if (row.graphic) row.graphic.x = Math.max(0, Math.min(plan.output.width, Math.round(value))); }), 0, plan.output.width)}{number('中心 Y', placement.graphic.y ?? Math.round(plan.output.height * .25), (value) => onChange((row) => { if (row.graphic) row.graphic.y = Math.max(0, Math.min(plan.output.height, Math.round(value))); }), 0, plan.output.height)}</div>
      {number('字号', placement.graphic.font_size ?? Math.round(plan.output.height * .06), (value) => onChange((row) => { if (row.graphic) row.graphic.font_size = Math.max(1, Math.min(plan.output.height, Math.round(value))); }), 1, plan.output.height)}
      <p className="text-text-muted">内置模板直接渲染，无需绑定画面素材。精确效果可用「核对导出画面」查看。</p>
    </div>}
    {placement.kind === 'visual' && <><div className="grid grid-cols-2 gap-2">{number('X', placement.x ?? 0, (value) => onChange((row) => { row.x = Math.max(0, value); }), 0)}{number('Y', placement.y ?? 0, (value) => onChange((row) => { row.y = Math.max(0, value); }), 0)}{number('宽', placement.width ?? plan.output.width, (value) => onChange((row) => { row.width = Math.max(1, value); }), 1)}{number('高', placement.height ?? plan.output.height, (value) => onChange((row) => { row.height = Math.max(1, value); }), 1)}{number('层级', placement.z_order ?? 0, (value) => onChange((row) => { row.z_order = value; }))}{number('透明度', placement.opacity ?? 1, (value) => onChange((row) => { row.opacity = Math.max(0, Math.min(1, value)); }), 0, 1)}</div><label>填充方式<select className={`${input} mt-1 w-full`} value={placement.fit ?? 'cover'} onChange={(event) => onChange((row) => { row.fit = event.target.value as PlanPlacementV2['fit']; })}><option value="cover">铺满并裁切</option><option value="contain">完整显示</option><option value="stretch">拉伸</option></select></label></>}
    {placement.kind === 'audio' && (speechTake
      ? number('音量', speechTake.volume ?? 1, (value) => onChangeTake(speechTake.id, (take) => { take.volume = Math.max(0, Math.min(1, value)); }), 0, 1)
      : number('音量', placement.volume ?? 1, (value) => onChange((row) => { row.volume = Math.max(0, Math.min(1, value)); }), 0, 1))}
    {placement.kind === 'subtitle' && <><label className="block">字幕文字<textarea className={`${input} mt-1 w-full`} value={placement.text ?? ''} onChange={(event) => onChange((row) => { row.text = event.target.value; })} /></label><div className="grid grid-cols-3 gap-2">{number('中心 X', placement.subtitle_x ?? Math.round(plan.output.width / 2), (value) => onChange((row) => { row.subtitle_x = Math.max(0, Math.min(plan.output.width, value)); row.subtitle_y ??= Math.round(plan.output.height * 0.93); }), 0, plan.output.width)}{number('底边 Y', placement.subtitle_y ?? Math.round(plan.output.height * 0.93), (value) => onChange((row) => { row.subtitle_y = Math.max(0, Math.min(plan.output.height, value)); row.subtitle_x ??= Math.round(plan.output.width / 2); }), 0, plan.output.height)}{number('字号', placement.font_size ?? Math.round(plan.output.height * 0.052), (value) => onChange((row) => { row.font_size = Math.max(1, Math.min(plan.output.height, value)); }), 1, plan.output.height)}</div><button className={button} onClick={() => onChange((row) => { row.subtitle_x = null; row.subtitle_y = null; row.font_size = null; })}>恢复默认字幕位置与字号</button></>}
    {issues.map((issue, index) => <p key={index} className="text-amber-300">{String(issue.message)}</p>)}{placement.issue && <p className="text-amber-300">{placement.issue}</p>}
    <div className="flex gap-2"><button className={button} onClick={onSplit} disabled={takeAudioOrigin} title={takeAudioOrigin ? '发声素材是一条连续原声，不支持拆分' : undefined}><Scissors className="mr-1 inline h-3 w-3" />在播放头拆分</button><button className={button} onClick={onDelete} disabled={takeAudioOrigin} title={takeAudioOrigin ? '请到作品方案中替换或删除发声素材' : undefined}><Trash2 className="mr-1 inline h-3 w-3" />删除</button></div>
  </div>;
}
