import { createPortal } from 'react-dom';
import { useEffect, useId, useRef, useState } from 'react';
import { associateReferenceMaterialUpload, confirmReferenceMaterialReviewBatch, confirmReferenceDecision, confirmReferenceSpeechCorrection, confirmReferenceMaterialReview, confirmReferenceOutputReview, getReferenceDirectorState, REFERENCE_DIRECTOR_PHASE_LABELS, type DirectorMaterialReview, type DirectorOutputReview, type DirectorSpeechCorrection, type ReferenceDecision, type ReferenceDirectorState, type ReferenceProductionState } from '@/api/referenceDirector';
import { flushFreezoneCanvasRuntime, captureFreezoneCanvasScope } from '@/features/freezone/canvasSyncRuntime';
import { REFERENCE_ANALYSIS_STATUS_EVENT, referenceAnalysisRunMatchesScope } from '@/features/freezone/referenceAnalysisEvents';
import { useCanvasStore } from '@/stores/canvasStore';
import type { ReferenceAnalysisRun } from '@/api/referenceAnalysisRuns';
import { useAuthStore } from '@/stores/auth-store';
import { refreshProductionCanvas } from './productionCanvasRefresh';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import { MaterialSpeechReviewCard } from './MaterialSpeechReviewCard';

const DIRECTOR_STATUS_EVENT = 'reference-director-status';
const EMPTY: ReferenceDirectorState = { commissions: [], decisions: [], material_work: [], speech_corrections: [] };
const STEP_LABELS = { planned: '待制作', awaiting_upload: '等待上传', submitted: '制作中', candidate: '待验收', accepted: '已验收', rejected: '需返工', stale: '依赖已变化' };
const REVIEW_LABELS = { passed: '已通过', rejected: '需返工', unavailable: '待审看' };
const OUTPUT_CHECK_LABELS: Record<string, string> = { encoded_dimensions: '画面尺寸', encoded_fps: '帧率', encoded_duration: '实际时长', expected_audio_stream: '声音轨道', current_native_render: '合成结果版本', current_source_hashes: '素材版本' };
const ACTIONS = { direction: '确认复刻方向', plan: '确认作品方案', material: '确认素材选择', composition: '确认可编辑合成' };
const PROPOSAL_LABELS: Record<string, string> = { target_goal: '创作目标', preserve_replace: '保留与替换', output_requirements: '输出要求', reuse_resource_refs: '采用素材', keep: '保留', preserve: '保留', replace: '替换', reuse: '直接采用素材', regenerate: '重新制作', goal: '目标', summary: '方案摘要', dialogue: '台词', text: '内容', speaker: '说话人', direction: '复刻方向', plan_ref: '作品方案版本', output: '交付设置', findings: '审核结果' };
function Proposal({ value }: { value: unknown }) {
  if (value == null) return null;
  if (typeof value !== 'object') return <span className="break-words">{String(value)}</span>;
  if (Array.isArray(value)) return <ul className="space-y-1 pl-4 list-disc">{value.map((row, index) => <li key={index}><Proposal value={row} /></li>)}</ul>;
  return <dl className="space-y-1">{Object.entries(value).filter(([key]) => !/(?:^|_)(?:id|ids|ref|refs|metadata|hash|revision)$/.test(key)).map(([key, row]) => <div key={key}><dt className="inline font-medium">{PROPOSAL_LABELS[key] ?? key}：</dt><dd className="inline"><Proposal value={row} /></dd></div>)}</dl>;
}
function locate(nodeId: string) {
  const store = useCanvasStore.getState();
  if (!store.nodes.some((node) => node.id === nodeId)) return;
  store.onNodesChange(store.nodes.map((node) => ({ id: node.id, type: 'select' as const, selected: node.id === nodeId })));
  store.setSelectedNode(nodeId);
  store.requestFocusNode(nodeId);
}
const samePlan = (ref: unknown, preview: DirectorSpeechCorrection) => {
  const current = ref as { artifactId?: string; revision?: number } | null;
  return current?.artifactId === preview.source_plan_ref.artifact_id && current.revision === preview.source_plan_ref.revision;
};
const planArtifactId = (ref: unknown) => (ref as { artifactId?: string } | null)?.artifactId;
const materialReviewKey = (review: DirectorMaterialReview) => JSON.stringify([review.review_ref, review.work_ref, review.step_id, review.resource]);
const TECHNICAL_CHECKS: Record<string, string> = { current_canvas_node: '画布素材与当前节点一致', project_file_hash: '文件完整未变', native_media_probe: '文件可播放并带声音轨道' };
function humanizeFinding(value: string) {
  // Host checks are machine codes; a user only needs the conclusion. Legacy English
  // fallbacks say "a human must look", which the confirmation button already states.
  if (TECHNICAL_CHECKS[value]) return TECHNICAL_CHECKS[value];
  if (/^Actual content inspection unavailable/i.test(value)) return '系统未做自动内容判断，请以你的审看为准。';
  if (/^Audio content inspection unavailable/i.test(value)) return '系统未做自动声音判断，请完整试听。';
  return value;
}
type Tone = 'success' | 'warning' | 'danger' | 'muted';
const statusDot = (tone: Tone) => `inline-block h-1.5 w-1.5 shrink-0 rounded-full ${tone === 'success' ? 'bg-success' : tone === 'danger' ? 'bg-destructive' : tone === 'warning' ? 'bg-warning' : 'bg-text-muted/60'}`;
function technicalSummary(technical: DirectorMaterialReview['technical']): { label: string; tone: Tone } {
  if (technical.status === 'rejected') return { label: '文件检查未通过', tone: 'danger' };
  if (technical.status !== 'passed') return { label: '文件检查待完成', tone: 'muted' };
  return { label: '文件可用', tone: 'success' };
}
function reviewHint(review: DirectorMaterialReview) {
  if (review.technical.status !== 'passed') return '文件检查通过后才能确认。';
  const kind = review.resource.media_kind;
  if (kind === 'image') return '请确认画面主体、构图与细节符合预期。';
  if (kind === 'audio') return '请完整试听，确认语气、音量与噪音可接受。';
  return '请完整观看并试听，重点确认画面主体与声音是否符合预期。';
}
const pagerArrowClass = 'inline-flex size-6 shrink-0 items-center justify-center rounded-[var(--ui-radius-pill)] border border-[var(--ui-border-soft)] bg-[rgba(var(--bg-rgb)/0.42)] text-text-muted transition-colors duration-[var(--duration-fast)] enabled:hover:border-[var(--ui-border-strong)] enabled:hover:text-text disabled:cursor-not-allowed disabled:opacity-40';
const pagerSegmentClass = 'h-[3px] flex-1 rounded-full bg-[var(--ui-border-soft)] transition-colors duration-[var(--duration-base)] data-[state=done]:bg-success/45 data-[state=active]:bg-[rgb(var(--accent-rgb))]';
const panelClass = 'rounded-[var(--ui-radius-xl)] border border-[var(--ui-border-soft)] bg-[var(--ui-surface-panel)] p-3 space-y-2';

