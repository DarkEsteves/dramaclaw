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
}

export interface ReferenceBlueprintPanelHandle {
  ensureSaved: () => Promise<RemixArtifactRef>;
}

export const ReferenceBlueprintPanel = forwardRef<ReferenceBlueprintPanelHandle, Props>(function ReferenceBlueprintPanel({ nodeId, source, rows, referenceEvents, draft, onPatch, onReanalyze, suggestedNarrativeRole }, ref) {
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
    if (!project || !draft.referenceBlueprintRef || draft.blueprintDirty) return;
    let active = true;
    fetchRemixArtifact(project, draft.referenceBlueprintRef).then((artifact) => {
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
  }, [draft.blueprintDirty, draft.referenceBlueprintRef?.artifactId, draft.referenceBlueprintRef?.revision, project]);

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

  return <section className="ui-scrollbar nowheel max-h-[220px] overflow-auto border-b border-white/[0.08] p-3 text-xs text-text-dark">
    <div className="mb-2 flex items-center gap-2">
      <strong>参考蓝图</strong>
      <span className="text-text-muted">{source ? `原片 ${source.mediaPath}` : '请连接已上传的原片视频'}</span>
      {source?.transcriptRef && <span className="text-text-muted">转写 r{source.transcriptRef.revision}</span>}
    </div>
    <div className="mb-2 flex flex-wrap gap-2">
      <button type="button" disabled={!source || busy || Boolean(operationId || draft.managedEvidenceOperationId)} onClick={() => void analyzeEvidence()} className="rounded border border-border-dark px-2 py-1 disabled:opacity-50">{operationId || draft.managedEvidenceOperationId ? '提取中…' : evidenceRef ? '重新提取原片证据' : '提取原片证据'}</button>
      <button type="button" disabled={!source || busy} onClick={onReanalyze} className="rounded border border-border-dark px-2 py-1 disabled:opacity-50">重新分析画面</button>
      {evidenceRef && <span className="text-text-muted">证据 {evidenceRef.artifactId} · r{evidenceRef.revision}</span>}
    </div>
    <p className="mb-2 text-text-muted">画面描述来自模型分析，请核对原片；下方规则会在生成改编脚本时自动保存。</p>
    <label className="mb-1 block">整体叙事作用<input aria-label="整体叙事作用" value={role} onChange={(event) => edit('blueprintNarrativeRole', event.target.value, setRole)} className="mt-1 w-full rounded border border-border-dark bg-surface-field p-1" /></label>
    <div className="grid grid-cols-2 gap-2">
      <label>保留规则<textarea aria-label="保留规则" value={preserve} onChange={(event) => edit('blueprintPreserveRule', event.target.value, setPreserve)} className="mt-1 w-full rounded border border-border-dark bg-surface-field p-1" /></label>
      <label>替换规则<textarea aria-label="替换规则" value={replace} onChange={(event) => edit('blueprintReplaceRule', event.target.value, setReplace)} className="mt-1 w-full rounded border border-border-dark bg-surface-field p-1" /></label>
    </div>
    {draft.referenceBlueprintRef && <div className="mt-2 text-text-muted">蓝图 {draft.referenceBlueprintRef.artifactId} · r{draft.referenceBlueprintRef.revision}</div>}
    {error && <p role="alert" className="mt-1 text-red-300">{error}</p>}
  </section>;
});
