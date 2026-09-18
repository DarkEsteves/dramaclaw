import { memo, useEffect, useMemo, useRef, useState } from 'react';
import { Handle, Position, useUpdateNodeInternals, type NodeProps } from '@xyflow/react';
import { FileText } from 'lucide-react';
import { fetchRemixArtifact, fetchRemixOperation, submitRemixProcessAction, type RemixArtifactRef } from '@/api/remix';
import type { TranscriptNodeData } from '@/features/canvas/domain/canvasNodes';
import { correctedTimeline, sourceMediaForTranscript, sourceScriptForTranscript, timelineWords, transcriptSrt, type TranscriptTimeline } from '@/features/canvas/remix/transcriptModel';
import { reviseTranscriptArtifact } from '@/features/canvas/remix/transcriptArtifactApi';
import { NodeHeader, NODE_HEADER_FLOATING_POSITION_CLASS } from '@/features/canvas/ui/NodeHeader';
import { NodeResizeHandle } from '@/features/canvas/ui/NodeResizeHandle';
import { CANVAS_NODE_PANEL_SURFACE_CLASS, canvasNodeFrameClass } from '@/features/canvas/ui/nodeFrameStyles';
import { readUrl } from '@/lib/url-params';
import { useCanvasStore } from '@/stores/canvasStore';

type Props = NodeProps & { id: string; data: TranscriptNodeData; selected?: boolean };

function stamp(us: number): string {
  const ms = Math.max(0, Math.floor(us / 1_000));
  return `${String(Math.floor(ms / 60_000)).padStart(2, '0')}:${String(Math.floor(ms / 1_000) % 60).padStart(2, '0')}.${String(ms % 1_000).padStart(3, '0')}`;
}

function download(name: string, body: string, type: string): void {
  const url = URL.createObjectURL(new Blob([body], { type }));
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1_000);
}

