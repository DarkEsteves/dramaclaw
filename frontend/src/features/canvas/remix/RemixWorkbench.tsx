import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { confirmRemixOperation, createRemixArtifact, fetchRemixArtifact, fetchRemixOperation, fetchRemixTaskStatus, listRemixOperations, renderRemixManifest, submitRemixProcessAction, uploadRemixMedia, type RemixOperation } from '@/api/remix';
import { buildWorkbenchManifest, type RemixCaptionCue, type RemixWorkbenchDraft, type UploadedRemixMedia } from './workbenchModel';
import { applyRemoteFreezoneCanvas, flushFreezoneCanvasRuntime } from '@/features/freezone/canvasSyncRuntime';
import { getFreezoneCanvas } from '@/api/canvas';
import { useCanvasStore } from '@/stores/canvasStore';
import { captureCanvasBaseline, mergeConfirmedCanvasWithLocalChanges } from './workbenchCanvasMerge';
import { cuesFromWordTimeline, type WordTimelinePayload } from './transcriptionCues';

interface Props {
  project: string;
  canvasId: string;
  nodeId: string;
  initialDraft: RemixWorkbenchDraft;
  onPersist: (draft: RemixWorkbenchDraft) => void;
  onClose: () => void;
}

const fieldClass = 'rounded-md border border-border-dark bg-surface-field px-2 py-1.5 text-sm text-text-dark';

function mediaUrl(project: string, path: string): string {
  return `/api/v1/projects/${encodeURIComponent(project)}/media/${path.split('/').map(encodeURIComponent).join('/')}`;
}

function matchesCurrentDraft(operation: RemixOperation, draft: RemixWorkbenchDraft): boolean {
  const ref = draft.manifestRef;
  return Boolean(ref && operation.node_revision === draft.nodeRevision && operation.input_refs?.length === 1
    && operation.input_refs[0].artifact_id === ref.artifactId && operation.input_refs[0].revision === ref.revision);
}

