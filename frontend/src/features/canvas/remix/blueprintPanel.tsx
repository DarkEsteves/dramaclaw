import { forwardRef, useEffect, useImperativeHandle, useState } from 'react';
import { createRemixArtifact, fetchRemixArtifact, fetchRemixOperation, reviseRemixArtifact, submitRemixProcessAction, type RemixArtifactRef } from '@/api/remix';
import type { VideoStoryRow } from '@/features/canvas/domain/canvasNodes';
import { readUrl } from '@/lib/url-params';
import { blueprintReadinessErrors, buildReferenceBlueprint, referenceEventsFromBlueprint, type BlueprintSources } from './blueprintModel';
import type { ReferenceEvent } from './ReferenceEventList';

interface BlueprintDraft {
  referenceEvidenceRef?: RemixArtifactRef | null;
  referenceBlueprintRef?: RemixArtifactRef | null;
  evidenceOperationId?: string | null;
  managedEvidenceOperationId?: string | null;
  sourceMediaPath?: string | null;
  blueprintNarrativeRole?: string;
  blueprintPreserveRule?: string;
  blueprintReplaceRule?: string;
  blueprintRevision?: number;
  blueprintDirty?: boolean;
}

interface Props {
  nodeId: string;
  source: BlueprintSources | null;
  rows: VideoStoryRow[];
  referenceEvents?: ReferenceEvent[];
  draft: BlueprintDraft;
  onPatch: (patch: Partial<BlueprintDraft>) => void;
  onReanalyze: () => void;
  suggestedNarrativeRole?: string;
  seedRef?: RemixArtifactRef | null;
}

export interface ReferenceBlueprintPanelHandle {
  ensureSaved: () => Promise<RemixArtifactRef>;
}

