import { createPortal } from 'react-dom';
import { useAuthStore } from '@/stores/auth-store';
import { useEffect, useRef, useState } from 'react';
import { AlertCircle, CheckCircle2, Clock3, LoaderCircle, LocateFixed } from 'lucide-react';
import { listReferenceAnalysisRuns, retryReferenceAnalysisRun, type ReferenceAnalysisRun, type ReferenceAnalysisStatus as RunStatus } from '@/api/referenceAnalysisRuns';
import { REFERENCE_DIRECTOR_PHASE_LABELS, type ReferenceProductionState } from '@/api/referenceDirector';
import { getFreezoneCanvas } from '@/api/canvas';
import { applyRemoteFreezoneCanvas, captureFreezoneCanvasScope, getFreezoneCanvasBaseline } from '@/features/freezone/canvasSyncRuntime';
import { mergeReferenceAnalysisCanvas, mergeReferenceAnalysisMetadata, REFERENCE_ANALYSIS_STATUS_EVENT, referenceAnalysisRunMatchesScope, shouldRefreshReferenceAnalysis } from '@/features/freezone/referenceAnalysisEvents';
import { normalizeCanvasData, useCanvasStore, type CanvasNode } from '@/stores/canvasStore';

const LABELS: Record<RunStatus, string> = {
  ...REFERENCE_DIRECTOR_PHASE_LABELS,
  submitting: '正在提交原片分析',
  transcribing: '原片语音转写中',
  analyzing: '原片分析中',
  analysis_ready: '原片分析已完成，等待确认复刻方向',
  resuming: '虾导正在整理复刻建议',
  completed: '原片分析已完成，请查看虾导回复',
  analysis_failed: '原片分析失败',
  resume_failed: '原片分析已保留，虾导续接中断',
  stale: '原片或拆解状态需核对',
};
const FAILED = new Set<RunStatus>(['analysis_failed', 'resume_failed', 'stale']);
const ACTIVE = new Set<RunStatus>(['submitting', 'transcribing', 'analyzing', 'resuming', 'drafting_plan', 'awaiting_plan_review', 'preparing_materials', 'aligning_speech', 'composing', 'reviewing_output']);

function mergeRun(current: ReferenceAnalysisRun[], incoming: ReferenceAnalysisRun): ReferenceAnalysisRun[] {
  const existing = current.find((run) => run.run_id === incoming.run_id);
  if (existing && Date.parse(existing.updated_at) > Date.parse(incoming.updated_at)) return current;
  return [...current.filter((run) => run.run_id !== incoming.run_id), incoming]
    .sort((left, right) => Date.parse(right.created_at) - Date.parse(left.created_at)).slice(0, 5);
}