export const TranscriptNode = memo(({ id, data, selected }: Props) => {
  const updateNodeInternals = useUpdateNodeInternals();
  const setSelectedNode = useCanvasStore((state) => state.setSelectedNode);
  const updateNodeData = useCanvasStore((state) => state.updateNodeData);
  const nodes = useCanvasStore((state) => state.nodes);
  const edges = useCanvasStore((state) => state.edges);
  const source = useMemo(() => sourceMediaForTranscript(id, nodes, edges), [id, nodes, edges]);
  const scriptRef = useMemo(() => sourceScriptForTranscript(id, nodes, edges), [id, nodes, edges]);
  const [operationId, setOperationId] = useState<string | null>(typeof data.operationId === 'string' ? data.operationId : null);
  const [language, setLanguage] = useState('zh');
  const [edited, setEdited] = useState<string[]>(() => data.words.map((word) => word.text));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const mediaRef = useRef<HTMLMediaElement>(null);
  useEffect(() => updateNodeInternals(id), [id, updateNodeInternals]);
  useEffect(() => setEdited(data.words.map((word) => word.text)), [data.words]);

  const project = readUrl().project;
  const artifactRef = data.artifactRef as RemixArtifactRef | null | undefined;
  const canSave = Boolean(artifactRef && edited.some((text, index) => text !== data.words[index]?.text));
  useEffect(() => {
    const aligned = data.alignedScriptRef as RemixArtifactRef | null | undefined;
    if (artifactRef && aligned && (!scriptRef || aligned.artifactId !== scriptRef.artifactId || aligned.revision !== scriptRef.revision)) {
      updateNodeData(id, { artifactRef: null, status: 'needs_review', alignmentStale: true, unresolved: ['脚本已修改，请重新对齐'] });
    }
  }, [artifactRef, data.alignedScriptRef, id, scriptRef, updateNodeData]);

  const start = async (mode: 'transcribe' | 'align') => {
    if (busy || operationId) return;
    if (!project || !source) { setError('请先连接已上传的音频或带声音视频'); return; }
    if (mode === 'align' && !scriptRef) { setError('请先连接已保存脚本的节点'); return; }
    if (data.words.length > 0 && !window.confirm('重新识别会替换当前校字结果，确定继续吗？')) return;
    setBusy(true); setError(null);
    try {
      const revision = (typeof data.nodeRevision === 'number' ? data.nodeRevision : 0) + 1;
      const task = await submitRemixProcessAction(project, mode === 'align'
        ? { action: 'align_script', mediaPath: source.path, scriptRef: scriptRef!, idempotencyKey: crypto.randomUUID(), nodeRevision: revision }
        : { action: 'transcribe_media', mediaPath: source.path, language, idempotencyKey: crypto.randomUUID(), nodeRevision: revision });
      updateNodeData(id, { operationId: task.operation_id, sourceMediaPath: source.path, nodeRevision: revision, status: 'needs_review', unresolved: [], artifactRef: null, words: [], pendingScriptRef: mode === 'align' ? scriptRef : null, alignedScriptRef: null, alignmentStale: false });
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
        if (operation.status === 'failed' || operation.status === 'cancelled') throw new Error('语音识别失败，请重试');
        if (operation.status !== 'needs_confirmation' && operation.status !== 'completed') return;
        const output = operation.output_refs.find((item) => item.artifact_id && item.revision);
        if (!output?.artifact_id || !output.revision) throw new Error('语音识别未返回转写产物');
        const artifact = await fetchRemixArtifact(project, { artifactId: output.artifact_id, revision: output.revision });
        if (cancelled) return;
        if (artifact.schema !== 'word_timeline.v1') throw new Error('转写产物格式错误');
        const payload = artifact.payload as TranscriptTimeline;
        const words = timelineWords(payload);
        if (payload.status !== 'completed' || words.length === 0) throw new Error('没有识别到人声，请检查素材');
        const store = useCanvasStore.getState();
        const current = sourceMediaForTranscript(id, store.nodes, store.edges);
        const saved = store.nodes.find((node) => node.id === id)?.data as TranscriptNodeData | undefined;
        if (!current || current.path !== saved?.sourceMediaPath) throw new Error('上游音频已变化，请重新转写');
        updateNodeData(id, { artifactRef: { artifactId: artifact.id, revision: artifact.revision }, words, status: 'completed', unresolved: [], operationId: null, alignedScriptRef: saved?.pendingScriptRef ?? null, pendingScriptRef: null, alignmentStale: false });
        setOperationId(null);
      } catch (caught) {
        if (!cancelled) {
          setError(caught instanceof Error ? caught.message : String(caught));
          updateNodeData(id, { operationId: null, status: 'failed' });
          setOperationId(null);
        }
      } finally { checking = false; }
    };
    void check();
    const timer = window.setInterval(() => void check(), 3_000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [id, operationId, project, updateNodeData]);

  const saveCorrections = async () => {
    if (!project || !artifactRef || !canSave || busy) return;
    setBusy(true); setError(null);
    try {
      const artifact = await fetchRemixArtifact(project, artifactRef);
      if (artifact.schema !== 'word_timeline.v1') throw new Error('转写产物格式错误');
      const payload = artifact.payload as TranscriptTimeline;
      const revised = correctedTimeline(payload, payload.words.map((word, index) => edited[index] ?? word.text));
      const rawRefs = (artifact as typeof artifact & { upstream_refs?: Array<{ artifact_id: string; revision: number }> }).upstream_refs;
      if (!Array.isArray(rawRefs) && (data.alignedScriptRef || payload.script_ref)) throw new Error('无法确认对齐脚本版本，请重新对齐');
      const upstreamRefs = (rawRefs ?? []).map((upstream) => ({ artifactId: upstream.artifact_id, revision: upstream.revision }));
      const saved = await reviseTranscriptArtifact(project, artifactRef, revised, upstreamRefs);
      updateNodeData(id, { artifactRef: { artifactId: saved.id, revision: saved.revision }, words: timelineWords(revised), status: 'completed' });
    } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)); }
    finally { setBusy(false); }
  };

  const exportResult = async (format: 'json' | 'srt') => {
    if (!project || !artifactRef) return;
    try {
      const artifact = await fetchRemixArtifact(project, artifactRef);
      const name = `transcript-${artifactRef.artifactId}-r${artifactRef.revision}`;
      if (format === 'json') download(`${name}.json`, JSON.stringify(artifact.payload, null, 2), 'application/json');
      else download(`${name}.srt`, transcriptSrt(timelineWords(artifact.payload as TranscriptTimeline)), 'text/plain');
    } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)); }
  };

  return <div className="group relative w-[420px]" onClick={() => setSelectedNode(id)}>
    <Handle type="target" position={Position.Left} id="media" className="!h-2 !w-2 !border-0 !bg-[rgb(148,163,184)]" />
    <Handle type="source" position={Position.Right} id="transcript" className="!h-2 !w-2 !border-0 !bg-[rgb(45,212,191)]" />
    <NodeHeader className={NODE_HEADER_FLOATING_POSITION_CLASS} icon={<FileText className="h-4 w-4" />} titleText={data.displayName || '转写与对齐'} editable={false} />
    <NodeResizeHandle minWidth={320} minHeight={220} maxWidth={760} maxHeight={900} />
    <div className={`flex max-h-[600px] flex-col overflow-hidden rounded-[var(--node-radius)] border ${CANVAS_NODE_PANEL_SURFACE_CLASS} ${canvasNodeFrameClass({ selected })}`}>
      <div className="border-b border-white/[0.08] p-3 text-xs text-text-muted">
        <div className="mb-2 truncate">{source ? `${source.kind === 'audio' ? '音频' : '视频'}：${source.path}` : '请连接上传的音频或带声音视频'}</div>
        {source?.url && (source.kind === 'audio' ? <audio ref={mediaRef as React.RefObject<HTMLAudioElement>} src={source.url} controls className="w-full" /> : <video ref={mediaRef as React.RefObject<HTMLVideoElement>} src={source.url} controls className="max-h-40 w-full" />)}
        <div className="mt-2 flex items-center gap-2">
          <select aria-label="转写语言" value={language} onChange={(event) => setLanguage(event.target.value)} className="rounded border border-border-dark bg-surface-field px-1 py-1"><option value="zh">中文</option><option value="en">English</option></select>
          <button type="button" disabled={busy || Boolean(operationId) || !source} onClick={() => void start('transcribe')} className="rounded border border-border-dark px-2 py-1 disabled:opacity-50">{operationId ? '处理中…' : data.words.length ? '重新转写' : '开始转写'}</button>
          {scriptRef && <button type="button" disabled={busy || Boolean(operationId) || !source} onClick={() => void start('align')} className="rounded border border-border-dark px-2 py-1 disabled:opacity-50">按脚本对齐</button>}
          <span>{artifactRef ? `${artifactRef.artifactId} · r${artifactRef.revision}` : data.status === 'failed' ? '失败' : '待转写'}</span>
        </div>
      </div>
      <div className="ui-scrollbar overflow-auto p-2">
        {Boolean(data.alignmentStale) && <p className="px-2 py-1 text-xs text-amber-300">脚本已修改，请重新对齐</p>}
        {data.words.map((word, index) => <div key={`${word.startUs}-${index}`} className="flex items-center gap-2 rounded-md px-2 py-1 text-[12px] text-text-dark">
          <button type="button" className="w-[145px] shrink-0 text-left font-mono text-[11px] text-text-muted" onClick={() => { if (mediaRef.current) mediaRef.current.currentTime = word.startUs / 1_000_000; }}>{stamp(word.startUs)} – {stamp(word.endUs)}</button>
          <input aria-label={`第 ${index + 1} 个字词`} value={edited[index] ?? word.text} onChange={(event) => setEdited((current) => current.map((text, position) => position === index ? event.target.value : text))} className="min-w-0 flex-1 rounded border border-border-dark bg-surface-field px-1 py-1" />
          {typeof word.confidence === 'number' && word.confidence < 0.6 && <span className="shrink-0 text-amber-300">低置信度</span>}
        </div>)}
        {data.words.length === 0 && <p className="px-2 py-3 text-[12px] text-text-muted">暂无对齐字词</p>}
      </div>
      {artifactRef && <div className="flex gap-2 border-t border-white/[0.08] p-2 text-xs"><button type="button" disabled={!canSave || busy} onClick={() => void saveCorrections()} className="rounded border border-border-dark px-2 py-1 disabled:opacity-50">保存校字</button><button type="button" onClick={() => void exportResult('json')}>导出 JSON</button><button type="button" onClick={() => void exportResult('srt')}>导出 SRT</button></div>}
      {error && <p role="alert" className="px-3 pb-2 text-xs text-red-300">{error}</p>}
    </div>
  </div>;
});
TranscriptNode.displayName = 'TranscriptNode';
