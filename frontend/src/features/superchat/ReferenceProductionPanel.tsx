import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from '@/api/client';
import { BackendStatusError } from '@/lib/api-errors';
import { getReferenceProductionState, getReferenceProductionHistory,
  getReferenceProductionRecovery, resumeReferenceProduction, getReferenceConversationCards, type ReferenceConversationCard,
  type DirectorArtifactRef, type ReferenceProductionRecovery, type ReferenceProductionState } from '@/api/referenceDirector';
import type { ChatMessage } from './types';
import { productionCardRefs } from './referenceConversationCards';
import { useAuthStore } from '@/stores/auth-store';
import { ReferenceDirectorPanel } from './ReferenceDirectorPanel';
import { REFERENCE_ANALYSIS_STATUS_EVENT } from '@/features/freezone/referenceAnalysisEvents';
import { refreshProductionCanvas } from './productionCanvasRefresh';

interface Props {
  projectId: string | null;
  canvasId: string | null;
  agentId: string | null;
  connected: boolean;
  messages?: ChatMessage[];
  summaryTarget?: HTMLElement | null;
  onCurrentState?: (state: ReferenceProductionState | null) => void;
  conversationMode?: boolean;
}
const HISTORY_LABELS: Record<string, string> = {
  'material_work_package.v1': '素材清单', 'material_review.v1': '素材审阅',
  'production_plan.v2': '作品方案', 'production_output_review.v1': '成片审阅',
  'speech_correction_preview.v1': '对白校正', 'reference_decision.v1': '制作选择',
};

const artifactKey = (ref: DirectorArtifactRef) => JSON.stringify([ref.artifact_id, ref.revision, ref.project_id ?? null]);