export const ReferenceBlueprintPanel = forwardRef<ReferenceBlueprintPanelHandle, Props>(function ReferenceBlueprintPanel({ nodeId, source, rows, referenceEvents, draft, onPatch, onReanalyze, suggestedNarrativeRole, seedRef }, ref) {
  const project = readUrl().project;
  const [operationId, setOperationId] = useState<string | null>(draft.evidenceOperationId ?? null);
  const [role, setRole] = useState(draft.blueprintNarrativeRole ?? '');
  const [preserve, setPreserve] = useState(draft.blueprintPreserveRule ?? '');
  const [replace, setReplace] = useState(draft.blueprintReplaceRule ?? '');
  const [busy, setBusy] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const evidenceRef = draft.sourceMediaPath && source?.mediaPath !== draft.sourceMediaPath ? null : draft.referenceEvidenceRef;

  useEffect(() => {
    const restoreRef = draft.referenceBlueprintRef ?? seedRef;
    if (!project || !restoreRef || draft.blueprintDirty) return;
    let active = true;
    fetchRemixArtifact(project, restoreRef).then((artifact) => {
      if (!active || artifact.schema !== 'reference_blueprint.v1') return;
      const payload = artifact.payload;
      const restoredEvents = referenceEventsFromBlueprint(payload);
      const patch: Partial<BlueprintDraft> & { referenceEvents?: ReferenceEvent[] } = {};
      if (typeof payload.narrative_role === 'string' && !draft.blueprintNarrativeRole) { setRole(payload.narrative_role); patch.blueprintNarrativeRole = payload.narrative_role; }
      const choices = Array.isArray(payload.user_choices) ? payload.user_choices : [];
      const preserveChoice = choices.find((choice) => choice && typeof choice === 'object' && (choice as Record<string, unknown>).type === 'preserve') as Record<string, unknown> | undefined;
      const replaceChoice = choices.find((choice) => choice && typeof choice === 'object' && (choice as Record<string, unknown>).type === 'replace') as Record<string, unknown> | undefined;
      if (typeof preserveChoice?.text === 'string' && !draft.blueprintPreserveRule) { setPreserve(preserveChoice.text); patch.blueprintPreserveRule = preserveChoice.text; }
      if (typeof replaceChoice?.text === 'string' && !draft.blueprintReplaceRule) { setReplace(replaceChoice.text); patch.blueprintReplaceRule = replaceChoice.text; }
      if ((!referenceEvents || referenceEvents.length === 0) && restoredEvents.length) patch.referenceEvents = restoredEvents;
      if (Object.keys(patch).length) onPatch(patch);
    }).catch((caught) => { if (active) setError(caught instanceof Error ? caught.message : String(caught)); });
    return () => { active = false; };
  }, [draft.blueprintDirty, draft.referenceBlueprintRef?.artifactId, draft.referenceBlueprintRef?.revision, project, seedRef?.artifactId, seedRef?.revision]);

  const analyzeEvidence = async () => {
    if (!project || !source || busy || operationId || draft.managedEvidenceOperationId) return;
    setBusy(true); setError(null);
    try {
      const revision = (draft.blueprintRevision ?? 0) + 1;
      const task = await submitRemixProcessAction(project, { action: 'analyze_reference_video', mediaPath: source.mediaPath, idempotencyKey: crypto.randomUUID(), nodeRevision: revision });
      onPatch({ evidenceOperationId: task.operation_id, sourceMediaPath: source.mediaPath, blueprintRevision: revision, referenceEvidenceRef: null, referenceBlueprintRef: null });
      setOperationId(task.operation_id);
    } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)); }
    finally { setBusy(false); }
  };

  useEffect(() => {
    if (!project || !operationId) return;
    let cancelled = false;
    let checking = false;
    const check = async () => {
      if (checking) return;
      checking = true;
      try {
        const operation = await fetchRemixOperation(project, operationId);
        if (cancelled) return;
        if (operation.status === 'failed' || operation.status === 'cancelled') throw new Error('原片证据提取失败');
        if (operation.status !== 'needs_confirmation' && operation.status !== 'completed') return;
        const output = operation.output_refs.find((item) => item.artifact_id && item.revision);
        if (!output?.artifact_id || !output.revision) throw new Error('证据提取没有返回产物');
        const artifact = await fetchRemixArtifact(project, { artifactId: output.artifact_id, revision: output.revision });
        if (cancelled) return;
        if (artifact.schema !== 'reference_evidence.v1') throw new Error('原片证据格式错误');
        onPatch({ referenceEvidenceRef: { artifactId: artifact.id, revision: artifact.revision }, evidenceOperationId: null, referenceBlueprintRef: null });
        setOperationId(null);
      } catch (caught) {
        if (!cancelled) { setError(caught instanceof Error ? caught.message : String(caught)); onPatch({ evidenceOperationId: null }); setOperationId(null); }
      } finally { checking = false; }
    };
    void check();
    const timer = window.setInterval(() => void check(), 3_000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [nodeId, operationId, project, onPatch]);

  const ensureSaved = async (): Promise<RemixArtifactRef> => {
    if (!project || !source) throw new Error('请先连接已上传的原片视频。');
    if (!evidenceRef) throw new Error('请先完成原片证据提取。');
    if (busy) throw new Error('参考蓝图正在处理，请稍后再试。');
    if (draft.referenceBlueprintRef && !dirty && !draft.blueprintDirty) {
      setBusy(true); setError(null);
      try {
        const artifact = await fetchRemixArtifact(project, draft.referenceBlueprintRef);
        if (artifact.id !== draft.referenceBlueprintRef.artifactId || artifact.revision !== draft.referenceBlueprintRef.revision || artifact.schema !== 'reference_blueprint.v1') throw new Error('已保存的参考蓝图版本无效。');
        const blockers = blueprintReadinessErrors(artifact.payload, {
          evidenceRef,
          transcriptRef: source.transcriptRef,
          upstreamRefs: artifact.upstream_refs,
        });
        if (blockers.length) throw new Error(blockers.join('；'));
        return draft.referenceBlueprintRef;
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : String(caught));
        throw caught;
      } finally {
        setBusy(false);
      }
    }
    const narrativeRole = role.trim() || suggestedNarrativeRole?.trim() || '';
    if (!narrativeRole) throw new Error('缺少叙事作用，请在参考蓝图设置中填写。');
    setBusy(true); setError(null);
    try {
      const payload = buildReferenceBlueprint({ source, rows, referenceEvents, narrativeRole, preserveRule: preserve, replaceRule: replace, evidenceRef });
      const blockers = blueprintReadinessErrors(payload);
      if (blockers.length) throw new Error(blockers.join('；'));
      const upstream = [evidenceRef, ...(source.transcriptRef ? [source.transcriptRef] : [])];
      const artifact = draft.referenceBlueprintRef
        ? await reviseRemixArtifact(project, draft.referenceBlueprintRef, payload, upstream)
        : await createRemixArtifact(project, 'reference_blueprint.v1', payload, upstream);
      const nextRef = { artifactId: artifact.id, revision: artifact.revision };
      onPatch({ referenceBlueprintRef: nextRef, blueprintNarrativeRole: narrativeRole, blueprintPreserveRule: preserve, blueprintReplaceRule: replace, blueprintDirty: false });
      if (!role.trim()) setRole(narrativeRole);
      setDirty(false);
      return nextRef;
    } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)); throw caught; }
    finally { setBusy(false); }
  };
  useImperativeHandle(ref, () => ({ ensureSaved }));

  const edit = (field: 'blueprintNarrativeRole' | 'blueprintPreserveRule' | 'blueprintReplaceRule', value: string, setter: (value: string) => void) => {
    setter(value);
    setDirty(true);
    onPatch({ [field]: value, blueprintDirty: true });
  };

  const labelClass = 'grid gap-1.5 text-[11px] font-medium text-text-muted';
  const fieldClass = 'ui-field w-full px-3 py-2 text-xs font-normal text-text-dark placeholder:text-[var(--canvas-node-input-placeholder)]';

  return <section className="ui-scrollbar nowheel grid gap-5 text-xs text-text-dark">
    <p className="rounded-[var(--ui-radius-lg)] bg-[rgb(var(--accent-rgb)/0.07)] px-3 py-2.5 leading-5 text-text-dark/78">这些规则只属于当前脚本分支，不会影响原片拆解或其他脚本节点。</p>
    <div className="grid gap-3">
      <div className="text-xs font-semibold text-text-dark">改编规则</div>
      <label className={labelClass}>整体叙事作用<input aria-label="整体叙事作用" value={role} onChange={(event) => edit('blueprintNarrativeRole', event.target.value, setRole)} className={fieldClass} /></label>
      <div className="grid gap-3 sm:grid-cols-2">
        <label className={labelClass}>保留规则<textarea aria-label="保留规则" value={preserve} onChange={(event) => edit('blueprintPreserveRule', event.target.value, setPreserve)} className={`${fieldClass} min-h-24 resize-y`} /></label>
        <label className={labelClass}>替换规则<textarea aria-label="替换规则" value={replace} onChange={(event) => edit('blueprintReplaceRule', event.target.value, setReplace)} className={`${fieldClass} min-h-24 resize-y`} /></label>
      </div>
    </div>
    <details className="rounded-[var(--ui-radius-lg)] border border-[var(--ui-border-soft)] bg-[rgba(var(--bg-rgb)/0.28)]">
      <summary className="cursor-pointer px-3 py-2.5 font-medium text-text-muted transition-colors hover:text-text-dark">查看生成依据</summary>
      <div className="grid gap-3 border-t border-[var(--ui-border-soft)] px-3 py-3">
        <div className="flex flex-wrap items-center gap-2">
          <strong className="font-medium">原片与分析产物</strong>
          <span className="break-all text-text-muted">{source ? `原片 ${source.mediaPath}` : '请连接已上传的原片视频'}</span>
          {source?.transcriptRef && <span className="text-text-muted">转写 r{source.transcriptRef.revision}</span>}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" disabled={!source || busy || Boolean(operationId || draft.managedEvidenceOperationId)} onClick={() => void analyzeEvidence()} className="tap-button">{operationId || draft.managedEvidenceOperationId ? '提取中…' : evidenceRef ? '重新提取原片证据' : '提取原片证据'}</button>
          <button type="button" disabled={!source || busy} onClick={onReanalyze} className="tap-button">重新分析画面</button>
        </div>
        <div className="flex flex-wrap gap-3 text-text-muted">
          {evidenceRef && <span>证据 {evidenceRef.artifactId} · r{evidenceRef.revision}</span>}
          {draft.referenceBlueprintRef && <span>改编规则版本 {draft.referenceBlueprintRef.artifactId} · r{draft.referenceBlueprintRef.revision}</span>}
        </div>
        <p className="text-text-muted">画面描述来自模型分析；这里用于查看生成来源，或在证据缺失时重新提取和分析。</p>
      </div>
    </details>
    {error && <p role="alert" className="text-red-300">{error}</p>}
  </section>;
});