export function ReferenceAnalysisStatus({ projectId, canvasId, agentId, connected, currentProduction, summaryTarget, conversationMode = false }: {
  projectId: string | null; canvasId: string | null; agentId: string | null; connected: boolean;
  summaryTarget?: HTMLElement | null;
  currentProduction?: ReferenceProductionState | null;
  conversationMode?: boolean;
}) {
  const account = useAuthStore((state) => state.username);
  const [runs, setRuns] = useState<ReferenceAnalysisRun[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [retrying, setRetrying] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const scopeRef = useRef('');
  const epochRef = useRef(0);
  const refreshRef = useRef<(run: ReferenceAnalysisRun) => Promise<void>>(async () => {});
  const currentScope = `${account ?? ''}:${projectId ?? ''}:${canvasId ?? ''}:${agentId?.trim() || 'main'}`;
  scopeRef.current = currentScope;

  useEffect(() => {
    epochRef.current += 1;
    setRetrying(null);
    setRuns((current) => current.filter((run) => referenceAnalysisRunMatchesScope(run, projectId, canvasId, agentId)));
    setError(null);
    if (!projectId || !canvasId || !connected) return;
    let cancelled = false;
    let queue: Promise<void> = Promise.resolve();
    const refreshed = new Set<string>();
    const matches = (run: ReferenceAnalysisRun) => referenceAnalysisRunMatchesScope(run, projectId, canvasId, agentId);
    const refresh = (run: ReferenceAnalysisRun): Promise<void> => {
      if (!shouldRefreshReferenceAnalysis(run)) return queue;
      const key = `${run.run_id}:${run.status}:${run.updated_at}`;
      if (refreshed.has(key)) return queue;
      refreshed.add(key);
      const isCurrentCanvas = captureFreezoneCanvasScope(projectId, canvasId);
      queue = queue.then(async () => {
        if (cancelled) return;
        try {
          const remote = await getFreezoneCanvas(projectId, canvasId);
          if (cancelled || !isCurrentCanvas()) return;
          const baseline = getFreezoneCanvasBaseline(projectId, canvasId);
          if (!baseline) throw new Error('画布尚未完成载入，请稍后刷新状态查看结果。');
          if (typeof remote.revision === 'number' && baseline.revision !== null && baseline.revision > remote.revision) throw new Error('画布刚完成新的保存，请刷新状态查看分析结果。');
          const local = useCanvasStore.getState();
          const merged = mergeReferenceAnalysisCanvas((remote.nodes ?? []) as CanvasNode[], remote.edges as typeof local.edges ?? [], local.nodes, local.edges, run, baseline);
          // Check conflicts before apply() retires the old save session. Applying
          // a partial graph here would autosave away unrelated backend changes.
          const metadata = baseline.localMetadata !== undefined
            ? mergeReferenceAnalysisMetadata(baseline.metadata ?? null, baseline.localMetadata, remote.metadata ?? null)
            : remote.metadata;
          const normalizedRemote = { ...remote, ...normalizeCanvasData((remote.nodes ?? []) as CanvasNode[], (remote.edges ?? []) as typeof local.edges) };
          const applied = applyRemoteFreezoneCanvas(projectId, canvasId, normalizedRemote, () => baseline.localMetadata !== undefined ? { ...merged, metadata } : merged);
          if (!applied) throw new Error('画布同步暂不可用，请重新打开画布查看分析结果。');
          setError(null);
        } catch (caught) {
          refreshed.delete(key);
          if (!cancelled) setError(caught instanceof Error ? caught.message : String(caught));
        }
      });
      return queue;
    };
    refreshRef.current = refresh;
    const accept = (run: ReferenceAnalysisRun) => {
      if (cancelled || !matches(run)) return;
      setRuns((current) => mergeRun(current, run));
      void refresh(run);
    };
    const onStatus = (event: Event) => {
      const run = (event as CustomEvent<{ run?: ReferenceAnalysisRun }>).detail?.run;
      if (run) accept(run);
    };
    window.addEventListener(REFERENCE_ANALYSIS_STATUS_EVENT, onStatus);
    void listReferenceAnalysisRuns(projectId, canvasId, agentId?.trim() || 'main').then((restored) => {
      for (const run of restored.filter(matches).sort((left, right) => Date.parse(right.created_at) - Date.parse(left.created_at)).slice(0, 5)) accept(run);
    }).catch((caught) => { if (!cancelled) setError(caught instanceof Error ? caught.message : String(caught)); });
    return () => {
      cancelled = true;
      window.removeEventListener(REFERENCE_ANALYSIS_STATUS_EVENT, onStatus);
      refreshRef.current = async () => {};
    };
  }, [account, projectId, canvasId, agentId, connected, reload]);

  const retry = async (run: ReferenceAnalysisRun) => {
    if (!projectId || !canvasId || !connected || retrying || !['resume_failed', 'stale'].includes(run.status)) return;
    const scope = currentScope;
    const epoch = epochRef.current;
    setRetrying(run.run_id);
    try {
      const next = await retryReferenceAnalysisRun(projectId, canvasId, run.run_id);
      if (scopeRef.current !== scope || epochRef.current !== epoch) return;
      if (!referenceAnalysisRunMatchesScope(next, projectId, canvasId, agentId) || next.run_id !== run.run_id
        || next.source_node_id !== run.source_node_id || next.analysis_node_id !== run.analysis_node_id
        || run.task_id && next.task_id !== run.task_id
        || run.commission_ref && next.commission_ref?.artifact_id !== run.commission_ref.artifact_id)
        throw new Error('继续结果与原分析任务不匹配，请刷新状态。');
      setError(null);
      setRuns((current) => mergeRun(current, next));
      void refreshRef.current(next);
    } catch (caught) {
      if (scopeRef.current === scope && epochRef.current === epoch) setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      if (scopeRef.current === scope && epochRef.current === epoch) setRetrying(null);
    }
  };
  const locate = async (run: ReferenceAnalysisRun) => {
    const scope = currentScope;
    await refreshRef.current(run);
    if (scopeRef.current !== scope) return;
    const store = useCanvasStore.getState();
    if (!store.nodes.some((node) => node.id === run.analysis_node_id)) return;
    store.onNodesChange(store.nodes.map((node) => ({ id: node.id, type: 'select' as const, selected: node.id === run.analysis_node_id })));
    store.setSelectedNode(run.analysis_node_id);
    store.requestFocusNode(run.analysis_node_id);
  };

  const visibleRuns = runs.filter((run) => !(
    (!FAILED.has(run.status) || !!run.commission_ref && run.commission_ref.revision < (currentProduction?.commission_ref.revision ?? 0)) && currentProduction && currentProduction.phase !== 'awaiting_direction'
    && currentProduction.scope.project_id === projectId && currentProduction.scope.canvas_id === canvasId
    && currentProduction.scope.agent_id === (agentId?.trim() || 'main')
    && currentProduction.analysis_node_id === run.analysis_node_id
    && (!run.commission_ref || run.commission_ref.artifact_id === currentProduction.commission_ref.artifact_id)
  ));
  const displayedRuns = conversationMode ? visibleRuns.filter((run) => FAILED.has(run.status)) : visibleRuns;
  if (displayedRuns.length === 0 && !error) return null;
  return <div className="mb-2 space-y-2" aria-label="参考视频复刻状态">
    {summaryTarget && !conversationMode ? createPortal(<details><summary>原片分析 · {visibleRuns.length} 项</summary>{visibleRuns.map((run) => <p key={run.run_id}>{LABELS[FAILED.has(run.status) ? run.status : run.phase ?? run.status]}</p>)}</details>, summaryTarget) : null}
    {displayedRuns.map((run) => <article aria-label="参考视频制作恢复" key={run.run_id} className="rounded-[var(--ui-radius-xl)] border border-[var(--ui-border-soft)] bg-[var(--ui-surface-panel)] p-3 text-xs">
      <div className="flex items-center gap-2" role="status" aria-live="polite">
        {FAILED.has(run.status) ? <AlertCircle className="size-3.5 shrink-0 text-warning" /> : ACTIVE.has(run.phase ?? run.status) ? <LoaderCircle className="size-3.5 shrink-0 animate-spin text-primary" /> : run.status !== 'completed' && ['analysis_ready', 'awaiting_direction', 'awaiting_plan_confirmation'].includes(run.phase ?? run.status) ? <Clock3 className="size-3.5 shrink-0" /> : <CheckCircle2 className="size-3.5 shrink-0 text-success" />}
        <span>{LABELS[FAILED.has(run.status) ? run.status : run.phase ?? run.status]}</span>
      </div>
      {run.execution_blocker && run.execution_blocker.code !== 'user_required' ? <><p className="mt-1.5 text-text-muted">原片分析已完成</p><p className="mt-1.5 text-warning" role="alert">{run.execution_blocker.code === 'context_delivery_failed' ? '虾导读取失败' : run.execution_blocker.message}</p></> : null}
      {run.execution_blocker?.code === 'user_required' ? <p className="mt-1.5 text-text-muted">{run.execution_blocker.message}</p> : null}
      {run.status === 'transcribing' || run.status === 'analyzing' || run.status === 'submitting' ? <p className="mt-1.5 text-text-muted">完成后会通知虾导继续，重新打开画布也可查看进度。</p> : null}
      {FAILED.has(run.status) && run.error ? <p className="mt-1.5 break-words text-warning" role="alert">{run.error}</p> : null}
      <div className="mt-2 flex flex-wrap gap-3 text-primary">
        {run.analysis_node_id ? <button type="button" className="inline-flex items-center gap-1 hover:underline" onClick={() => void locate(run)}><LocateFixed className="size-3" />查看原片拆解</button> : null}
        {run.status === 'resume_failed' || run.status === 'stale' ? <button type="button" className="tap-button tap-button-quiet-primary" disabled={Boolean(retrying) || !connected} onClick={() => void retry(run)}>{retrying === run.run_id ? '正在恢复…' : run.status === 'stale' ? '核对并继续' : run.phase === 'drafting_plan' ? '继续准备方案' : run.execution_blocker ? '继续处理' : '继续制作流程'}</button> : null}
      </div>
    </article>)}
    {error ? <div className="rounded-xl border border-border p-3 text-xs text-warning"><p role="alert">{error}</p><button type="button" className="mt-2 text-primary hover:underline" onClick={() => setReload((value) => value + 1)}>刷新状态</button></div> : null}
  </div>;
}