export function ReferenceDirectorPanel({ projectId, canvasId, agentId, connected, controlledState, progress,
  onRefresh, preferenceKey, summaryTarget, defaultExpanded = true, conversationMode = false }: { projectId: string | null; canvasId: string | null;
  agentId: string | null; connected: boolean; controlledState?: ReferenceDirectorState;
  progress?: ReferenceProductionState; onRefresh?: () => Promise<void>; preferenceKey?: string; defaultExpanded?: boolean; summaryTarget?: HTMLElement | null; conversationMode?: boolean }) {
  const account = useAuthStore((store) => store.username);
  const [state, setState] = useState<ReferenceDirectorState>(EMPTY);
  const [expanded, setExpanded] = useState(defaultExpanded);
  const controlledRef = useRef(controlledState);
  controlledRef.current = controlledState;
  const externalRefreshRef = useRef(onRefresh);
  externalRefreshRef.current = onRefresh;
  useEffect(() => {
    if (controlledState !== undefined) setState(controlledState);
  }, [controlledState]);
  useEffect(() => {
    if (!preferenceKey) return;
    try {
      const saved = localStorage.getItem(preferenceKey);
      setExpanded(saved === null ? defaultExpanded : saved === 'true');
    } catch { setExpanded(defaultExpanded); }
  }, [preferenceKey, defaultExpanded]);
  const toggleExpanded = () => setExpanded((value) => {
    const next = !value;
    if (preferenceKey) {
      try { localStorage.setItem(preferenceKey, String(next)); } catch { /* UI remains usable. */ }
    }
    return next;
  });
  const detailsId = useId();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusyState] = useState<string | null>(null);
  const busyRef = useRef<string | null>(null);
  const setBusy = (value: string | null) => { busyRef.current = value; setBusyState(value); };
  const mediaElements = useRef<Record<string, HTMLMediaElement | null>>({});
  const batchCommand = useRef<{ input: string; id: string } | null>(null);
  const [batchMaterials, setBatchMaterials] = useState<Set<string>>(new Set());
  const [viewedMaterials, setViewedMaterials] = useState<Set<string>>(new Set());
  const [viewedOutputs, setViewedOutputs] = useState<Set<string>>(new Set());
  const [uploadSelections, setUploadSelections] = useState<Record<string,string>>({});
  const [expandedStages, setExpandedStages] = useState<Set<string>>(new Set());
  const [stageMaterialIndex, setStageMaterialIndex] = useState<Record<string, number>>({});
  const canvasNodes = useCanvasStore((store) => store.nodes);
  const [reload, setReload] = useState(0);
  const scopeRef = useRef('');
  const epochRef = useRef(0);
  const refreshRef = useRef<() => Promise<void>>(async () => {});
  const identityScope = JSON.stringify([account, projectId, canvasId, agentId?.trim() || 'main', progress?.commission_ref.artifact_id ?? controlledState?.commissions.map((row) => row.commission_ref.artifact_id) ?? 'legacy', progress?.work_ref?.artifact_id]);
  const scope = JSON.stringify([account, projectId, canvasId, agentId?.trim() || 'main', progress?.commission_ref ?? controlledState?.commissions.map((row) => row.commission_ref.artifact_id) ?? 'legacy', progress?.work_ref, controlledState?.material_reviews?.map(materialReviewKey), controlledState?.material_stages, controlledState?.decisions, progress?.projection_revision]);
  scopeRef.current = scope;

  useEffect(() => { epochRef.current += 1; setBusy(null); setError(null); }, [scope]);

  useEffect(() => {
    const epoch = ++epochRef.current;
    let cancelled = false;
    let request = 0;
    let canvasQueue: Promise<void> = Promise.resolve();
    setState(controlledRef.current ?? EMPTY); setError(null); setBusy(null); setViewedOutputs(new Set()); setViewedMaterials(new Set()); setBatchMaterials(new Set()); setUploadSelections({}); setExpandedStages(new Set());
    if (!projectId || !canvasId || !connected) return;
    if (controlledRef.current !== undefined) {
      refreshRef.current = async () => { await externalRefreshRef.current?.(); };
      return () => { cancelled = true; epochRef.current += 1; refreshRef.current = async () => {}; };
    }
    const current = () => !cancelled && scopeRef.current === scope && epochRef.current === epoch;
    const refresh = async () => {
      const sequence = ++request;
      try {
        const next = await getReferenceDirectorState(projectId, canvasId);
        if (!current() || request !== sequence) return;
        setState(next); setError(null);
      } catch (caught) {
        if (current() && request === sequence) setError(caught instanceof Error ? caught.message : String(caught));
      }
    };
    refreshRef.current = refresh;
    const onStatus = (event: Event) => {
      const run = (event as CustomEvent<{ run?: ReferenceAnalysisRun }>).detail?.run;
      if (run && referenceAnalysisRunMatchesScope(run, projectId, canvasId, agentId)) void refresh();
    };
    const refreshCanvas = () => {
      // Invalidate an older state GET before restoring the graph it refers to.
      request += 1;
      canvasQueue = canvasQueue.then(async () => {
        const isCanvas = captureFreezoneCanvasScope(projectId, canvasId);
        if (!current() || !isCanvas()) return;
        try {
          await refreshProductionCanvas(projectId, canvasId, current);
          if (!current() || !isCanvas()) return;
          await refresh();
        } catch (caught) { if (current()) setError(caught instanceof Error ? caught.message : String(caught)); }
      });
      return canvasQueue;
    };
    const onDirectorStatus = (event: Event) => {
      const detail = (event as CustomEvent<{ project_id?: string; canvas_id?: string; agent_id?: string; canvas_changed?: boolean }>).detail;
      if (!detail || detail.project_id !== projectId || detail.canvas_id !== canvasId
        || detail.agent_id && detail.agent_id !== (agentId?.trim() || 'main')) return;
      if (detail.canvas_changed === true) void refreshCanvas();
      else void refresh();
    };
    window.addEventListener(REFERENCE_ANALYSIS_STATUS_EVENT, onStatus);
    window.addEventListener(DIRECTOR_STATUS_EVENT, onDirectorStatus);
    void refresh();
    return () => {
      cancelled = true; epochRef.current += 1; refreshRef.current = async () => {};
      window.removeEventListener(REFERENCE_ANALYSIS_STATUS_EVENT, onStatus);
      window.removeEventListener(DIRECTOR_STATUS_EVENT, onDirectorStatus);
    };
  }, [projectId, canvasId, agentId, connected, reload, identityScope, controlledState !== undefined]);

  const decisionCurrent = (decision: ReferenceDecision) => decision.status === 'pending' && (!progress && !state.commissions.length || state.commissions.some((commission) =>
    commission.project_id === projectId && commission.canvas_id === canvasId
    && commission.commission_ref.artifact_id === decision.commission_ref.artifact_id
    && commission.commission_ref.revision === decision.commission_ref.revision));
  const confirm = async (decision: ReferenceDecision) => {
    if (!projectId || !canvasId || !connected || busyRef.current || !decisionCurrent(decision)) return;
    const epoch = epochRef.current;
    const current = () => scopeRef.current === scope && epochRef.current === epoch;
    setBusy(decision.decision_ref.artifact_id); setError(null);
    try {
      const result = await confirmReferenceDecision(projectId, canvasId, decision.decision_ref.artifact_id, ...(progress ? [{
        decision_ref: decision.decision_ref, commission_ref: progress.commission_ref,
        agent_id: agentId?.trim() || 'main', expected_projection_revision: progress.projection_revision,
      }] : []));
      if (!current()) return;
      setState((prior) => ({ ...prior, decisions: prior.decisions.filter((row) => row.decision_ref.artifact_id !== decision.decision_ref.artifact_id), commissions: prior.commissions.map((row) => row.commission_ref.artifact_id === result.commission.commission_ref.artifact_id ? result.commission : row) }));
      await refreshRef.current();
    } catch (caught) { if (current()) setError(caught instanceof Error ? caught.message : String(caught)); }
    finally { if (current()) setBusy(null); }
  };
  const confirmOutput = async (review: DirectorOutputReview) => {
    const key = `${review.review_ref.artifact_id}:${review.review_ref.revision}`;
    if (!projectId || !canvasId || !connected || busy || !viewedOutputs.has(key) || review.technical.status !== 'passed' || review.status === 'passed') return;
    const epoch = epochRef.current;
    const current = () => scopeRef.current === scope && epochRef.current === epoch;
    setBusy(review.review_ref.artifact_id); setError(null);
    try {
      const result = await confirmReferenceOutputReview(projectId, canvasId, review.review_ref);
      if (!current()) return;
      setState((prior) => ({ ...prior,
        output_reviews: prior.output_reviews?.filter((row) => row.review_ref.artifact_id !== review.review_ref.artifact_id),
        commissions: prior.commissions.map((row) => row.commission_ref.artifact_id === result.commission.commission_ref.artifact_id ? result.commission : row),
      }));
      await refreshRef.current();
    } catch (caught) { if (current()) setError(caught instanceof Error ? caught.message : String(caught)); }
    finally { if (current()) setBusy(null); }
  };
  const materialGroups = state.material_stages !== undefined
    ? state.material_stages.map((stage) => ({ ...stage, reviews: (state.material_reviews ?? []).filter((review) =>
      stage.step_ids.includes(review.step_id) && stage.stage_id.startsWith(`${(review.current_work_ref ?? review.work_ref).artifact_id}:stage:`)) }))
    : (state.material_reviews?.length ? [{ stage_id: 'legacy-materials', stage_number: 0,
      status: state.material_reviews.every((review) => review.content.status === 'passed') ? 'confirmed' as const : 'awaiting_confirmation' as const,
      total: state.material_reviews.length, confirmed: state.material_reviews.filter((review) => review.content.status === 'passed').length,
      reviews: state.material_reviews }] : []);
  const actionableMaterials = materialGroups.filter((group) => group.status === 'awaiting_confirmation').flatMap((group) => group.reviews);
  const confirmMaterial = async (review: DirectorMaterialReview) => {
    if (!projectId || !canvasId || !connected || busyRef.current || !actionableMaterials.includes(review) || review.content.status === 'passed' || review.technical.status !== 'passed'
      || review.resource.media_kind !== 'image' && !viewedMaterials.has(materialReviewKey(review))) return;
    const epoch = epochRef.current;
    const current = () => scopeRef.current === scope && epochRef.current === epoch;
    setBusy(review.review_ref.artifact_id); setError(null);
    try {
      await confirmReferenceMaterialReview(projectId, canvasId, review.review_ref.artifact_id);
      if (!current()) return;
      // Keep the current card while the authoritative projection refreshes.
      // Removing the review here made partial approvals and completed stages disappear.
      await refreshRef.current();
    } catch (caught) { if (current()) setError(caught instanceof Error ? caught.message : String(caught)); }
    finally { if (current()) setBusy(null); }
  };
  const selectedMaterials = actionableMaterials.filter((review) => review.content.status !== 'passed' && batchMaterials.has(materialReviewKey(review)));
  const materialsReady = (reviews: DirectorMaterialReview[]) => reviews.length > 0 && reviews.length <= 20 && reviews.every((review) => review.technical.status === 'passed'
    && (review.resource.media_kind === 'image' || viewedMaterials.has(materialReviewKey(review))));
  const confirmBatch = async (reviews: DirectorMaterialReview[]) => {
    const selected = selectedMaterials.filter((review) => reviews.includes(review));
    if (!projectId || !canvasId || !connected || busyRef.current || !materialsReady(selected)) return;
    const epoch = epochRef.current;
    const current = () => scopeRef.current === scope && epochRef.current === epoch;
    const items = selected.map((review) => ({ review_ref: review.review_ref, work_ref: review.current_work_ref ?? review.work_ref, step_id: review.step_id, node_id: review.resource.node_id }));
    const serialized = JSON.stringify([scope, items]);
    if (batchCommand.current?.input !== serialized) batchCommand.current = { input: serialized, id: 'materials-' + crypto.randomUUID() };
    setBusy('material-batch'); setError(null);
    try {
      await confirmReferenceMaterialReviewBatch(projectId, canvasId, { items, command_id: batchCommand.current.id });
      if (!current()) return;
      setBatchMaterials((prior) => {
        const next = new Set(prior); selected.forEach((review) => next.delete(materialReviewKey(review))); return next;
      });
      await refreshRef.current();
    } catch (caught) { if (current()) setError(caught instanceof Error ? caught.message : String(caught)); }
    finally { if (current()) setBusy(null); }
  };
  const confirmSpeech = async (preview: DirectorSpeechCorrection) => {
    if (!projectId || !canvasId || !connected || busy || preview.blockers.length > 0) return;
    const epoch = epochRef.current;
    const isCanvas = captureFreezoneCanvasScope(projectId, canvasId);
    const current = () => scopeRef.current === scope && epochRef.current === epoch && isCanvas();
    const before = useCanvasStore.getState().nodes.find((row) => row.id === preview.node_id);
    if (!current() || !before || before.type !== 'scriptNode' || before.data.remixRole !== 'production_plan'
      || !samePlan(before.data.productionPlanV2Ref, preview) || before.data.productionPlanV2Dirty || before.data.productionPlanV2Conflict) {
      setError('方案版本已变化或存在未保存修改，请先处理方案，再刷新制作状态。'); return;
    }
    const draft = before.data.productionPlanV2Draft;
    setBusy(preview.preview_ref.artifact_id); setError(null);
    try {
      const result = await confirmReferenceSpeechCorrection(projectId, canvasId, preview.preview_ref.artifact_id);
      if (!current()) return;
      if (result.node_id !== preview.node_id || result.plan.schema !== 'production_plan.v2' || result.plan.id !== preview.source_plan_ref.artifact_id || result.plan.revision <= preview.source_plan_ref.revision) throw new Error('校正结果与作品方案不匹配，请刷新制作状态。');
      const store = useCanvasStore.getState();
      const node = store.nodes.find((row) => row.id === preview.node_id);
      if (!node || !samePlan(node.data.productionPlanV2Ref, preview) || node.data.productionPlanV2Draft !== draft || node.data.productionPlanV2Dirty || node.data.productionPlanV2Conflict) {
        throw new Error('语音校正已保存，本地修改已保留；请处理版本冲突后刷新方案。');
      }
      const nextRef = { artifactId: result.plan.id, revision: result.plan.revision };
      store.updateNodeData(node.id, { productionPlanV2Ref: nextRef, productionPlanV2Draft: result.plan.payload, productionPlanV2Dirty: false, productionPlanV2Conflict: false, productionPlanV2SaveError: null });
      for (const row of store.nodes) if (row.type === 'videoComposeNode' && row.data.productionPlanNodeId === node.id) store.updateNodeData(row.id, { productionPlanV2Ref: nextRef, remixInputsCurrent: false });
      setState((prior) => ({ ...prior, speech_corrections: prior.speech_corrections?.filter((row) => row.preview_ref.artifact_id !== preview.preview_ref.artifact_id) }));
      await refreshRef.current();
    } catch (caught) { if (current()) setError(caught instanceof Error ? caught.message : String(caught)); }
    finally { if (current()) setBusy(null); }
  };

  if (!state.commissions.length && !state.decisions.length && !state.material_work.length && !state.speech_corrections?.length && !state.material_reviews?.length && !state.material_stages?.length && !state.output_reviews?.length && !error) return null;
  const pendingCount = Math.max(0, (progress?.progress.user_action_count ?? (state.decisions.filter((row) => row.status === 'pending').length
    + state.material_work.reduce((count, work) => count + work.steps.filter((step) => step.source === 'upload' && ['planned', 'awaiting_upload', 'rejected', 'stale'].includes(step.status)).length, 0)
    + actionableMaterials.filter((review) => review.content.status !== 'passed').length
    + (state.output_reviews ?? []).filter((review) => review.status !== 'passed').length
    + (state.speech_corrections ?? []).length)) - state.decisions.filter((row) => row.kind === 'direction' && row.status === 'pending').length);
  const phaseLabel = (phase: keyof typeof REFERENCE_DIRECTOR_PHASE_LABELS) => progress?.execution_status === 'stopped' ? '制作已停止'
    : progress?.execution_status === 'failed' ? (phase === 'drafting_plan' ? '作品方案准备失败' : `${REFERENCE_DIRECTOR_PHASE_LABELS[phase]}遇到问题`)
    : progress?.execution_status === 'waiting_user' ? '等待审看或确认' : REFERENCE_DIRECTOR_PHASE_LABELS[phase];
  const summary = <>
    <div className="flex flex-wrap items-center justify-between gap-2">
      <span className="font-medium">虾导制作进度</span>
      <button type="button" className="tap-button" disabled={!connected || Boolean(busy)} onClick={() => void refreshRef.current()}>刷新制作状态</button>
    </div>
    <details><summary className="cursor-pointer">{state.commissions.map((commission) => phaseLabel(commission.phase)).join('；')} · {pendingCount} 项待处理</summary>
      {progress ? <p>素材就绪 {progress.progress.materials_available}/{progress.progress.materials_total} · 已采用 {progress.progress.materials_accepted}/{progress.progress.materials_total}</p> : null}
      {state.commissions.map((commission) => <p key={commission.commission_ref.artifact_id}>{commission.blocker?.reason}</p>)}
    </details>
  </>;
  return <section aria-label={conversationMode ? '作品制作操作' : '虾导制作进度'} className={`mb-2 space-y-2 text-sm ${expanded && !conversationMode ? 'max-h-80 overflow-y-auto' : ''}`}>
    {!conversationMode && (summaryTarget ? createPortal(summary, summaryTarget) : <>
    <div className="flex flex-wrap items-center justify-between gap-2">
      <span className="font-medium">虾导制作进度</span>
      <div className="flex items-center gap-1">
        <button type="button" className="tap-button" disabled={!connected || Boolean(busy)} onClick={() => controlledRef.current !== undefined ? void refreshRef.current() : setReload((value) => value + 1)}>刷新制作状态</button>
        <button type="button" className="tap-button" aria-expanded={expanded} aria-controls={detailsId} aria-label={`${expanded ? '收起' : '展开'}虾导制作进度`} onClick={toggleExpanded}>{expanded ? '收起' : '展开'}</button>
      </div>
    </div>
    {!expanded ? <div className="space-y-1" role="status" aria-live="polite">
      {state.commissions.map((commission) => <div key={commission.commission_ref.artifact_id}>
        <p>{phaseLabel(commission.phase)}</p>
        {commission.blocker ? <p className="break-words">{commission.blocker.reason}{commission.blocker.next_action ? `；${commission.blocker.next_action}` : ''}</p> : null}
      </div>)}
      {progress ? <p>素材就绪 {progress.progress.materials_available}/{progress.progress.materials_total} · 已采用 {progress.progress.materials_accepted}/{progress.progress.materials_total}{pendingCount ? ` · ${pendingCount} 项待审看` : ''}</p>
        : pendingCount ? <p>{pendingCount} 项待处理</p> : null}
      {progress?.execution_problem?.length ? <p className="text-warning">有素材任务失败或中断，请展开查看；已完成的素材仍已保留。</p> : null}
    </div> : null}
    </>)}
    <div id={detailsId} hidden={!conversationMode && !summaryTarget && !expanded} className="space-y-2">
    {!conversationMode && state.commissions.map((commission) => <div className={panelClass} key={commission.commission_ref.artifact_id}>
      <p role="status" aria-live="polite">{phaseLabel(commission.phase)}</p>
      <p>{commission.goal}</p>
      {commission.phase === 'awaiting_direction' && !state.decisions.some((decision) => decision.status === 'pending' && decision.kind === 'direction' && decision.commission_ref.artifact_id === commission.commission_ref.artifact_id) ? <p className="text-text-muted">方向建议尚未准备完成，请查看续接状态。</p> : null}
      {commission.blocker ? <p>{commission.blocker.reason}{commission.blocker.next_action ? `；${commission.blocker.next_action}` : ''}</p> : null}
      {commission.analysis_node_id ? <button className="tap-button" type="button" onClick={() => locate(commission.analysis_node_id!)}>查看原片拆解</button> : null}
      {commission.plan_ref && canvasNodes.some((node) => node.type === 'scriptNode' && planArtifactId(node.data.productionPlanV2Ref) === commission.plan_ref?.artifact_id)
        ? <button className="tap-button" type="button" onClick={() => {
          const node = useCanvasStore.getState().nodes.find((row) => row.type === 'scriptNode' && planArtifactId(row.data.productionPlanV2Ref) === commission.plan_ref?.artifact_id);
          if (node) locate(node.id);
        }}>查看作品方案</button> : null}
    </div>)}
    {state.decisions.filter((row) => row.status === 'pending' && row.kind !== 'direction' && row.kind !== 'plan'
      && (!conversationMode || row.kind !== 'material')).map((decision) => <article aria-label={ACTIONS[decision.kind]} className={panelClass} key={decision.decision_ref.artifact_id}>
      <p className="font-medium">{ACTIONS[decision.kind]}</p><p className="whitespace-pre-wrap break-words">{decision.summary}</p>
      <details><summary className="cursor-pointer">查看确认内容</summary><div className="mt-2"><Proposal value={decision.proposal} /></div></details>
      {decision.kind === 'composition' && typeof decision.proposal.compose_node_id === 'string' ? <button type="button" className="tap-button" onClick={() => locate(decision.proposal.compose_node_id as string)}>查看可编辑合成</button> : null}
      <button type="button" className="tap-button tap-button-quiet-primary" disabled={!connected || Boolean(busy) || !decisionCurrent(decision)} onClick={() => void confirm(decision)}>{busy === decision.decision_ref.artifact_id ? '正在确认…' : ACTIONS[decision.kind]}</button>
    </article>)}
    {!conversationMode && state.material_work.map((work) => <div className={panelClass} key={work.work_ref.artifact_id}>
      <p className="font-medium">素材制作依赖</p>
      <ul className="space-y-2">{work.requirements.map((requirement) => <li key={requirement.id}><p>{requirement.description}</p>{requirement.acceptance?.length ? <p className="text-xs">验收要求：{requirement.acceptance.join('；')}</p> : null}</li>)}</ul>
      <ul className="space-y-2">{work.steps.map((step) => {
        const deps = [...new Set([...step.depends_on, ...(step.references ?? []).flatMap((row) => row.step_id ? [row.step_id] : [])])];
        return <li key={step.id} className="border-t border-[var(--ui-border-soft)] pt-2"><div className="flex flex-wrap items-center gap-2"><span>{step.label}</span><span className="text-xs">{STEP_LABELS[step.status]}</span></div>
          {deps.length ? <p className="text-xs">依赖：{deps.map((id) => work.steps.find((row) => row.id === id)?.label ?? id).join('、')}</p> : null}
          {step.source === 'upload' && ['planned','awaiting_upload','rejected','stale'].includes(step.status) ? <div className="space-y-1">
            <p className="text-xs">将所需素材上传到画布后，选择素材关联此项，再审看验收。</p>
            <select aria-label={`选择${step.label}素材`} className="w-full rounded-[var(--ui-radius-md)] border border-[var(--ui-border-soft)] bg-[var(--ui-surface-field)] p-1" value={uploadSelections[work.work_ref.artifact_id+step.id] ?? ''} disabled={!connected || !!busy} onChange={(event) => setUploadSelections((prior) => ({...prior,[work.work_ref.artifact_id+step.id]:event.target.value}))}>
              <option value="">选择画布素材</option>
              {canvasNodes.filter((node) => { const field = step.media_kind === 'video' ? 'videoUrl' : step.media_kind === 'audio' ? 'audioUrl' : 'imageUrl'; return !!node.data[field] && node.data.workflowResultStale !== true && node.data.remixVisualOutputCurrent !== false; }).map((node) => <option key={node.id} value={node.id}>{String(node.data.label || node.data.name || node.id)}</option>)}
            </select>
            <button type="button" className="tap-button" disabled={!connected || !!busy || !uploadSelections[work.work_ref.artifact_id+step.id]} onClick={async () => {
              if (!projectId || !canvasId || busy) return;
              const epoch = epochRef.current; const isCanvas = captureFreezoneCanvasScope(projectId,canvasId);
              const current = () => scopeRef.current === scope && epochRef.current === epoch && isCanvas();
              if (!current()) return;
              setBusy('upload:'+step.id); setError(null);
              try {
                if (await flushFreezoneCanvasRuntime(projectId,canvasId) !== true) throw new Error('请先完成画布保存，再关联素材。');
                if (!current()) return;
                await associateReferenceMaterialUpload(projectId,canvasId,work.work_ref,step.id,uploadSelections[work.work_ref.artifact_id+step.id]);
                if (current()) await refreshRef.current();
              } catch (caught) { if (current()) setError(caught instanceof Error ? caught.message : String(caught)); }
              finally { if (current()) setBusy(null); }
            }}>关联{step.label}</button>
          </div> : null}
          {step.reason ? <p className="text-xs">{step.reason}</p> : null}
          {step.node_id ? <button type="button" className="tap-button mt-1" onClick={() => locate(step.node_id!)}>查看{step.label}</button> : null}
        </li>;
      })}</ul>
    </div>)}
    {materialGroups.map((group) => {
      const title = group.stage_number ? `第 ${group.stage_number} 阶段素材确认` : '素材确认';
      const open = group.status === 'awaiting_confirmation' || expandedStages.has(group.stage_id);
      const stageSelection = selectedMaterials.filter((review) => group.reviews.includes(review));
      const pendingIndex = group.reviews.findIndex((review) => review.content.status !== 'passed');
      const requestedIndex = stageMaterialIndex[group.stage_id] ?? Math.max(pendingIndex, 0);
      const activeIndex = Math.min(Math.max(requestedIndex, 0), Math.max(group.reviews.length - 1, 0));
      const statusText = group.status === 'preparing' ? '本阶段素材正在准备，全部通过技术检查后统一审看。'
        : group.status === 'confirmed' ? `本阶段素材已全部确认（${group.confirmed}/${group.total}）`
        : `本阶段素材已就绪，请统一审看 · 已确认 ${group.confirmed}/${group.total}`;
      return <article aria-label={title} className={panelClass} key={group.stage_id}>
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0 space-y-1">
            <h3 className="text-[13px] font-semibold tracking-[0.01em]">{title}</h3>
            <p role="status" className="text-xs leading-relaxed text-text-muted">{statusText}</p>
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            <span className="tap-chip px-2 py-0.5 text-[11px] tabular-nums" aria-hidden="true">{group.confirmed}/{group.total}</span>
            {group.status === 'confirmed' ? <button type="button" className="tap-button" aria-expanded={open} aria-label={`${open ? '收起' : '展开'}${title}`} onClick={() => setExpandedStages((prior) => {
              const next = new Set(prior); if (open) next.delete(group.stage_id); else next.add(group.stage_id); return next;
            })}>{open ? '收起素材' : '查看已确认素材'}</button> : null}
          </div>
        </div>
        {group.status !== 'preparing' && open ? <div className="space-y-3">{group.reviews.length > 1 ? <div role="group" aria-label="素材翻页" className="flex items-center gap-2">
          <button type="button" className={pagerArrowClass} aria-label="上一条" disabled={activeIndex <= 0} onClick={() => setStageMaterialIndex((prior) => ({ ...prior, [group.stage_id]: activeIndex - 1 }))}>
            <ChevronLeft className="size-3.5" strokeWidth={2} aria-hidden="true" />
          </button>
          <div className="flex flex-1 items-center gap-1" aria-hidden="true">
            {group.reviews.map((review, index) => <span key={review.review_ref.artifact_id} data-state={index === activeIndex ? 'active' : review.content.status === 'passed' ? 'done' : 'idle'} className={pagerSegmentClass} />)}
          </div>
          <span className="min-w-8 text-center text-[11px] tabular-nums text-text-muted" role="status" aria-live="polite">{activeIndex + 1}/{group.reviews.length}</span>
          <button type="button" className={pagerArrowClass} aria-label="下一条" disabled={activeIndex >= group.reviews.length - 1} onClick={() => setStageMaterialIndex((prior) => ({ ...prior, [group.stage_id]: activeIndex + 1 }))}>
            <ChevronRight className="size-3.5" strokeWidth={2} aria-hidden="true" />
          </button>
        </div> : null}{group.reviews.map((review, index) => {
      if (index !== activeIndex) return null;
      const reviewKey = materialReviewKey(review);
      const validPath = !review.resource.media_path.startsWith('/') && !review.resource.media_path.includes('\\') && review.resource.media_path.split('/').every((part) => part && part !== '.' && part !== '..');
      const url = validPath && projectId ? `/static/projects/${encodeURIComponent(projectId)}/${review.resource.media_path.split('/').map(encodeURIComponent).join('/')}` : null;
      const pending = review.content.status !== 'passed';
      const technical = technicalSummary(review.technical);
      const contentTone = review.content.status === 'passed' ? 'success' : review.content.status === 'rejected' ? 'danger' : 'warning';
      const findings = [...(review.technical.checks ?? []), ...(review.technical.findings ?? []), ...(review.content.checks ?? []), ...(review.content.findings ?? [])];
      return <div className="space-y-3 border-t border-[var(--ui-border-soft)] pt-3" key={review.review_ref.artifact_id}>
        <div className="flex items-baseline justify-between gap-2">
          <p className="text-[13px] font-medium">{state.material_work.find((work) => work.work_ref.artifact_id === (review.current_work_ref ?? review.work_ref).artifact_id)?.steps.find((step) => step.id === review.step_id)?.label ?? '素材需审看'}{review.content.status === 'passed' ? ' · 已确认' : ''}</p>
          <span className="tap-chip px-2 py-0.5 text-[11px] text-text-muted">{REVIEW_LABELS[review.content.status]}</span>
        </div>
        {url ? <div className="overflow-hidden rounded-[var(--ui-radius-lg)] border border-[var(--ui-border-soft)] bg-[var(--ui-surface-field)]">{review.resource.media_kind === 'image' ? <img src={url} alt="待审看素材" className="mx-auto max-h-56 w-full object-contain" /> : review.resource.media_kind === 'video' ? <video ref={(element) => { if (element) mediaElements.current[reviewKey] = element; else delete mediaElements.current[reviewKey]; }} controls preload="metadata" src={url} className="max-h-56 w-full" aria-label="待审看素材" /> : <audio ref={(element) => { if (element) mediaElements.current[reviewKey] = element; else delete mediaElements.current[reviewKey]; }} controls preload="metadata" src={url} className="w-full" aria-label="待审听素材" />}</div> : null}
        {projectId && canvasId && pending && review.resource.media_kind !== 'image' ? <MaterialSpeechReviewCard
          key={JSON.stringify([identityScope, materialReviewKey(review)])}
          projectId={projectId} canvasId={canvasId} agentId={agentId?.trim() || 'main'} review={review}
          active={(conversationMode || !!summaryTarget || expanded) && connected} refreshToken={progress ?? state}
          onSaved={() => refreshRef.current()} onSeek={(seconds) => {
            const media = mediaElements.current[reviewKey];
            if (media) { media.currentTime = seconds; void media.play()?.catch(() => {}); }
          }} /> : null}
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-xs">
          <span className="inline-flex items-center gap-1.5"><span aria-hidden="true" className={statusDot(technical.tone)} />{technical.label}</span>
          <span className="inline-flex items-center gap-1.5 text-text-muted"><span aria-hidden="true" className={statusDot(contentTone)} />内容{review.content.status === 'passed' ? '已确认' : pending ? '待你确认' : '需返工'}</span>
          <button type="button" className="tap-button ml-auto px-2 py-0.5 text-[11px]" onClick={() => locate(review.resource.node_id)}>查看素材节点</button>
        </div>
        {pending ? <p className="text-xs leading-relaxed text-text-muted">{reviewHint(review)}</p> : null}
        {findings.length ? <details className="text-xs text-text-muted"><summary className="cursor-pointer">检查详情</summary><ul className="mt-1 space-y-0.5">{findings.map((finding, findingIndex) => <li key={findingIndex}>{humanizeFinding(finding)}</li>)}</ul></details> : null}
        {pending ? <div className="space-y-2 rounded-[var(--ui-radius-lg)] bg-[var(--ui-surface-field)] p-2">
          {review.resource.media_kind !== 'image' ? <label className="flex items-start gap-2 text-xs leading-relaxed">
            <input type="checkbox" className="mt-0.5" checked={viewedMaterials.has(reviewKey)} disabled={Boolean(busy)} onChange={(event) => {
              const checked = event.currentTarget.checked;
              setViewedMaterials((prior) => { const next = new Set(prior); if (checked) next.add(reviewKey); else next.delete(reviewKey); return next; });
            }} />{review.resource.media_kind === 'audio' ? '已完整试听当前素材' : '已完整观看并试听当前素材'}
          </label> : null}
          <details className="text-xs text-text-muted">
            <summary className="cursor-pointer">一次确认本阶段的全部素材</summary>
            <label className="mt-2 flex items-start gap-2 leading-relaxed">
              <input type="checkbox" className="mt-0.5" checked={batchMaterials.has(reviewKey)} disabled={!connected || Boolean(busy) || review.technical.status !== 'passed' || selectedMaterials.length >= 20 && !batchMaterials.has(reviewKey)} onChange={(event) => {
                const checked = event.currentTarget.checked;
                setBatchMaterials((prior) => { const next = new Set(prior); if (checked) next.add(reviewKey); else next.delete(reviewKey); return next; });
              }} />已审看此素材，加入本次确认
            </label>
          </details>
          <button type="button" className="tap-button tap-button-primary w-full" disabled={!connected || Boolean(busy) || review.technical.status !== 'passed' || review.resource.media_kind !== 'image' && !viewedMaterials.has(reviewKey)} onClick={() => void confirmMaterial(review)}>{busy === review.review_ref.artifact_id ? '正在确认…' : '我已审看，确认素材内容'}</button>
        </div> : null}
      </div>;
    })}
    {group.status === 'awaiting_confirmation' && group.reviews.some((review) => review.content.status !== 'passed') ? <button type="button"
      className="tap-button w-full" disabled={!connected || Boolean(busy) || !materialsReady(stageSelection)}
      onClick={() => void confirmBatch(group.reviews)}>{busy === 'material-batch' ? '正在确认已勾选素材…' : '确认已勾选的素材'}</button> : null}
        </div> : null}
      </article>;
    })}
    {(state.output_reviews ?? []).map((review) => {
      const key = `${review.review_ref.artifact_id}:${review.review_ref.revision}`;
      return <div className={panelClass} key={key}>
        <p className="font-medium">成片审看</p>
        <video controls preload="metadata" src={review.media_url} className="max-h-48 w-full" aria-label="审看实际成片" />
        <p>技术检查：{REVIEW_LABELS[review.technical.status]}</p>
        <ul>{[...(review.technical.checks ?? []), ...(review.technical.findings ?? [])].map((finding, index) => <li key={index}>{OUTPUT_CHECK_LABELS[finding] ?? finding}</li>)}</ul>
        <p>内容检查：{REVIEW_LABELS[review.content.status]}</p>
        <ul>{[...(review.content.checks ?? []), ...(review.content.findings ?? []), ...review.findings].map((finding, index) => <li key={index}>{OUTPUT_CHECK_LABELS[finding] ?? finding}</li>)}</ul>
        <button type="button" className="tap-button" onClick={() => locate(review.compose_node_id)}>查看合成节点</button>
        {review.status !== 'passed' ? <>
          <label className="flex items-center gap-2"><input type="checkbox" checked={viewedOutputs.has(key)} disabled={Boolean(busy)} onChange={(event) => {
            const checked = event.currentTarget.checked;
            setViewedOutputs((prior) => { const next = new Set(prior); if (checked) next.add(key); else next.delete(key); return next; });
          }} />已完整观看并试听</label>
          <button type="button" className="tap-button tap-button-quiet-primary" disabled={!connected || Boolean(busy) || !viewedOutputs.has(key) || review.technical.status !== 'passed'} onClick={() => void confirmOutput(review)}>{busy === review.review_ref.artifact_id ? '正在确认…' : '确认成片交付'}</button>
        </> : <p>成片已确认</p>}
      </div>;
    })}
    {(state.speech_corrections ?? []).map((preview) => <div className={panelClass} key={preview.preview_ref.artifact_id}>
      <p className="font-medium">实际语音校对</p><p className="text-xs">{preview.lexical_change ? '请试听对应素材，核对下面的文字纠正。' : '标点与格式规范化，保留原台词和真实时间。'}</p>
      {preview.target_text ? <p className="whitespace-pre-wrap">{preview.target_text}</p> : preview.cues?.map((cue) => <p key={cue.turn_id}>{cue.text}</p>)}
      {preview.source_url ? <audio controls preload="metadata" src={preview.source_url} className="w-full" aria-label="试听实际原声" /> : null}
      <ul className="space-y-1">{preview.corrections.map((row) => {
        const original = preview.original_words?.find((word) => word.id === row.word_id);
        return <li key={row.word_id}><span>{original ? `${original.text} → ${row.text}` : row.text}</span><p className="text-xs">{row.reason}</p></li>;
      })}</ul>
      {preview.blockers.map((blocker, index) => <p role="status" key={index}>{blocker}</p>)}
      <button type="button" className="tap-button" onClick={() => locate(preview.source_node_id || preview.node_id)}>查看原声素材</button>
      <button type="button" className="tap-button" onClick={() => locate(preview.node_id)}>查看作品方案</button>
      <button type="button" className="tap-button tap-button-quiet-primary" disabled={!connected || Boolean(busy) || preview.blockers.length > 0} onClick={() => void confirmSpeech(preview)}>{busy === preview.preview_ref.artifact_id ? '正在应用…' : '一次校对并应用'}</button>
    </div>)}
    </div>
    {error ? <div className={expanded ? panelClass : 'break-words'}><p role="alert">{error}</p></div> : null}
  </section>;
}