export function ReferenceProductionPanel(props: Props) {
  const { projectId, canvasId, agentId, connected } = props;
  const account = useAuthStore((state) => state.username);
  const [cardPages, setCardPages] = useState(1);
  const [cardLoading, setCardLoading] = useState(false);
  const [cards, setCards] = useState<ReferenceConversationCard[]>([]);
  const [cardError, setCardError] = useState<string | null>(null);
  const [branches, setBranches] = useState<ReferenceProductionState[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [legacy, setLegacy] = useState(false);
  const [busy, setBusy] = useState(false);
  const [history, setHistory] = useState<Array<{ schema: string; revision: number }>>([]);
  const [historyShown, setHistoryShown] = useState(false);
  const [historyCursor, setHistoryCursor] = useState<string | null>(null);
  const [recoveries, setRecoveries] = useState<ReferenceProductionRecovery['items']>([]);
  const [recoverySelected, setRecoverySelected] = useState<string | null>(null);
  const [resumePending, setResumePending] = useState<string | null>(null);
  const resumeCommands = useRef(new Map<string, string>());
  const branchesRef = useRef(branches);
  branchesRef.current = branches;
  const request = useRef(0);
  const epoch = useRef(0);
  const selectionEpoch = useRef(0);
  const canvasQueue = useRef<Promise<void>>(Promise.resolve());
  const callback = useRef(props.onCurrentState);
  callback.current = props.onCurrentState;
  const scope = JSON.stringify([account, projectId, canvasId, agentId?.trim() || 'main']);
  const resumeCommandScope = useRef(scope);
  if (resumeCommandScope.current !== scope) { resumeCommands.current.clear(); resumeCommandScope.current = scope; }
  const scopeRef = useRef(scope);
  if (scopeRef.current !== scope) { epoch.current += 1; scopeRef.current = scope; }
  const selectedRef = useRef(selected);
  const current = branches.find((row) => row.commission_ref.artifact_id === selected) ?? null;
  const recovery = recoveries.find((row) => row.commission_ref.artifact_id === recoverySelected);
  const selectionKey = JSON.stringify([selected, recoverySelected, current ? artifactKey(current.commission_ref) : null,
    recovery ? artifactKey(recovery.commission_ref) : null]);
  const selectionRef = useRef(selectionKey);
  if (selectionRef.current !== selectionKey) { selectionEpoch.current += 1; selectionRef.current = selectionKey; }
  selectedRef.current = selected;
  useEffect(() => { callback.current?.(current); }, [current]);
  const cardRefs = projectId && canvasId && current ? productionCardRefs(props.messages ?? [], projectId, canvasId,
    agentId?.trim() || 'main', current.commission_ref.artifact_id) : [];
  const visibleCardRefs = cardRefs.slice(-cardPages * 20);
  const cardReadKey = JSON.stringify([scope, selectionKey, current?.projection_revision, visibleCardRefs]);
  useEffect(() => { setCardPages(1); }, [scope, selected]);
  useEffect(() => {
    let cancelled = false;
    setCards([]); setCardError(null);
    if (!projectId || !canvasId || !current || !connected || !cardRefs.length) return;
    const generation = epoch.current;
    const branchGeneration = selectionEpoch.current;
    setCardLoading(true);
    const isCurrent = () => !cancelled && generation === epoch.current && branchGeneration === selectionEpoch.current && scopeRef.current === scope;
    const readPages = async () => {
      const items: ReferenceConversationCard[] = [];
      for (let offset = 0; offset < visibleCardRefs.length; offset += 20) {
        if (!isCurrent()) return;
        const batch = visibleCardRefs.slice(offset, offset + 20);
        const result = await getReferenceConversationCards(projectId, canvasId, current.commission_ref.artifact_id, batch, agentId?.trim() || 'main');
        if (!isCurrent()) return;
        if (result.scope.project_id !== projectId || result.scope.canvas_id !== canvasId || result.scope.agent_id !== (agentId?.trim() || 'main')
          || result.commission_ref.artifact_id !== current.commission_ref.artifact_id
          || result.items.some((card) => !batch.some((ref) => ref.artifact_id === card.card_id)
            || card.commission_ref.artifact_id !== current.commission_ref.artifact_id
            || card.ref.project_id && card.ref.project_id !== projectId)) throw new Error('历史卡片范围已变化，请刷新');
        items.push(...result.items);
      }
      if (isCurrent()) setCards(items);
    };
    void readPages().catch((caught) => {
      if (isCurrent()) setCardError(caught instanceof Error ? caught.message : String(caught));
    }).finally(() => { if (isCurrent()) setCardLoading(false); });
    return () => { cancelled = true; };
  }, [cardReadKey, connected]);
  const refresh = useCallback(async () => {
    if (!projectId || !canvasId || !connected) return;
    const generation = epoch.current;
    const sequence = ++request.current;
    try {
      const response = await getReferenceProductionState(projectId, canvasId, undefined, agentId?.trim() || 'main');
      if (scopeRef.current !== scope || epoch.current !== generation || request.current !== sequence) return;
      const next = 'branches' in response ? response.branches : [response];
      if (next.some((row) => row.scope.project_id !== projectId || row.scope.canvas_id !== canvasId
        || row.scope.agent_id !== (agentId?.trim() || 'main'))) throw new Error('制作状态范围已变化，请刷新');
      for (const row of next) {
        if (row.execution_status !== 'stopped') resumeCommands.current.delete(artifactKey(row.commission_ref));
      }
      branchesRef.current = next;
      setBranches(next);
      setRecoveries([]); setRecoverySelected(null);
      setResumePending((pending) => next.some((row) => row.commission_ref.artifact_id === pending
        && row.execution_status === 'stopped') ? pending : null);
      setSelected((previous) => next.some((row) => row.commission_ref.artifact_id === previous)
        ? previous : next.length === 1 ? next[0].commission_ref.artifact_id : null);
      setError(null);
      setLegacy(false);
    } catch (caught) {
      if (scopeRef.current !== scope || epoch.current !== generation || request.current !== sequence) return;
      if ((caught instanceof ApiError || caught instanceof BackendStatusError) && caught.status === 404) {
        setLegacy(true); setRecoveries([]); setResumePending(null);
      }
      else if ((caught instanceof ApiError || caught instanceof BackendStatusError) && caught.status === 409) {
        setError('制作状态正在更新，请稍后再试。');
        if (branchesRef.current.length) return;
        try {
          const recovery = await getReferenceProductionRecovery(projectId, canvasId, agentId?.trim() || 'main');
          if (scopeRef.current !== scope || epoch.current !== generation || request.current !== sequence) return;
          if (recovery.scope.project_id !== projectId || recovery.scope.canvas_id !== canvasId
            || recovery.scope.agent_id !== (agentId?.trim() || 'main')) throw new Error('作品方案范围已变化，请刷新');
          setRecoveries(recovery.items);
          setRecoverySelected((previous) => recovery.items.some((row) => row.commission_ref.artifact_id === previous) ? previous : null);
          if (recovery.items.length) setError(null);
        } catch (recoveryError) {
          if (scopeRef.current !== scope || epoch.current !== generation || request.current !== sequence) return;
          setRecoveries([]); setRecoverySelected(null); setResumePending(null);
          setError(recoveryError instanceof Error ? recoveryError.message : String(recoveryError));
        }
      } else {
        setRecoveries([]); setRecoverySelected(null); setResumePending(null);
        setError(caught instanceof Error ? caught.message : String(caught));
      }
    }
  }, [projectId, canvasId, agentId, connected, scope]);
  useEffect(() => {
    request.current += 1;
    const generation = ++epoch.current;
    const isCurrent = () => scopeRef.current === scope && epoch.current === generation;
    canvasQueue.current = Promise.resolve();
    setBranches([]); setSelected(null); setError(null); setLegacy(false); setBusy(false);
    branchesRef.current = [];
    setRecoveries([]); setRecoverySelected(null); setResumePending(null);
    setHistory([]); setHistoryShown(false); setHistoryCursor(null);
    callback.current?.(null);
    void refresh();
    const status = (event: Event) => {
      const detail = (event as CustomEvent).detail;
      const run = detail?.run ?? detail;
      if (run?.project_id === projectId && run?.canvas_id === canvasId
        && (!run.agent_id || run.agent_id === (agentId?.trim() || 'main'))) {
        if (detail?.canvas_changed === true && projectId && canvasId) {
          request.current += 1;
          canvasQueue.current = canvasQueue.current.then(async () => {
            if (!isCurrent()) return;
            await refreshProductionCanvas(projectId, canvasId, isCurrent);
            if (isCurrent()) await refresh();
          }).catch((caught) => {
            if (isCurrent()) setError(caught instanceof Error ? caught.message : String(caught));
          });
        } else void refresh();
      }
    };
    window.addEventListener('reference-director-status', status);
    window.addEventListener(REFERENCE_ANALYSIS_STATUS_EVENT, status);
    return () => {
      request.current += 1; epoch.current += 1;
      window.removeEventListener('reference-director-status', status);
      window.removeEventListener(REFERENCE_ANALYSIS_STATUS_EVENT, status);
    };
  }, [scope, connected, refresh, projectId, canvasId, agentId]);
  useEffect(() => { setHistory([]); setHistoryShown(false); setHistoryCursor(null); setBusy(false); setError(null); }, [selected]);
  useEffect(() => { setBusy(false); }, [selectionKey]);
  const resume = async (commissionRef: DirectorArtifactRef) => {
    if (!projectId || !canvasId || !connected || busy || resumePending) return;
    const generation = epoch.current;
    const branchGeneration = selectionEpoch.current;
    const isCurrent = () => scopeRef.current === scope && epoch.current === generation && selectionEpoch.current === branchGeneration;
    const key = artifactKey(commissionRef);
    let command = resumeCommands.current.get(key);
    if (!command) { command = 'resume-' + crypto.randomUUID(); resumeCommands.current.set(key, command); }
    setBusy(true); setError(null);
    try {
      const result = await resumeReferenceProduction(projectId, canvasId, {
        commission_ref: commissionRef, command_id: command, agent_id: agentId?.trim() || 'main',
      });
      if (!isCurrent()) return;
      if (result.accepted !== true || result.commission_ref.artifact_id !== commissionRef.artifact_id
        || (result.commission_ref.project_id !== undefined && result.commission_ref.project_id !== projectId)
        || !Number.isSafeInteger(result.commission_ref.revision) || result.commission_ref.revision < commissionRef.revision
        || result.scope.project_id !== projectId || result.scope.canvas_id !== canvasId
        || result.scope.agent_id !== (agentId?.trim() || 'main')) throw new Error('作品方案范围已变化，请刷新');
      setResumePending(commissionRef.artifact_id);
      await refresh();
    } catch (caught) { if (isCurrent()) setError(caught instanceof Error ? caught.message : String(caught)); }
    finally { if (isCurrent()) setBusy(false); }
  };
  const loadHistory = async (more = false) => {
    if (!current || !projectId || !canvasId || busy) return;
    const commissionId = current.commission_ref.artifact_id;
    const generation = epoch.current;
    const branchGeneration = selectionEpoch.current;
    const isCurrent = () => scopeRef.current === scope && epoch.current === generation && selectionEpoch.current === branchGeneration;
    setBusy(true);
    try {
      const result = await getReferenceProductionHistory(projectId, canvasId, commissionId,
        more ? historyCursor ?? undefined : undefined, agentId?.trim() || 'main');
      if (!isCurrent() || selectedRef.current !== commissionId) return;
      setHistory((prior) => more ? [...prior, ...result.items] : result.items);
      setHistoryCursor(result.next_cursor); setHistoryShown(true);
    } catch (caught) { if (isCurrent()) setError(caught instanceof Error ? caught.message : String(caught)); }
    finally { if (isCurrent()) setBusy(false); }
  };
  if (legacy) return <ReferenceDirectorPanel key={scope} {...props} defaultExpanded={false} />;
  if (!branches.length && !recoveries.length && !error && !resumePending) return null;
  return <article aria-label="作品制作操作" className="space-y-2">
    {cards.filter((card) => card.schema !== 'material_work_package.v1'
      && card.schema !== 'material_review.v1'
      && !(['direction', 'plan'].includes(card.decision_kind || '')
      || (card.schema === 'reference_decision.v1' && ['复刻方向', '作品方案'].includes(card.title)))
      && (!props.conversationMode || card.status === 'confirmed' || card.status === 'recorded')).map((card) => <article aria-label={`${card.title} · 历史记录`} key={card.card_id}
      className="rounded-[var(--ui-radius-xl)] border border-[var(--ui-border-soft)] bg-[var(--ui-surface-panel)] p-3 text-sm space-y-1">
      <p className="font-medium">{card.title} · 历史记录</p><p>{card.summary}</p><p>{card.reason}</p>
    </article>)}
    {!props.conversationMode && cardRefs.length > visibleCardRefs.length ? <button type="button" className="tap-button" disabled={cardLoading || !connected} onClick={() => setCardPages((pages) => pages + 1)}>显示更早的制作卡片</button> : null}
    {cardError ? <p role="alert" className="text-sm">历史卡片暂不可核对：{cardError}</p> : null}
    {recoveries.length ? <div className="space-y-1 text-xs">
      <p className="text-text-muted">可选择已保存的作品方案继续制作。</p>
      <select aria-label="选择要恢复的作品方案" className="w-full rounded-md bg-[var(--ui-surface-field)] p-2 text-sm"
        value={recoverySelected ?? ''} disabled={busy || !!resumePending}
        onChange={(event) => { setRecoverySelected(event.target.value || null); setError(null); }}>
        <option value="">选择要恢复的作品方案</option>
        {recoveries.map((row, index) => <option key={row.commission_ref.artifact_id} value={row.commission_ref.artifact_id}>
          {row.goal || `作品方案 ${index + 1}`}
        </option>)}
      </select>
      <button type="button" className="tap-button text-xs" disabled={!recovery || busy || !connected || !!resumePending}
        onClick={() => recovery && void resume(recovery.commission_ref)}>恢复这份方案</button>
    </div> : null}
    {resumePending ? <p role="status" className="text-xs text-text-muted">已提交继续请求，正在更新制作状态…</p> : null}
    {branches.length > 1 ? <select aria-label="选择作品方案进度" className="w-full rounded-md bg-[var(--ui-surface-field)] p-2 text-sm"
      value={selected ?? ''} onChange={(event) => setSelected(event.target.value || null)}>
      <option value="">选择要查看的作品方案</option>
      {branches.map((row, index) => <option key={row.commission_ref.artifact_id} value={row.commission_ref.artifact_id}>
        {row.director_state.commissions[0]?.goal || `作品方案 ${index + 1}`}
      </option>)}
    </select> : null}
    {current ? <>
      <ReferenceDirectorPanel key={`${scope}:${current.commission_ref.artifact_id}`} {...props} controlledState={current.director_state} progress={current}
        onRefresh={refresh} defaultExpanded={false}
        preferenceKey={account ? `production-expanded:${scope}:${current.commission_ref.artifact_id}` : undefined} />
      {current.execution_status === 'stopped' ? <button type="button" className="tap-button text-xs"
        disabled={busy || !connected || !!resumePending} onClick={() => void resume(current.commission_ref)}>继续当前方案</button> : null}
      {!props.conversationMode ? <button type="button" className="tap-button text-xs" disabled={busy}
        onClick={() => historyShown ? setHistoryShown(false) : void loadHistory()}>
        {historyShown ? '收起历史记录' : '查看历史记录'}
      </button> : null}
      {!props.conversationMode && historyShown ? <div aria-label="制作历史记录" className="max-h-40 overflow-y-auto space-y-1 text-xs text-text-muted">
        {history.map((row, index) => <p key={index}>{HISTORY_LABELS[row.schema] || '制作记录'} · 版本 {row.revision}</p>)}
        {!history.length ? <p>暂无历史记录</p> : null}
        {historyCursor ? <button className="tap-button" disabled={busy} onClick={() => void loadHistory(true)}>加载更多</button> : null}
      </div> : null}
    </> : null}
    {error ? <p role="alert" className="text-xs text-warning">{error}</p> : null}
    {!current && (error || resumePending || recoveries.length) ? <button type="button" className="tap-button text-xs"
      disabled={busy || !connected} onClick={() => void refresh()}>刷新制作状态</button> : null}
  </article>;
}
