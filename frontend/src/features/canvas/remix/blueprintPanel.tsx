import { useEffect, useState } from 'react';
import { createRemixArtifact, fetchRemixArtifact, fetchRemixOperation, submitRemixProcessAction, type RemixArtifactRef } from '@/api/remix';
import type { VideoStoryRow } from '@/features/canvas/domain/canvasNodes';
import { readUrl } from '@/lib/url-params';
import { buildReferenceBlueprint, type BlueprintSources } from './blueprintModel';

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
}

interface Props {
  nodeId: string;
  source: BlueprintSources | null;
  rows: VideoStoryRow[];
  draft: BlueprintDraft;
  onPatch: (patch: Partial<BlueprintDraft>) => void;
  onReanalyze: () => void;
}

export function ReferenceBlueprintPanel({ nodeId, source, rows, draft, onPatch, onReanalyze }: Props) {
  const project = readUrl().project;
  const [operationId, setOperationId] = useState<string | null>(draft.evidenceOperationId ?? null);
  const [role, setRole] = useState(draft.blueprintNarrativeRole ?? '');
  const [preserve, setPreserve] = useState(draft.blueprintPreserveRule ?? '');
  const [replace, setReplace] = useState(draft.blueprintReplaceRule ?? '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const evidenceRef = draft.sourceMediaPath && source?.mediaPath !== draft.sourceMediaPath ? null : draft.referenceEvidenceRef;

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

  const save = async () => {
    if (!project || !source || !evidenceRef || busy) return;
    if (!role.trim()) { setError('请先填写整体叙事作用'); return; }
    setBusy(true); setError(null);
    try {
      const payload = buildReferenceBlueprint({ source, rows, narrativeRole: role, preserveRule: preserve, replaceRule: replace, evidenceRef });
      const upstream = [evidenceRef, ...(source.transcriptRef ? [source.transcriptRef] : [])];
      const artifact = await createRemixArtifact(project, 'reference_blueprint.v1', payload, upstream);
      onPatch({ referenceBlueprintRef: { artifactId: artifact.id, revision: artifact.revision }, blueprintNarrativeRole: role, blueprintPreserveRule: preserve, blueprintReplaceRule: replace });
    } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)); }
    finally { setBusy(false); }
  };

  const edit = (field: 'blueprintNarrativeRole' | 'blueprintPreserveRule' | 'blueprintReplaceRule', value: string, setter: (value: string) => void) => {
    setter(value);
    onPatch({ [field]: value, referenceBlueprintRef: null });
  };

  return <section className="ui-scrollbar max-h-[220px] overflow-auto border-b border-white/[0.08] p-3 text-xs text-text-dark">
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
    <p className="mb-2 text-text-muted">画面描述来自模型分析，请核对原片；叙事解释和人工规则分别保存。</p>
    <label className="mb-1 block">整体叙事作用<input aria-label="整体叙事作用" value={role} onChange={(event) => edit('blueprintNarrativeRole', event.target.value, setRole)} className="mt-1 w-full rounded border border-border-dark bg-surface-field p-1" /></label>
    <div className="grid grid-cols-2 gap-2">
      <label>保留规则<textarea aria-label="保留规则" value={preserve} onChange={(event) => edit('blueprintPreserveRule', event.target.value, setPreserve)} className="mt-1 w-full rounded border border-border-dark bg-surface-field p-1" /></label>
      <label>替换规则<textarea aria-label="替换规则" value={replace} onChange={(event) => edit('blueprintReplaceRule', event.target.value, setReplace)} className="mt-1 w-full rounded border border-border-dark bg-surface-field p-1" /></label>
    </div>
    <div className="mt-2 flex items-center gap-2"><button type="button" disabled={!source || !evidenceRef || busy} onClick={() => void save()} className="rounded border border-border-dark px-2 py-1 disabled:opacity-50">保存参考蓝图</button>{draft.referenceBlueprintRef && <span>蓝图 {draft.referenceBlueprintRef.artifactId} · r{draft.referenceBlueprintRef.revision}</span>}</div>
    {error && <p role="alert" className="mt-1 text-red-300">{error}</p>}
  </section>;
}
