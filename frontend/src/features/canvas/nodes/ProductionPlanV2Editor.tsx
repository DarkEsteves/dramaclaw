import { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { createRemixArtifact, fetchRemixArtifact, type RemixArtifactRef } from '@/api/remix';
import { getFreezoneWorkflowRun, listFreezoneWorkflowRuns, type FreezoneWorkflowRunAction } from '@/api/canvas';
import { useCanvasStore } from '@/stores/canvasStore';
import { CANVAS_NODE_TYPES } from '@/features/canvas/domain/canvasNodes';
import type { PlanAnchorV2, ProductionPlanV2 } from '@/features/canvas/remix/productionPlanV2Model';
import { planMediaCandidates } from '@/features/canvas/remix/productionPlanModel';
import { flushProductionPlanV2AutoSave, scheduleProductionPlanV2AutoSave } from '@/features/canvas/remix/productionPlanV2AutoSave';
import { inspectProductionSpeechReadiness, sharedSpeechTakeSourceBlocker, type PendingSpeechOperation, type SpeechReviewSnapshot } from '@/features/canvas/remix/productionSpeechReadiness';
import { bindProductionCanvasCandidate, executeProductionCanvasCommand, type ProductionCanvasCommand } from '@/features/canvas/remix/productionCanvasCommand';
import { upgradeMigratedPlanAnchors, upgradePlanSpeech } from '@/features/canvas/remix/productionPlanV2Upgrade';
import { acceptPlanSpeechTimings, countIndependentAudioSources } from '@/features/canvas/remix/productionPlanV2Edits';
import { applyAcceptedSpeechCues } from '@/features/canvas/remix/productionSpeechCorrection';
import { hashAuthorText } from '@/features/canvas/remix/authoredTextTiming';
import { ProductionScriptContent, selectedAuthorText } from '@/features/canvas/remix/ProductionScriptContent';
import { ProductionSelectionInspector } from '@/features/canvas/remix/ProductionSelectionInspector';
import type { ProductionTextRange } from '@/features/canvas/remix/productionTextSelection';
import { applyCanvasChatCommandsAsync, CANVAS_CHAT_COMMANDS_SCHEMA_VERSION, FREEZONE_WORKFLOW_RUN_UPDATED_EVENT } from '@/features/freezone/canvasChatCommands';
import { useTaskCenterStore } from '@/task-center/store';
import { ProductionPlanMediaPicker, type PlanMaterialMode } from './ProductionPlanMediaPicker';
import { ProductionPlanV2Materials } from './ProductionPlanV2Materials';
import { ProductionSpeechMainline } from './ProductionSpeechMainline';
import { ProductionSpeechTimingControls } from './ProductionSpeechTimingControls';
const field = 'tap-field w-full text-sm';
const isAuthoringAction = (action: string) => action === 'generate_production_plan_draft' || action === 'generate_production_plan_arrangement';
const draftActionInFlight = (runStatus: string, action: FreezoneWorkflowRunAction): boolean =>
 ['pending', 'running'].includes(action.status)
 && (runStatus === 'running' || (runStatus === 'interrupted' && Boolean(action.task_key)));
export function ProductionPlanV2Editor({ id, project, canvasId, data, onClose, onCompose }: { id: string; project: string | null | undefined; canvasId?: string | null; data: Record<string, unknown>; onClose: () => void; onCompose: () => void }) {
 const [draft, setDraft] = useState(data.productionPlanV2Draft as ProductionPlanV2 | undefined);
 const [view, setView] = useState<'materials' | 'speech'>('materials');
 const [error, setError] = useState<string | null>(null);
 const [pickerPlacementId, setPickerPlacementId] = useState<string | null>(null);
 const [binding, setBinding] = useState(false);
 const [drafting, setDrafting] = useState(false);
 const [activeDraftAction, setActiveDraftAction] = useState<FreezoneWorkflowRunAction | null>(null);
 const [activeDraftRunId, setActiveDraftRunId] = useState<string | null>(null);
 const locallyAcceptedRunId = useRef<string | null>(null);
 const [checkedDraftScope, setCheckedDraftScope] = useState<string | null>(null);
 const draftScope = `${project ?? ''}\u0000${canvasId ?? ''}\u0000${id}`;
 const runLookupPending = Boolean(project && canvasId && checkedDraftScope !== draftScope);
 const taskCenterTasks = useTaskCenterStore((state) => state.tasks);
 const [focusTurnId, setFocusTurnId] = useState<string | null>(null);
 const [selectedPlacementId, setSelectedPlacementId] = useState<string | null>(null);
 const mainRef = useRef<HTMLElement | null>(null);
 const focusedEntry = useRef(false);
 const ref = data.productionPlanV2Ref as RemixArtifactRef | undefined;
 const focusPlacementId = typeof data.productionPlanV2FocusPlacementId === 'string' ? data.productionPlanV2FocusPlacementId : null;
 const inspectedPlacementId = selectedPlacementId ?? focusPlacementId;
 const focusSelectionId = typeof data.productionPlanV2FocusSelectionId === 'string' ? data.productionPlanV2FocusSelectionId : null;
 const speechData = useCanvasStore.getState().nodes.find((node) => node.id === id)?.data ?? data;
 const candidates = project ? planMediaCandidates(project, useCanvasStore.getState().nodes, id) : [];
 const previewUrls: Record<string, string> = {};
 for (const placement of draft?.placements ?? []) {
  const resource = draft?.resources.find((item) => item.id === placement.resource_id);
  const candidate = candidates.find((item) => item.node.id === resource?.node_id);
  const nodeData = candidate?.node.data;
  const url = nodeData?.previewImageUrl ?? nodeData?.imageUrl ?? nodeData?.thumbnailUrl;
  if (typeof url === 'string' && url) previewUrls[placement.id] = url;
 }
 const mediaReady = (placement: ProductionPlanV2['placements'][number]): boolean => {
  const resource = draft?.resources.find((item) => item.id === placement.resource_id);
  const candidate = candidates.find((item) => item.node.id === resource?.node_id);
  return Boolean(resource && candidate?.media?.media_hash === resource.media_hash && candidate.media.media_path === resource.media_path);
 };
 const visualPlacements = draft?.placements.filter((placement) => placement.kind === 'visual') ?? [];
 const visualReady = visualPlacements.filter(mediaReady).length;
 const readyAudioIds = new Set((draft?.placements ?? []).filter((placement) => placement.kind === 'audio' && mediaReady(placement))
  .map((placement) => placement.resource_id).filter((value): value is string => Boolean(value)));
 const independentAudio = draft ? countIndependentAudioSources(draft, readyAudioIds) : { ready: 0, total: 0 };
 const unresolvedSpeech = draft?.beats.flatMap((beat) => beat.turns).filter((turn) => turn.text.trim() && turn.speech_placement_id === undefined).length ?? 0;
 const pickerPlacement = draft?.placements.find((placement) => placement.id === pickerPlacementId);
 const speechIssues = draft ? inspectProductionSpeechReadiness(draft,
  (speechData.productionSpeechReviews as Record<string, SpeechReviewSnapshot> | undefined) ?? {},
  (speechData.productionSpeechOperations as Record<string, PendingSpeechOperation> | undefined) ?? {}) : [];
 const activeDraftTask = activeDraftAction?.task_key ? taskCenterTasks.get(activeDraftAction.task_key) : undefined;
 const draftTaskFailed = activeDraftTask?.status === 'failed'
  || activeDraftAction?.status === 'failed' || activeDraftAction?.status === 'blocked';
 const draftTaskPending = Boolean(activeDraftAction && !draftTaskFailed &&
  (activeDraftAction.status === 'pending' || activeDraftAction.status === 'running'));
 const draftTaskError = draftTaskFailed ? (activeDraftTask?.error || activeDraftAction?.error || '起草任务失败') : null;

 useEffect(() => {
  if (!project || !canvasId) return;
  let live = true;
  const refresh = () => {
   void listFreezoneWorkflowRuns(project, canvasId).then(({ runs }) => {
    if (!live) return;
    const relevantRuns = [...runs].sort((a, b) => b.created_at.localeCompare(a.created_at))
     .filter((run) => run.actions.some((item) => item.node_id === id && isAuthoringAction(item.action)));
    const latestRun = (locallyAcceptedRunId.current
     ? relevantRuns.find((run) => run.run_id === locallyAcceptedRunId.current) : null)
     ?? relevantRuns.find((run) => run.actions.some((item) => item.node_id === id
      && isAuthoringAction(item.action) && draftActionInFlight(run.status, item)))
     ?? relevantRuns[0];
    const latestAction = latestRun?.actions.find((item) => item.node_id === id
     && isAuthoringAction(item.action));
    const action = latestAction && (
     draftActionInFlight(latestRun?.status ?? '', latestAction)
     || ['failed', 'blocked'].includes(latestAction.status)
    ) ? latestAction : null;
    if (action) {
     if (!draftActionInFlight(latestRun?.status ?? '', action)) locallyAcceptedRunId.current = null;
     setActiveDraftAction(action);
     setActiveDraftRunId(latestRun?.run_id ?? null);
    }
    else if (!locallyAcceptedRunId.current || runs.some((run) => run.run_id === locallyAcceptedRunId.current
      && run.status !== 'running')) {
     locallyAcceptedRunId.current = null;
     setActiveDraftAction(null);
     setActiveDraftRunId(null);
    }
    setCheckedDraftScope(`${project}\u0000${canvasId}\u0000${id}`);
   }).catch(() => {
    if (!live) return;
    setCheckedDraftScope(`${project}\u0000${canvasId}\u0000${id}`);
    setError('暂时无法核对已有起草任务，请重试');
   });
  };
  const onWorkflowUpdate = () => refresh();
  refresh();
  window.addEventListener(FREEZONE_WORKFLOW_RUN_UPDATED_EVENT, onWorkflowUpdate);
  return () => { live = false; window.removeEventListener(FREEZONE_WORKFLOW_RUN_UPDATED_EVENT, onWorkflowUpdate); };
 }, [project, canvasId, id]);

 useEffect(() => {
  if (!project || !canvasId || !activeDraftRunId || activeDraftAction?.task_key
   || !['pending', 'running'].includes(activeDraftAction?.status ?? '')) return;
  let cancelled = false;
  let timer: ReturnType<typeof setTimeout>;
  const reconcile = async () => {
   const run = await getFreezoneWorkflowRun(project, canvasId, activeDraftRunId, 10).catch(() => null);
   if (cancelled) return;
   const action = run?.actions.find((item) => item.node_id === id && isAuthoringAction(item.action));
   if (action) {
    setActiveDraftAction(action);
    if (['failed', 'blocked', 'completed'].includes(action.status)) locallyAcceptedRunId.current = null;
   }
   if (!action?.task_key && !['failed', 'blocked', 'completed'].includes(action?.status ?? '')) {
    timer = setTimeout(() => void reconcile(), 1500);
   }
  };
  timer = setTimeout(() => void reconcile(), 11_000);
  return () => { cancelled = true; clearTimeout(timer); };
 }, [project, canvasId, id, activeDraftRunId, activeDraftAction?.task_key, activeDraftAction?.status]);

 const actionableSpeechIssues = speechIssues.filter((issue) => issue.reason !== 'invalid_source');
 const unboundSpeechSources = speechIssues.filter((issue) => issue.reason === 'invalid_source').flatMap((issue) => {
  const sourceId = draft?.beats[issue.beatIndex]?.turns[issue.turnIndex]?.speech_placement_id;
  const placement = draft?.placements.find((row) => row.id === sourceId);
  return placement && !placement.resource_id ? [placement.id] : [];
 });
 const waitingForSpeech = actionableSpeechIssues.length > 0 && actionableSpeechIssues.every((issue) => issue.reason === 'running');
 const anchorOptions = useMemo(() => {
  if (!draft) return [] as Array<{ label: string; anchor: PlanAnchorV2 }>;
  const options: Array<{ label: string; anchor: PlanAnchorV2 }> = [];
  for (const beat of draft.beats) {
   for (const edge of ['start', 'end'] as const) options.push({ label: `段落「${beat.purpose}」${edge === 'start' ? '开始' : '结束'}`, anchor: { kind: 'beat', ref_id: beat.id, edge } });
   for (const turn of beat.turns) {
    for (const edge of ['start', 'end'] as const) options.push({ label: `台词「${turn.text}」${edge === 'start' ? '开始' : '结束'}`, anchor: { kind: 'turn', ref_id: turn.id, edge } });
    const sourceTiming = turn.speech_timing;
    const sourcePlacement = draft.placements.find((row) => row.id === sourceTiming?.placement_id);
    const sourceTake = draft.speech_takes?.find((row) => row.turn_ids.includes(turn.id) && row.origin_placement_id === sourceTiming?.placement_id);
    const sourceResource = draft.resources.find((row) => row.id === (sourceTake?.resource_id ?? sourcePlacement?.resource_id));
    const activeSourceWords = sourceTiming && sourceTiming.text === turn.text && sourceTiming.placement_id === (turn.speech_placement_id ?? sourceTiming.placement_id) && sourceTiming.media_hash === sourceResource?.media_hash ? sourceTiming.words : [];
    for (const word of [...(turn.timing_basis === 'aligned' ? turn.words ?? [] : []), ...activeSourceWords]) for (const edge of ['start', 'end'] as const) options.push({ label: `词「${word.text}」${edge === 'start' ? '开始' : '结束'}`, anchor: { kind: 'word', ref_id: word.id, edge } });
   }
  }
  return options;
 }, [draft]);
 useEffect(() => {
  if (!draft || (!focusPlacementId && !focusSelectionId) || focusedEntry.current) return;
  const entry = Array.from(mainRef.current?.querySelectorAll<HTMLElement>('[data-plan-placement-id], [data-plan-selection-id]') ?? [])
   .find((element) => element.dataset.planPlacementId === focusPlacementId || element.dataset.planSelectionId === focusSelectionId);
  if (entry) { entry.scrollIntoView?.({ block: 'center' }); focusedEntry.current = true; }
 }, [draft, focusPlacementId, focusSelectionId]);
 useEffect(() => {
  if (view !== 'speech' || !focusTurnId) return;
  const row = Array.from(mainRef.current?.querySelectorAll<HTMLElement>('[data-speech-turn-id]') ?? [])
   .find((element) => element.dataset.speechTurnId === focusTurnId);
  row?.scrollIntoView?.({ block: 'center' });
  const select = row?.querySelector<HTMLSelectElement>('select[aria-label$="发声素材"]');
  const button = row?.querySelector<HTMLButtonElement>('button[aria-label^="校准 "]');
  (select && !select.value ? select : button)?.focus();
  setFocusTurnId(null);
 }, [view, focusTurnId]);
 useEffect(() => { if (data.productionPlanV2Draft) setDraft(data.productionPlanV2Draft as ProductionPlanV2); }, [data.productionPlanV2Draft]);
 useEffect(() => {
  if (!draft || !project || !ref || data.productionPlanV2Conflict) return;
  let cancelled = false;
  const starting = draft;
  const evidenceCache = new Map<string, Promise<boolean>>();
  void upgradePlanSpeech(upgradeMigratedPlanAnchors(draft), (timing) => {
   const key = `${timing.evidence_ref.artifact_id}:${timing.evidence_ref.revision}`;
   if (!evidenceCache.has(key)) evidenceCache.set(key, fetchRemixArtifact(project, { artifactId: timing.evidence_ref.artifact_id, revision: timing.evidence_ref.revision }).then((artifact) => {
    const evidence = artifact.payload; const source = evidence.plan_ref as { artifact_id?: string; revision?: number } | undefined;
    return artifact.schema === 'word_timeline.v1' && (artifact as unknown as { producer_version?: string }).producer_version === 'remix-align-production-speech.v1'
     && source?.artifact_id === ref.artifactId && Number.isInteger(source.revision)
     && Boolean(artifact.upstream_refs?.some((row) => row.artifact_id === source.artifact_id && row.revision === source.revision));
   }).catch(() => false));
   return evidenceCache.get(key)!;
  }).then((upgraded) => {
   const current = useCanvasStore.getState().nodes.find((node) => node.id === id)?.data.productionPlanV2Draft;
   if (cancelled || upgraded === starting || current && current !== starting) return;
   setDraft(upgraded);
   useCanvasStore.getState().updateNodeData(id, { productionPlanV2Draft: upgraded, productionPlanV2Dirty: true });
   scheduleProductionPlanV2AutoSave(project, id);
  });
  return () => { cancelled = true; };
 }, [draft, project, id, data.productionPlanV2Conflict]);
 useEffect(() => {
  // Autosave already keeps an in-memory draft for the saved revision. Fetching
  // it again replaces the object during an in-flight SRT evidence transaction.
  if (!project || !ref || data.productionPlanV2Dirty || data.productionPlanV2Draft) return;
  let cancelled = false;
  void fetchRemixArtifact(project, ref).then((artifact) => {
   if (cancelled) return;
   const current = useCanvasStore.getState().nodes.find((node) => node.id === id)?.data;
   if (current?.productionPlanV2Dirty || (current?.productionPlanV2Ref as RemixArtifactRef)?.revision !== ref.revision) return;
   if (artifact.schema !== 'production_plan.v2') throw new Error('作品方案类型不匹配');
   setDraft(artifact.payload as unknown as ProductionPlanV2);
   useCanvasStore.getState().updateNodeData(id, { productionPlanV2Draft: artifact.payload });
  }).catch((cause) => setError(cause instanceof Error ? cause.message : String(cause)));
  return () => { cancelled = true; };
 }, [project, id, ref?.artifactId, ref?.revision]);
 const change = (edit: (next: ProductionPlanV2) => void) => {
  const nodeData = useCanvasStore.getState().nodes.find((node) => node.id === id)?.data;
  const current = nodeData?.productionPlanV2Draft as ProductionPlanV2 | undefined ?? draft;
  if (!current) return;
  const next = structuredClone(current); edit(next); setDraft(next);
  const placementIds = new Set(next.placements.map((placement) => placement.id));
  const pending = nodeData?.productionPlanV2PendingBindings as Record<string, string> | undefined;
  const pendingErrors = nodeData?.productionPlanV2PendingErrors as Record<string, string> | undefined;
  const liveEntries = (value: Record<string, string>) => Object.fromEntries(Object.entries(value).filter(([key]) => placementIds.has(key)));
  useCanvasStore.getState().updateNodeData(id, { productionPlanV2Draft: next, productionPlanV2Dirty: true,
   ...(pending && Object.keys(pending).some((key) => !placementIds.has(key)) ? { productionPlanV2PendingBindings: liveEntries(pending) } : {}),
   ...(pendingErrors && Object.keys(pendingErrors).some((key) => !placementIds.has(key)) ? { productionPlanV2PendingErrors: liveEntries(pendingErrors) } : {}),
  });
  if (project && next.goal.trim()) scheduleProductionPlanV2AutoSave(project, id);
 };
 const runNativeCommand = async (kind: ProductionCanvasCommand['command']['kind'], targetId: string, payload: Record<string, unknown>) => {
  if (!project || !canvasId) throw new Error('请先打开当前画布再修改声音来源');
  const saved = await flushProductionPlanV2AutoSave(project, id);
  const current = useCanvasStore.getState().nodes.find((node) => node.id === id)?.data;
  const latest = saved ?? current?.productionPlanV2Ref as RemixArtifactRef | undefined;
  if (!latest || current?.productionPlanV2Conflict) throw new Error('请先保存当前作品方案并处理版本冲突');
  const command: ProductionCanvasCommand = { type: 'production_plan_command', node_id: id,
   command: { command_id: crypto.randomUUID(), plan_ref: { artifact_id: latest.artifactId, revision: latest.revision }, kind, target_id: targetId, payload } };
  await executeProductionCanvasCommand(command, project, canvasId);
  const result = useCanvasStore.getState().nodes.find((node) => node.id === id)?.data.productionPlanV2Draft as ProductionPlanV2 | undefined;
  if (result) setDraft(result);
 };
 const editSpeechSource = async (turnId: string, placementId: string | null): Promise<boolean> => {
  setError(null);
  const latest = useCanvasStore.getState().nodes.find((node) => node.id === id)?.data.productionPlanV2Draft as ProductionPlanV2 | undefined ?? draft;
  const beat = latest?.beats.find((row) => row.turns.some((turn) => turn.id === turnId));
  const turn = beat?.turns.find((row) => row.id === turnId);
  if (!turn) { setError('找不到这句台词，请重新读取作品方案'); return false; }
  if (turn.speech_placement_id === placementId) return true;
  const sharedTake = latest?.speech_takes?.find((take) => take.turn_ids.length > 1 && take.turn_ids.includes(turnId));
  const peers = sharedTake ? beat!.turns.filter((row) => sharedTake.turn_ids.includes(row.id))
   : turn.speech_placement_id == null ? [turn]
    : beat!.turns.filter((row) => row.speech_placement_id === turn.speech_placement_id);
  const blocker = sharedTake && latest ? sharedSpeechTakeSourceBlocker(latest, sharedTake.id) : null;
  if (blocker) { setError(blocker); return false; }
  const edit = (row: typeof turn) => ({ kind: 'edit_turn' as const, target_id: row.id,
   payload: { speech_placement_id: placementId,
    ...(row.speech_timing || row.words?.length || row.timing_basis === 'aligned' ? { invalidate_speech_timing: true } : {}) } });
  try {
   if (sharedTake) await runNativeCommand('reassign_speech_source', beat!.id, { placement_id: placementId, turn_ids: sharedTake.turn_ids });
   else if (peers.length > 1) await runNativeCommand('batch', beat!.id, { commands: peers.map(edit) });
   else await runNativeCommand('edit_turn', turnId, edit(turn).payload);
   const savedData = useCanvasStore.getState().nodes.find((node) => node.id === id)?.data;
   const savedPlan = savedData?.productionPlanV2Draft as ProductionPlanV2 | undefined;
   if (!peers.every((row) => savedPlan?.beats.flatMap((item) => item.turns).find((item) => item.id === row.id)?.speech_placement_id === placementId)) {
    setError('声音来源尚未保存，请重新检查作品方案'); return false;
   }
   const reviews = { ...((savedData?.productionSpeechReviews as Record<string, unknown> | undefined) ?? {}) };
   for (const row of peers) delete reviews[row.id];
   useCanvasStore.getState().updateNodeData(id, { productionSpeechReviews: reviews });
   return true;
  } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); return false; }
 };
 const editVideoSound = async (placementId: string, useSound: boolean) => {
  const latest = useCanvasStore.getState().nodes.find((node) => node.id === id)?.data.productionPlanV2Draft as ProductionPlanV2 | undefined ?? draft;
  const placement = latest?.placements.find((row) => row.id === placementId);
  if (!placement) return;
  setError(null);
  try { await runNativeCommand('upsert_placement', placementId, { placement: { ...placement, source_audio: useSound } }); }
  catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
 };
 const textAnchors = async (range: ProductionTextRange, selectedText: string) => {
   const current = useCanvasStore.getState().nodes.find((node) => node.id === id)?.data.productionPlanV2Draft as ProductionPlanV2 | undefined;
   if (!current) throw new Error('作品方案已变化，请重新选取台词');
   if (selectedAuthorText(current, range) !== selectedText) throw new Error('选中的台词已变化，请重新选取');
   const turns = current.beats.flatMap((beat) => beat.turns);
   const endpoints = [range.start, range.end];
   const sourceTexts = endpoints.map((endpoint) => turns.find((turn) => turn.id === endpoint.turn_id)?.text);
   if (sourceTexts.some((text) => text === undefined)) throw new Error('选中的台词已变化，请重新选取');
   const hashes = await Promise.all(sourceTexts.map((text) => hashAuthorText(text!)));
   const assertCurrentRange = (next: ProductionPlanV2) => {
    const latest = next.beats.flatMap((beat) => beat.turns);
    if (selectedAuthorText(next, range) !== selectedText ||
        endpoints.some((endpoint, index) => latest.find((turn) => turn.id === endpoint.turn_id)?.text !== sourceTexts[index]))
     throw new Error('台词在保存范围时已变化，请重新选取');
   };
   const start: PlanAnchorV2 = { kind: 'text', ref_id: range.start.turn_id, char_offset: range.start.char_offset,
     text_hash: hashes[0], affinity: 'after' };
   const end: PlanAnchorV2 = { kind: 'text', ref_id: range.end.turn_id, char_offset: range.end.char_offset,
     text_hash: hashes[1], affinity: 'before' };
   return { start, end, assertCurrentRange };
 };
 const addSelectedMaterial = async (kind: 'visual' | 'audio' | 'subtitle', range: ProductionTextRange, selectedText: string) => {
  try {
   const { start, end, assertCurrentRange } = await textAnchors(range, selectedText);
   change((next) => {
    assertCurrentRange(next);
    const role = kind === 'visual' ? 'overlay' : kind === 'audio' ? 'sfx' : 'dialogue';
    next.placements.push({ id: crypto.randomUUID(), kind, role,
     track_id: kind === 'subtitle' ? 'subtitles' : `${kind}:${role}`,
     description: `关联台词：${selectedText.slice(0, 80)}`,
     ...(kind === 'subtitle' ? { text: selectedText } : {}), start, end });
   });
   setError(null);
  } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
 };
 const replaceSelectedMaterialRange = async (range: ProductionTextRange, selectedText: string) => {
  if (!inspectedPlacementId) return;
  try {
   const { start, end, assertCurrentRange } = await textAnchors(range, selectedText);
   change((next) => {
    assertCurrentRange(next);
    const placement = next.placements.find((item) => item.id === inspectedPlacementId);
    if (!placement) throw new Error('选中的素材已变化，请重新选择');
    placement.start = start; placement.end = end;
   });
   setError(null);
  } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
 };
 const close = () => { if (project) void flushProductionPlanV2AutoSave(project, id).catch(() => undefined); onClose(); };
 const generateDraft = async (action: 'generate_production_plan_draft' | 'generate_production_plan_arrangement' = 'generate_production_plan_draft') => {
  const arranging = action === 'generate_production_plan_arrangement';
  if (drafting || draftTaskPending || runLookupPending) return;
  const current = useCanvasStore.getState().nodes.find((node) => node.id === id)?.data;
  const plan = current?.productionPlanV2Draft as ProductionPlanV2 | undefined ?? draft;
  if (!plan?.goal.trim()) { setError('请先填写作品目标'); return; }
  if (!arranging && (plan.beats.length || plan.resources.length || plan.placements.length)) { setError('方案已有内容，请新建方案节点起草另一版'); return; }
  if (!project || !canvasId) { setError('请先打开画布'); return; }
  setDrafting(true); setError(null);
  try {
   const saved = await flushProductionPlanV2AutoSave(project, id);
   if (arranging && !saved) throw new Error('请先保存当前作品方案版本');
   const latest = useCanvasStore.getState().nodes.find((node) => node.id === id)?.data;
   const currentPlan = latest?.productionPlanV2Draft as ProductionPlanV2 | undefined;
   if (!currentPlan || currentPlan.goal !== plan.goal || !arranging && (currentPlan.beats.length || currentPlan.resources.length || currentPlan.placements.length))
    throw new Error('方案在起草前已变化，请重新检查');
   const existing = (await listFreezoneWorkflowRuns(project, canvasId)).runs
    .find((run) => run.actions.some((item) => item.node_id === id
     && isAuthoringAction(item.action) && draftActionInFlight(run.status, item)));
   if (existing) {
    setActiveDraftRunId(existing.run_id);
    setActiveDraftAction(existing.actions.find((item) => item.node_id === id
     && isAuthoringAction(item.action)) ?? null);
    return;
   }
   const result = await applyCanvasChatCommandsAsync([{ schema_version: CANVAS_CHAT_COMMANDS_SCHEMA_VERSION,
    project_id: project, canvas_id: canvasId, commands: [{ type: 'run_node_action', node_id: id,
     action, parameters: arranging ? { plan_ref: { artifact_id: saved!.artifactId, revision: saved!.revision } } : { topic: currentPlan.goal } }] }],
    { projectId: project, canvasId });
   const rejected = result.commandResults.find((step) => step.status === 'error');
   if (rejected) throw new Error(rejected.error || result.errors[0] || '提交起草任务失败');
   if (result.errors.length) throw new Error(result.errors[0]);
   const accepted = result.commandResults.find((step) => step.type === 'run_node_action' && step.status === 'pending');
   if (accepted) {
    locallyAcceptedRunId.current = typeof accepted.output?.workflow_run_id === 'string'
     ? accepted.output.workflow_run_id : null;
    setActiveDraftRunId(locallyAcceptedRunId.current);
    setActiveDraftAction({ node_id: id, action, status: 'pending' });
    const run = locallyAcceptedRunId.current
     ? await getFreezoneWorkflowRun(project, canvasId, locallyAcceptedRunId.current, 10).catch(() => null)
     : null;
    const acceptedAction = run?.actions.find((item) => item.node_id === id
     && item.action === action);
    if (acceptedAction) setActiveDraftAction(acceptedAction);
   }
  } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
  finally { setDrafting(false); }
 };
 const bindCandidate = async (nodeId: string) => {
  if (!project || !canvasId || !pickerPlacementId) return;
  setBinding(true); setError(null);
  try {
   await bindProductionCanvasCandidate(project, canvasId, id, pickerPlacementId, nodeId);
   const store = useCanvasStore.getState();
   const nodeData = store.nodes.find((node) => node.id === id)?.data;
   if (nodeData?.productionPlanV2Draft) setDraft(nodeData.productionPlanV2Draft as ProductionPlanV2);
   const pending = { ...((nodeData?.productionPlanV2PendingBindings as Record<string, string> | undefined) ?? {}) };
   const pendingErrors = { ...((nodeData?.productionPlanV2PendingErrors as Record<string, string> | undefined) ?? {}) };
   delete pending[pickerPlacementId]; delete pendingErrors[pickerPlacementId];
   store.updateNodeData(id, { productionPlanV2PendingBindings: pending, productionPlanV2PendingErrors: pendingErrors });
   setPickerPlacementId(null);
  } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
  finally { setBinding(false); }
 };
 const createMaterial = (mode: PlanMaterialMode) => {
  if (!pickerPlacement || !draft) return;
  try {
   const store = useCanvasStore.getState();
   const title = pickerPlacement.description?.trim() || '作品素材';
   const type = mode === 'upload' ? CANVAS_NODE_TYPES.upload : mode === 'image' ? CANVAS_NODE_TYPES.imageGen
    : mode === 'video' ? CANVAS_NODE_TYPES.video : CANVAS_NODE_TYPES.audio;
   const nodeData = mode === 'image' ? { displayName: `生成画面 · ${title}`, prompt: title }
    : mode === 'video' ? { displayName: `生成视频 · ${title}`, prompt: title }
     : mode === 'audio' ? { displayName: `制作声音 · ${title}`, text: title }
      : { displayName: `上传素材 · ${title}` };
   const nodeId = store.addNode(type, store.findNodePosition(id, 420, 320), nodeData);
   if (!store.addEdge(nodeId, id)) { store.deleteNodes([nodeId]); throw new Error('无法连接新素材到作品方案'); }
   const current = store.nodes.find((node) => node.id === id)?.data;
   store.updateNodeData(id, { productionPlanV2PendingBindings: {
    ...((current?.productionPlanV2PendingBindings as Record<string, string> | undefined) ?? {}), [pickerPlacement.id]: nodeId,
   } });
   store.setSelectedNode(nodeId); store.requestFocusNode(nodeId);
   setPickerPlacementId(null); close();
  } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
 };
 const continueToCompose = () => {
  const current = useCanvasStore.getState().nodes.find((node) => node.id === id)?.data ?? data;
  const latest = current.productionPlanV2Draft as ProductionPlanV2 | undefined ?? draft;
  if (!latest) return;
  const issues = inspectProductionSpeechReadiness(latest,
   (current.productionSpeechReviews as Record<string, SpeechReviewSnapshot> | undefined) ?? {},
   (current.productionSpeechOperations as Record<string, PendingSpeechOperation> | undefined) ?? {});
  const missing = issues.find((issue) => issue.reason === 'invalid_source' && latest.placements.some((placement) =>
   placement.id === latest.beats[issue.beatIndex]?.turns[issue.turnIndex]?.speech_placement_id && !placement.resource_id));
  if (missing) { setPickerPlacementId(latest.beats[missing.beatIndex].turns[missing.turnIndex].speech_placement_id ?? null); return; }
  const first = issues.find((issue) => issue.reason !== 'invalid_source' && issue.reason !== 'running')
   ?? issues.find((issue) => issue.reason === 'running');
  if (!first) { onCompose(); return; }
  setFocusTurnId(first.turnId);
  setView('speech');
 };
 return createPortal(<div role="dialog" aria-label="编辑作品方案" className="fixed inset-0 z-[1000] flex items-center justify-center bg-black/75 p-4" onClick={close}>
  <div className="nodrag nowheel relative flex max-h-[90vh] w-full max-w-5xl flex-col overflow-hidden rounded-2xl border border-[var(--ui-border-strong)] bg-[var(--ui-surface-modal)] text-text-dark" onClick={(event) => event.stopPropagation()}>
   <header className="flex items-center justify-between border-b border-[var(--ui-border-soft)] px-6 py-4"><div><h2 className="text-base font-semibold">作品方案</h2><p className="mt-1 text-xs text-text-muted">按内容安排画面、台词和声音；需要时展开语音校准。</p></div><button className="tap-button" aria-label="关闭" onClick={close}>关闭</button></header>
   <div className="flex flex-wrap items-center gap-5 border-b border-[var(--ui-border-soft)] px-6 py-3 text-xs text-text-muted">
    <span className="font-medium text-text-dark">素材准备</span>
    <span>{`画面 ${visualReady}/${visualPlacements.length}`}</span>
    <span>{`独立声音 ${independentAudio.ready}/${independentAudio.total}`}</span>
    <span>{`台词来源 ${unresolvedSpeech ? `${unresolvedSpeech} 句尚未选择` : '已选择'}`}</span>
    {speechIssues.length > 0 && <button type="button" className="ml-auto text-primary hover:underline" onClick={() => { setView('speech'); setFocusTurnId(speechIssues[0].turnId); }}>{`查看声音待处理 ${speechIssues.length} 句`}</button>}
   </div>
   <main ref={mainRef} className="min-h-0 space-y-5 overflow-y-auto p-6">{draft ? <>
    <>
     <div className="flex items-end gap-3"><label className="min-w-0 flex-1 text-sm">作品目标<input aria-label="作品目标" className={field} value={draft.goal} onChange={(event) => change((next) => { next.goal = event.target.value; })} /></label>
      {draft.beats.length === 0 && draft.resources.length === 0 && draft.placements.length === 0 && <button type="button" className="tap-button tap-button-quiet-primary shrink-0" disabled={drafting || runLookupPending || draftTaskPending || !draft.goal.trim()} onClick={() => void generateDraft()}>{drafting ? '正在提交…' : runLookupPending ? '正在核对任务…' : draftTaskPending ? (activeDraftTask?.status === 'completed' ? '正在同步方案…' : activeDraftAction?.task_key ? '正在起草…' : '任务提交中…') : 'AI 起草作品方案'}</button>}
      {draft.authoring_stage === 'outline' && draft.beats.length > 0 && <button type="button" className="tap-button tap-button-quiet-primary shrink-0" disabled={drafting || runLookupPending || draftTaskPending} onClick={() => void generateDraft('generate_production_plan_arrangement')}>{drafting ? '正在提交…' : draftTaskPending ? '正在安排画面与声音…' : 'AI 安排画面与声音'}</button>}</div>
     <details className="rounded-xl border border-[var(--ui-border-soft)] px-4 py-3"><summary className="cursor-pointer text-xs text-text-muted">成片规格 · {draft.output.width}×{draft.output.height} · {(draft.output.fps_num / draft.output.fps_den).toFixed(2)} fps</summary>
      <div className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-4">{(['width', 'height', 'fps_num', 'fps_den'] as const).map((key) => <label key={key} className="text-xs text-text-muted">{({ width: '宽度', height: '高度', fps_num: '帧率分子', fps_den: '帧率分母' })[key]}<input aria-label={key} className={field} type="number" min="1" value={draft.output[key]} onChange={(event) => change((next) => { next.output[key] = Number(event.target.value); })} /></label>)}</div>
      <label className="mt-3 block text-xs text-text-muted">背景<input aria-label="背景" className={field} value={draft.output.background ?? '#000000'} onChange={(event) => change((next) => { next.output.background = event.target.value; })} /></label>
     </details>
     {draft.beats.length > 0 && <ProductionScriptContent plan={draft} onAddRange={addSelectedMaterial}
      onReplaceRange={inspectedPlacementId ? replaceSelectedMaterialRange : undefined}
      previewUrls={previewUrls} onSelectPlacement={(placementId) => {
       setSelectedPlacementId(placementId);
       focusedEntry.current = false;
       useCanvasStore.getState().updateNodeData(id, { productionPlanV2FocusPlacementId: placementId });
       Array.from(mainRef.current?.querySelectorAll<HTMLElement>('[data-plan-placement-id]') ?? [])
        .find((row) => row.dataset.planPlacementId === placementId)?.scrollIntoView?.({ block: 'center' });
      }} />}
     {inspectedPlacementId && <ProductionSelectionInspector plan={draft} placementId={inspectedPlacementId}
      anchorOptions={anchorOptions} change={change} onChooseMedia={setPickerPlacementId} />}
     <ProductionPlanV2Materials plan={draft} focusPlacementId={focusPlacementId} candidates={candidates}
      pending={(data.productionPlanV2PendingBindings as Record<string, string> | undefined) ?? {}}
      pendingErrors={(data.productionPlanV2PendingErrors as Record<string, string> | undefined) ?? {}}
      change={change} choose={setPickerPlacementId} onCalibrate={(turnId) => { setFocusTurnId(turnId); setView('speech'); }}
      onSourceChange={(turnId, placementId) => void editSpeechSource(turnId, placementId)} onVideoSoundChange={(placementId, enabled) => void editVideoSound(placementId, enabled)} />
     <button className="tap-button" onClick={() => change((next) => { next.beats.push({ id: crypto.randomUUID(), purpose: '新段落', duration_frames: Math.max(1, Math.round(3 * next.output.fps_num / next.output.fps_den)), turns: [] }); })}>添加段落</button>
     <section className="space-y-2 border-t border-[var(--ui-border-soft)] pt-4"><div className="flex items-center justify-between gap-2"><h3 className="text-sm font-semibold">命名范围 · {draft.selections?.length ?? 0} 项</h3><button className="tap-button" onClick={() => change((next) => { const first = next.beats[0]; if (!first) return; next.selections ??= []; next.selections.push({ id: crypto.randomUUID(), label: '新范围', start: { kind: 'beat', ref_id: first.id, edge: 'start' }, end: { kind: 'beat', ref_id: first.id, edge: 'end' } }); })}>添加命名范围</button></div><p className="text-xs text-text-muted">按段落、台词或已对齐的词确定范围。画面、声音和字幕可以共同引用它。</p>
      {(draft.selections ?? []).map((selection, index) => { const inUse = draft.placements.some((placement) => [placement.start, placement.end].some((anchor) => anchor.kind === 'selection' && anchor.ref_id === selection.id)); return <div key={selection.id} data-plan-selection-id={selection.id} className={`grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)_auto] gap-2 rounded-lg border p-2 ${focusSelectionId === selection.id ? 'border-primary' : 'border-[var(--ui-border-soft)]'}`}><input aria-label={`范围 ${index + 1} 名称`} className={field} value={selection.label} onChange={(event) => change((next) => { next.selections![index].label = event.target.value; })} />{(['start', 'end'] as const).map((edge) => <select key={edge} aria-label={`范围 ${index + 1} ${edge === 'start' ? '开始' : '结束'}`} className={field} value={JSON.stringify(selection[edge])} onChange={(event) => change((next) => { next.selections![index][edge] = JSON.parse(event.target.value) as PlanAnchorV2; })}>{!anchorOptions.some((option) => JSON.stringify(option.anchor) === JSON.stringify(selection[edge])) && <option value={JSON.stringify(selection[edge])}>当前锚点</option>}{anchorOptions.map((option) => <option key={`${option.anchor.kind}:${option.anchor.ref_id}:${option.anchor.edge}`} value={JSON.stringify(option.anchor)}>{option.label}</option>)}</select>)}<button className="tap-button" aria-label={`删除范围 ${index + 1}`} title={inUse ? '先解除时间线条目对此范围的引用' : undefined} disabled={inUse} onClick={() => change((next) => { next.selections!.splice(index, 1); })}>删除</button></div>; })}
     </section>
    </>
    <section className="space-y-3 border-t border-[var(--ui-border-soft)] pt-4">
     <div className="flex flex-wrap items-center justify-between gap-3"><div><h3 className="text-sm font-semibold">台词与发声校准</h3><p className="mt-1 text-xs text-text-muted">只有真实发声素材需要校准时间；无台词段可以直接编排。</p></div>
      <button type="button" className="tap-button" aria-expanded={view === 'speech'} onClick={() => setView(view === 'speech' ? 'materials' : 'speech')}>
       {view === 'speech' ? '收起校准' : '台词主线'}</button></div>
    {view === 'speech' && <ProductionSpeechMainline plan={draft} change={change} onChoosePlacement={(placementId) => {
     const placement = draft.placements.find((item) => item.id === placementId);
     if (placement?.kind === 'subtitle') {
      focusedEntry.current = false;
      useCanvasStore.getState().updateNodeData(id, { productionPlanV2FocusPlacementId: placementId });
      setView('materials');
     } else setPickerPlacementId(placementId);
    }}
     onImportSrt={async (takeId, originalSrt, rows, basis = 'srt_import', recognitionRef, correctedWords) => {
      if (!project) throw new Error('请先打开项目');
      const saved = await flushProductionPlanV2AutoSave(project, id);
      const currentData = useCanvasStore.getState().nodes.find((node) => node.id === id)?.data;
      const current = currentData?.productionPlanV2Draft as ProductionPlanV2;
      const exactRef = saved ?? currentData?.productionPlanV2Ref as RemixArtifactRef;
      const take = current.speech_takes?.find((item) => item.id === takeId);
      const resource = current.resources.find((item) => item.id === take?.resource_id);
      if (!take || !resource || !exactRef) throw new Error('发声素材或方案版本已变化，请重新打开导入');
      const seen = new Set<string>();
      const accepted = rows.map((row) => {
       if (!take.turn_ids.includes(row.turn_id)) throw new Error('台词不属于选中素材');
       const matches = row.cue_id === null ? [] : (current.speech_cues ?? []).filter((cue) => cue.take_id === takeId && cue.turn_id === row.turn_id && (row.cue_id ? cue.id === row.cue_id : cue.text === row.text));
       if (row.cue_id && matches.length !== 1) throw new Error('指定台词行已变化，请重新匹配');
       if (matches.length > 1) throw new Error('重复台词无法唯一匹配，请先复核对应行');
       const cueId = matches[0]?.id ?? crypto.randomUUID();
       if (seen.has(cueId)) throw new Error('重复台词无法唯一匹配，请先复核对应行');
       seen.add(cueId);
       return { id: cueId, turn_id: row.turn_id, text: row.text, start_us: row.source_start_us, end_us: row.source_end_us };
      });
      const textHashes: Record<string, string> = {};
      if (correctedWords?.length) for (const row of accepted) {
       if (textHashes[row.turn_id]) continue;
       const text = current.beats.flatMap((beat) => beat.turns).find((turn) => turn.id === row.turn_id)?.text;
       if (!text) throw new Error('方案台词已变化，请重新核对识别词');
       const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
       textHashes[row.turn_id] = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
      }
      applyAcceptedSpeechCues(structuredClone(current), takeId, accepted, basis, { artifact_id: 'speech-correction-preview', revision: 1 }, correctedWords, textHashes);
      const evidence = await createRemixArtifact(project, 'speech_cue_evidence.v1', { take_id: takeId, media_hash: resource.media_hash, basis, cues: accepted, source_plan_ref: { artifact_id: exactRef.artifactId, revision: exactRef.revision }, ...(basis === 'srt_import' ? { original_srt: originalSrt } : {}), ...(recognitionRef ? { recognition_ref: { artifact_id: recognitionRef.artifactId, revision: recognitionRef.revision } } : {}), ...(correctedWords?.length ? { corrected_words: correctedWords } : {}) }, recognitionRef ? [exactRef, recognitionRef] : [exactRef]);
      const latestData = useCanvasStore.getState().nodes.find((node) => node.id === id)?.data;
      if (latestData?.productionPlanV2Draft !== current || (latestData?.productionPlanV2Ref as RemixArtifactRef)?.revision !== exactRef.revision) throw new Error('方案在导入期间已变化，请重新确认');
      change((next) => applyAcceptedSpeechCues(next, takeId, accepted, basis, { artifact_id: evidence.id, revision: evidence.revision }, correctedWords, textHashes));
     }} renderCalibration={(turn, label, onRecognizedSrt) => <ProductionSpeechTimingControls plan={draft} turn={turn} label={label} project={project} nodeId={id} onRecognizedSrt={onRecognizedSrt}
      onBind={(placementId) => editSpeechSource(turn.id, placementId)}
     onAccept={(timings) => change((next) => { acceptPlanSpeechTimings(next, timings); })} />} />}
    </section>
   </> : <p>正在读取作品方案…</p>}
    {Boolean(error || draftTaskError || data.productionPlanV2SaveError) && <p role="alert" className="rounded-lg border border-red-400/25 bg-red-500/10 p-3 text-sm text-red-300">{error || draftTaskError || String(data.productionPlanV2SaveError)}{data.productionPlanV2Conflict ? ' · 版本冲突，已保留当前未提交修改，请重新加载并比较。' : ''}</p>}
   </main>
   <footer className="flex items-center justify-between gap-4 border-t border-[var(--ui-border-soft)] px-6 py-4"><span role="status" className="text-xs text-text-muted">{data.productionPlanV2SaveError ? '自动保存失败' : data.productionPlanV2Dirty ? '正在自动保存…' : ref ? '已自动保存' : '填写内容后自动保存'}{unboundSpeechSources.length ? ` · ${unboundSpeechSources.length} 句发声素材待准备` : actionableSpeechIssues.length ? ` · ${actionableSpeechIssues.length} 句台词待校准` : speechIssues.length ? ` · ${speechIssues.length} 句发声范围待调整` : ''}</span><button className="tap-button tap-button-quiet-primary" disabled={!draft || waitingForSpeech} onClick={continueToCompose}>{waitingForSpeech ? '校准中，请稍候' : unboundSpeechSources.length ? '先准备发声素材' : actionableSpeechIssues.length ? '先校准台词时间' : speechIssues.length ? '进入视频合成调整发声范围' : '进入视频合成'}</button></footer>
   {pickerPlacement && <ProductionPlanMediaPicker kind={pickerPlacement.kind === 'audio' ? 'audio' : 'visuals'} description={pickerPlacement.description ?? ''}
    candidates={candidates} recentNodeId={data.productionPlanLastMediaNodeId as string | undefined} busy={binding} error={error}
    onClose={() => { setPickerPlacementId(null); setError(null); }} onCreate={createMaterial} onSelect={(nodeId) => { void bindCandidate(nodeId); }} />}
  </div>
 </div>, document.body);
}