export function RemixWorkbench({ project, canvasId, nodeId, initialDraft, onPersist, onClose }: Props) {
  const [draft, setDraft] = useState<RemixWorkbenchDraft>(initialDraft);
  const [activeOperationId, setActiveOperationId] = useState<string | null>(initialDraft.operationId ?? null);
  const [operationView, setOperationView] = useState<{ id: string; status: string; path?: string } | null>(null);
  const [history, setHistory] = useState<RemixOperation[]>([]);
  const [reconciling, setReconciling] = useState(false);
  const reconcilingRef = useRef(false);
  const submittingRef = useRef(false);
  const pendingUploadsRef = useRef(0);
  const draftRef = useRef(draft);
  const [busy, setBusy] = useState(false);
  const [savingOperationHandle, setSavingOperationHandle] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [language, setLanguage] = useState('zh');
  const transcription = draft.transcription;

  const save = (next: RemixWorkbenchDraft) => { draftRef.current = next; setDraft(next); onPersist(next); };
  const changed = (next: RemixWorkbenchDraft) => {
    if (reconcilingRef.current || submittingRef.current) return;
    setActiveOperationId(null);
    setOperationView(null);
    save({ ...next, manifestRef: undefined, operationId: undefined, resultPath: undefined, status: undefined, nodeRevision: (draftRef.current.nodeRevision ?? 0) + 1 });
  };
  const updateCue = (id: string, patch: Partial<RemixCaptionCue>) => changed({ ...draft, cues: draft.cues.map((cue) => cue.id === id ? { ...cue, ...patch } : cue) });
  const upload = async (file: File, kind: 'video' | 'audio') => {
    if (reconcilingRef.current || submittingRef.current || draftRef.current.transcription) return;
    pendingUploadsRef.current += 1;
    setBusy(true); setError(null);
    try {
      const media = await uploadRemixMedia(project, file) as UploadedRemixMedia;
      if (media.media_kind !== kind) throw new Error(kind === 'video' ? '请选择视频文件' : '请选择音频文件');
      changed({ ...draftRef.current, [kind]: media });
    } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)); }
    finally { pendingUploadsRef.current -= 1; setBusy(false); }
  };
  const uploadVisual = async (file: File, cueId: string) => {
    if (reconcilingRef.current || submittingRef.current || draftRef.current.transcription) return;
    pendingUploadsRef.current += 1;
    setBusy(true); setError(null);
    try {
      const media = await uploadRemixMedia(project, file) as UploadedRemixMedia;
      if (media.media_kind !== 'video') throw new Error('替换画面需要视频文件');
      changed({ ...draftRef.current, cues: draftRef.current.cues.map((cue) => cue.id === cueId ? { ...cue, visual: media } : cue) });
    } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)); }
    finally { pendingUploadsRef.current -= 1; setBusy(false); }
  };

  const transcribe = async () => {
    if (busy || reconcilingRef.current || submittingRef.current || draftRef.current.transcription || pendingUploadsRef.current > 0) return;
    const current = draftRef.current;
    const narration = current.audio ?? (current.video?.has_audio ? current.video : undefined);
    if (!narration) { setError('请先上传带声音的视频或旁白音频'); return; }
    if (current.cues.length > 0 && !window.confirm('自动识别会替换现有台词、关键时刻和替换画面，确定继续吗？')) return;
    setBusy(true); setError(null);
    try {
      const nodeRevision = (current.nodeRevision ?? 0) + 1;
      const task = await submitRemixProcessAction(project, { action: 'transcribe_media', mediaPath: narration.media_path, language, idempotencyKey: crypto.randomUUID(), nodeRevision });
      save({ ...draftRef.current, transcription: { operationId: task.operation_id, mediaPath: narration.media_path, nodeRevision } });
      if (await flushFreezoneCanvasRuntime(project, canvasId) !== true) throw new Error('识别任务已提交，但画布尚未保存；请等待同步后重试');
    } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)); }
    finally { setBusy(false); }
  };

  useEffect(() => {
    if (!transcription) return;
    let cancelled = false;
    let checking = false;
    const check = async () => {
      if (checking) return;
      checking = true;
      let terminal = false;
      try {
        const operation = await fetchRemixOperation(project, transcription.operationId);
        if (cancelled || draftRef.current.transcription?.operationId !== transcription.operationId) return;
        if (operation.status === 'failed' || operation.status === 'cancelled') {
          terminal = true;
          throw new Error(`自动识别${operation.status === 'failed' ? '失败' : '已取消'}`);
        }
        const ref = operation.output_refs.find((item) => item.artifact_id && item.revision);
        if (operation.status === 'needs_confirmation' && ref?.artifact_id && ref.revision) {
          const artifact = await fetchRemixArtifact(project, { artifactId: ref.artifact_id, revision: ref.revision });
          if (cancelled || draftRef.current.transcription?.operationId !== transcription.operationId) return;
          if (artifact.schema !== 'word_timeline.v1') { terminal = true; throw new Error('语音识别结果格式错误'); }
          const current = draftRef.current;
          const media = current.audio ?? (current.video?.has_audio ? current.video : undefined);
          if (!media || media.media_path !== transcription.mediaPath) { terminal = true; throw new Error('音频已变更，请重新识别'); }
          const cues = cuesFromWordTimeline(artifact.payload as unknown as WordTimelinePayload, Math.min(media.duration_us, current.video?.duration_us ?? media.duration_us));
          if (!cues.length) { terminal = true; throw new Error('没有识别到人声，请手动添加台词'); }
          changed({ ...current, cues, transcription: undefined });
        } else if (operation.status === 'pending' && operation.task_type && operation.task_scope) {
          const task = await fetchRemixTaskStatus(project, operation.task_type, operation.task_scope);
          if (task?.status === 'failed' || task?.status === 'cancelled') { terminal = true; throw new Error(task.error ?? '自动识别失败'); }
        }
      } catch (caught) {
        if (!cancelled && draftRef.current.transcription?.operationId === transcription.operationId) {
          setError(caught instanceof Error ? caught.message : String(caught));
          if (terminal) save({ ...draftRef.current, transcription: undefined });
        }
      } finally { checking = false; }
    };
    void check();
    const timer = window.setInterval(() => void check(), 3_000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [project, transcription?.operationId, transcription?.mediaPath]);

  const submit = async () => {
    if (submittingRef.current || reconcilingRef.current || pendingUploadsRef.current > 0 || draftRef.current.transcription) return;
    submittingRef.current = true;
    setBusy(true); setError(null);
    try {
      if (!draft.video) throw new Error('请先上传参考视频');
      const narration = draft.audio ?? (draft.video.has_audio ? draft.video : undefined);
      if (!narration) throw new Error('视频没有音轨，请上传旁白音频');
      const manifest = buildWorkbenchManifest(draft.video, narration, draft.cues);
      const artifact = await createRemixArtifact(project, 'composition_manifest.v1', manifest, []);
      const manifestRef = { artifactId: artifact.id, revision: artifact.revision };
      const nodeRevision = (draft.nodeRevision ?? 0) + 1;
      save({ ...draft, manifestRef, nodeRevision, status: 'submitting', resultPath: undefined });
      if (await flushFreezoneCanvasRuntime(project, canvasId) !== true) throw new Error('画布尚未保存，请稍后重试');
      const operation = await renderRemixManifest(project, { manifestRef, idempotencyKey: crypto.randomUUID(), nodeRevision, canvasId, nodeId });
      setSavingOperationHandle(true);
      save({ ...draft, manifestRef, nodeRevision, operationId: operation.operation_id, status: 'pending', resultPath: undefined });
      if (await flushFreezoneCanvasRuntime(project, canvasId) !== true) throw new Error('任务已提交，但画布尚未保存；请刷新后从任务记录恢复');
      setActiveOperationId(operation.operation_id);
      setOperationView({ id: operation.operation_id, status: 'pending' });
      setSavingOperationHandle(false);
    } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)); }
    finally { submittingRef.current = false; setBusy(false); }
  };

  useEffect(() => {
    let cancelled = false;
    void listRemixOperations(project).then(({ items }) => {
      if (cancelled) return;
      const own = items.filter((item) => item.canvas_id === canvasId && item.node_id === nodeId && item.task_type === 'remix_render');
      setHistory(own);
      if (!draft.operationId) {
        const recovered = own.find((item) => matchesCurrentDraft(item, draft));
        if (recovered) setActiveOperationId((current) => current ?? recovered.operation_id);
      }
    }).catch((caught) => { if (!cancelled) setError(caught instanceof Error ? caught.message : String(caught)); });
    return () => { cancelled = true; };
  }, [project, canvasId, nodeId, draft.operationId, draft.manifestRef?.artifactId, draft.manifestRef?.revision, draft.nodeRevision]);

  useEffect(() => {
    if (!activeOperationId || savingOperationHandle || operationView?.path || operationView?.status === 'failed' || operationView?.status === 'cancelled') return;
    let cancelled = false;
    const check = async () => {
      try {
        const operation = await fetchRemixOperation(project, activeOperationId);
        if (cancelled) return;
        const path = operation.output_refs.find((ref) => ref.project_path)?.project_path;
        let status = operation.status;
        let confirmedHere = false;
        if (status === 'needs_confirmation' && path && matchesCurrentDraft(operation, draftRef.current)) {
          if (pendingUploadsRef.current > 0) return;
          reconcilingRef.current = true;
          setReconciling(true);
          try {
            if (await flushFreezoneCanvasRuntime(project, canvasId) !== true) throw new Error('画布尚未保存，稍后重试结果确认');
            if (cancelled) return;
            const beforeConfirm = useCanvasStore.getState();
            const baseline = captureCanvasBaseline(beforeConfirm.nodes, beforeConfirm.edges);
            status = (await confirmRemixOperation(project, activeOperationId, canvasId, nodeId)).status;
            confirmedHere = status === 'completed';
            if (confirmedHere) {
              const remote = await getFreezoneCanvas(project, canvasId);
              if (!applyRemoteFreezoneCanvas(project, canvasId, remote, (remoteNodes, remoteEdges, localNodes, localEdges) =>
                mergeConfirmedCanvasWithLocalChanges(remoteNodes, remoteEdges, localNodes, localEdges, baseline, nodeId))) {
                throw new Error('画布同步暂不可用，请重新打开后查看结果');
              }
            }
          } finally {
            reconcilingRef.current = false;
            if (!cancelled) setReconciling(false);
          }
        }
        if (operation.status === 'pending' && operation.task_type && operation.task_scope) {
          const task = await fetchRemixTaskStatus(project, operation.task_type, operation.task_scope);
          if (task?.status === 'failed' || task?.status === 'cancelled') {
            setError(task.error ?? `任务${task.status}`);
            setOperationView({ id: activeOperationId, status: task.status });
            return;
          }
        }
        if (!cancelled) setOperationView({ id: activeOperationId, status, path });
      } catch (caught) { if (!cancelled) setError(caught instanceof Error ? caught.message : String(caught)); }
    };
    void check();
    const timer = window.setInterval(() => void check(), 3_000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [activeOperationId, savingOperationHandle, operationView?.path, operationView?.status, project, canvasId, nodeId, draft.manifestRef?.artifactId, draft.manifestRef?.revision, draft.nodeRevision]);

  const visibleOperation = operationView?.id === activeOperationId ? operationView : null;
  const visibleStatus = visibleOperation?.status ?? (activeOperationId === draft.operationId ? draft.status : undefined);
  const visiblePath = visibleOperation?.path ?? (activeOperationId === draft.operationId ? draft.resultPath : undefined);

  return createPortal(
    <div className="fixed inset-0 z-[130] overflow-auto bg-bg-dark/95 p-5 text-text-dark backdrop-blur-sm" onClick={(event) => event.stopPropagation()}>
      <div className="mx-auto max-w-4xl space-y-5">
        <header className="flex items-center justify-between border-b border-border-dark pb-3">
          <div><h2 className="text-lg font-semibold">参考视频改编</h2><p className="text-xs text-text-muted">上传视频，标注关键台词时间，生成带字幕的竖屏视频</p></div>
          <button type="button" disabled={busy || reconciling} onClick={onClose} className="rounded-md border border-border-dark px-3 py-1.5 text-sm disabled:opacity-50">关闭</button>
        </header>
        <section className="grid gap-4 md:grid-cols-2">
          <label className="space-y-2 rounded-xl border border-border-dark bg-surface-panel p-4 text-sm">1. 参考视频
            <input type="file" accept="video/mp4,video/quicktime,video/x-matroska,video/webm" disabled={busy || reconciling || Boolean(transcription)} onChange={(event) => { const file = event.target.files?.[0]; if (file) void upload(file, 'video'); }} className="block w-full text-xs" />
            <span className="block text-xs text-text-muted">{draft.video ? `${draft.video.media_path.split('/').pop()} · ${(draft.video.duration_us / 1_000_000).toFixed(1)} 秒` : '尚未上传'}</span>
          </label>
          <label className="space-y-2 rounded-xl border border-border-dark bg-surface-panel p-4 text-sm">2. 旁白音频（可选）
            <input type="file" accept="audio/*" disabled={busy || reconciling || Boolean(transcription)} onChange={(event) => { const file = event.target.files?.[0]; if (file) void upload(file, 'audio'); }} className="block w-full text-xs" />
            <span className="block text-xs text-text-muted">{draft.audio ? draft.audio.media_path.split('/').pop() : draft.video?.has_audio ? '使用参考视频原声' : '无原声时必须上传'}</span>
          </label>
        </section>
        {draft.video && <video controls src={draft.video.url} className="max-h-64 w-full rounded-lg bg-black object-contain" />}
        <section className="rounded-xl border border-border-dark bg-surface-panel p-4">
          <div className="mb-3 flex items-center justify-between"><div><h3 className="font-medium">3. 台词和关键时刻</h3><p className="text-xs text-text-muted">可自动识别前 60 秒字幕，再调整文字与时间；关键时刻用于记录画面意义。</p></div><div className="flex items-center gap-2"><select aria-label="识别语言" value={language} onChange={(event) => setLanguage(event.target.value)} disabled={busy || Boolean(transcription)} className={fieldClass}><option value="zh">中文</option><option value="en">English</option></select><button type="button" disabled={busy || reconciling || Boolean(transcription) || !(draft.audio || draft.video?.has_audio)} className="rounded-md border border-border-dark px-3 py-1.5 text-xs disabled:opacity-50" onClick={() => void transcribe()}>{transcription ? '正在识别…' : '自动识别字幕'}</button><button type="button" disabled={busy || reconciling || Boolean(transcription)} className="rounded-md border border-border-dark px-3 py-1.5 text-xs disabled:opacity-50" onClick={() => changed({ ...draft, cues: [...draft.cues, { id: crypto.randomUUID(), label: '', text: '', startSeconds: 0, endSeconds: 1 }] })}>添加台词</button></div></div>
          <div className="space-y-3">{draft.cues.map((cue) => <div key={cue.id} className="grid gap-2 rounded-lg border border-border-dark p-3 md:grid-cols-[1fr_1fr_90px_90px_auto]">
            <label className="text-xs text-text-muted">关键时刻<input aria-label="关键时刻" disabled={busy || reconciling || Boolean(transcription)} value={cue.label} onChange={(event) => updateCue(cue.id, { label: event.target.value })} placeholder="如：产品登场" className={`mt-1 block w-full ${fieldClass}`} /></label>
            <label className="text-xs text-text-muted">字幕内容<input aria-label="字幕内容" disabled={busy || reconciling || Boolean(transcription)} value={cue.text} onChange={(event) => updateCue(cue.id, { text: event.target.value })} className={`mt-1 block w-full ${fieldClass}`} /></label>
            <label className="text-xs text-text-muted">开始秒<input aria-label="开始秒" disabled={busy || reconciling || Boolean(transcription)} type="number" min="0" step="0.1" value={cue.startSeconds} onChange={(event) => updateCue(cue.id, { startSeconds: Number(event.target.value) })} className={`mt-1 block w-full ${fieldClass}`} /></label>
            <label className="text-xs text-text-muted">结束秒<input aria-label="结束秒" disabled={busy || reconciling || Boolean(transcription)} type="number" min="0" step="0.1" value={cue.endSeconds} onChange={(event) => updateCue(cue.id, { endSeconds: Number(event.target.value) })} className={`mt-1 block w-full ${fieldClass}`} /></label>
            <button type="button" disabled={busy || reconciling || Boolean(transcription)} className="self-end text-xs text-text-muted disabled:opacity-50" onClick={() => changed({ ...draft, cues: draft.cues.filter((item) => item.id !== cue.id) })}>删除</button>
            <label className="text-xs text-text-muted md:col-span-full">替换画面（可选；留空则使用参考视频）<input aria-label="替换画面" type="file" accept="video/*" disabled={busy || reconciling || Boolean(transcription)} onChange={(event) => { const file = event.target.files?.[0]; if (file) void uploadVisual(file, cue.id); }} className="mt-1 block w-full text-xs" />{cue.visual && <span className="block text-primary">已选择 {cue.visual.media_path.split('/').pop()}</span>}</label>
          </div>)}</div>
        </section>
        {history.length > 0 && <section className="rounded-xl border border-border-dark bg-surface-panel p-4">
          <h3 className="mb-2 text-sm font-medium">生成记录</h3>
          <div className="space-y-2">{history.map((item) => {
            const path = item.output_refs.find((ref) => ref.project_path)?.project_path;
            return <div key={item.operation_id} className="flex items-center gap-3 text-xs">
              <button type="button" disabled={reconciling} onClick={() => { setActiveOperationId(item.operation_id); setOperationView(null); }} className="text-primary underline disabled:opacity-50">查看任务 {item.operation_id}</button>
              <span className="text-text-muted">{item.status}</span>
              {path && <a href={`/api/v1/projects/${encodeURIComponent(project)}/files/${path.split('/').map(encodeURIComponent).join('/')}`} className="text-primary underline">下载</a>}
            </div>;
          })}</div>
        </section>}
        <footer className="flex items-center gap-4 border-t border-border-dark pt-4">
          <button type="button" disabled={busy || reconciling || Boolean(transcription) || !draft.video} onClick={() => void submit()} className="rounded-lg bg-primary px-5 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50">{busy || reconciling ? '处理中…' : '生成视频'}</button>
          {visibleStatus && <span className="text-sm text-text-muted">任务状态：{visibleStatus}</span>}
          {visiblePath && <><a href={mediaUrl(project, visiblePath)} target="_blank" rel="noreferrer" className="text-sm text-primary underline">播放成片</a><a href={`/api/v1/projects/${encodeURIComponent(project)}/files/${visiblePath.split('/').map(encodeURIComponent).join('/')}`} className="text-sm text-primary underline">下载成片</a></>}
          {error && <span role="alert" className="text-sm text-destructive">{error}</span>}
        </footer>
      </div>
    </div>, document.body,
  );
}
