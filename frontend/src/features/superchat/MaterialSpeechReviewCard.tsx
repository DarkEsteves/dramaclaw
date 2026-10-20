import { useEffect, useRef, useState } from 'react';
import { getReferenceMaterialSpeechStatus, saveReferenceMaterialSpeechCorrection, type DirectorMaterialReview } from '@/api/referenceDirector';
import { MaterialSpeechReview, type MaterialSpeechCorrectionInput, type MaterialSpeechReviewSummary } from './MaterialSpeechReview';
import { useAuthStore } from '@/stores/auth-store';

/** Read saved evidence only; candidate analysis is admitted by the native workflow. */
export function MaterialSpeechReviewCard({ projectId, canvasId, agentId, review, active, refreshToken, onSeek, onSaved }: {
  projectId: string; canvasId: string; agentId: string; review: DirectorMaterialReview;
  active: boolean; refreshToken: unknown; onSeek: (seconds: number) => void; onSaved: () => Promise<void>;
}) {
  const account = useAuthStore((store) => store.username);
  const currentWorkRef = review.current_work_ref ?? review.work_ref;
  const [summary, setSummary] = useState<MaterialSpeechReviewSummary | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const epoch = useRef(0);
  const request = useRef(0);
  const evidenceEpoch = useRef(0);
  const evidenceKey = useRef<string | null>(null);
  const savedCommand = useRef<{ input: string; id: string } | null>(null);
  const identityScope = JSON.stringify([account, projectId, canvasId, agentId, review.review_ref, review.work_ref, review.step_id, review.resource]);
  const scope = JSON.stringify([identityScope, currentWorkRef]);
  const [verifiedScope, setVerifiedScope] = useState<string | null>(null);
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const activeRef = useRef(active);
  activeRef.current = active;
  useEffect(() => {
    epoch.current += 1;
    evidenceEpoch.current += 1; evidenceKey.current = null;
    setSummary(null); setMessage(null); setError(null); setBusy(false);
    return () => { epoch.current += 1; };
  }, [identityScope]);
  useEffect(() => { epoch.current += 1; setBusy(false); }, [scope]);
  const retireEvidence = () => {
    evidenceEpoch.current += 1; evidenceKey.current = null;
    setSummary(null); setBusy(false); setVerifiedScope(null);
  };
  useEffect(() => {
    if (!active) return;
    const generation = epoch.current;
    const sequence = ++request.current;
    const current = () => scopeRef.current === scope && epoch.current === generation && request.current === sequence;
    void getReferenceMaterialSpeechStatus(projectId, canvasId, {
      work_ref: currentWorkRef, step_id: review.step_id, node_id: review.resource.node_id, language: 'auto',
    }, agentId).then((result) => {
      if (!current()) return;
      if (result.status !== 'ready') { retireEvidence(); setMessage(result.message); return; }
      if (result.work_ref.artifact_id !== currentWorkRef.artifact_id || result.work_ref.revision !== currentWorkRef.revision
        || result.step_id !== review.step_id || result.node_id !== review.resource.node_id) {
        throw new Error('对白证据对应的素材已变化，请刷新制作状态。');
      }
      const nextEvidence = JSON.stringify([result.summary.observation_ref, result.summary.target_signature]);
      if (evidenceKey.current !== nextEvidence) { evidenceEpoch.current += 1; evidenceKey.current = nextEvidence; setBusy(false); }
      setSummary(result.summary); setMessage(null); setVerifiedScope(scope);
    }).catch((caught) => {
      if (current()) { retireEvidence(); setMessage(null); setError(caught instanceof Error ? caught.message : String(caught)); }
    });
    return () => { request.current += 1; };
  }, [scope, active, refreshToken]);
  const save = async (input: MaterialSpeechCorrectionInput) => {
    if (!activeRef.current || verifiedScope !== scope || busy || evidenceKey.current !== JSON.stringify([input.observation_ref, input.target_signature])) return;
    const generation = epoch.current;
    const evidenceGeneration = evidenceEpoch.current;
    const current = () => scopeRef.current === scope && epoch.current === generation && evidenceEpoch.current === evidenceGeneration;
    const serialized = JSON.stringify([scope, input]);
    if (savedCommand.current?.input !== serialized) savedCommand.current = { input: serialized, id: 'speech-' + crypto.randomUUID() };
    request.current += 1;
    setBusy(true); setError(null);
    try {
      const result = await saveReferenceMaterialSpeechCorrection(projectId, canvasId, { ...input,
        work_ref: currentWorkRef, step_id: review.step_id, node_id: review.resource.node_id,
        command_id: savedCommand.current.id });
      if (!current()) return;
      request.current += 1;
      if (result.summary.observation_ref.artifact_id !== input.observation_ref.artifact_id
        || result.summary.observation_ref.revision !== input.observation_ref.revision
        || result.summary.target_signature !== input.target_signature) throw new Error('校正结果与当前对白证据不匹配，请刷新制作状态。');
      setSummary(result.summary);
      await onSaved();
    } catch (caught) { if (current()) setError(caught instanceof Error ? caught.message : String(caught)); }
    finally { if (current()) setBusy(false); }
  };
  return <>
    {summary ? <MaterialSpeechReview summary={summary} onSave={save} onSeek={onSeek} busy={busy || !active || verifiedScope !== scope} error={error} />
      : error ? <p role="alert" className="text-warning text-xs">{error}</p> : null}
    {message ? <p role="status" className="text-xs text-text-muted">{message}</p> : null}
  </>;
}
