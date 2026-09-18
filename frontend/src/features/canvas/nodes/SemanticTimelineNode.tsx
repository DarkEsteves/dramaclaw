import { memo, useState } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { ScrollText } from 'lucide-react';
import { fetchRemixArtifact, fetchRemixOperation, fetchRemixTaskStatus, submitRemixProcessAction, type RemixArtifactRef } from '@/api/remix';
import { useUpstreamNodes } from '@/features/canvas/application/useUpstreamGraph';
import type { SemanticTimelineNodeData } from '@/features/canvas/domain/canvasNodes';
import { buildTimelineEditMap, buildTimelineOverrides, readTimingEvents, type TimelineEventOverride } from '@/features/canvas/remix/semanticTimelineModel';
import { NodeHeader, NODE_HEADER_FLOATING_POSITION_CLASS } from '@/features/canvas/ui/NodeHeader';
import { CANVAS_NODE_PANEL_SURFACE_CLASS, canvasNodeFrameClass } from '@/features/canvas/ui/nodeFrameStyles';
import { readUrl } from '@/lib/url-params';
import { useCanvasStore } from '@/stores/canvasStore';

type TimelineData = SemanticTimelineNodeData & {
  eventOverrides?: Record<string, TimelineEventOverride>;
  fps?: number;
  compileError?: string | null;
  compileOperationId?: string | null;
  nodeRevision?: number;
};

function asRef(value: unknown): RemixArtifactRef | null {
  if (!value || typeof value !== 'object') return null;
  const item = value as Record<string, unknown>;
  return typeof item.artifactId === 'string' && Number.isInteger(item.revision) && Number(item.revision) > 0
    ? { artifactId: item.artifactId, revision: Number(item.revision) } : null;
}

function sleep(ms: number) { return new Promise<void>((resolve) => setTimeout(resolve, ms)); }

export const SemanticTimelineNode = memo(({ id, data, selected }: NodeProps & { id: string; data: TimelineData; selected?: boolean }) => {
  const updateNodeData = useCanvasStore((state) => state.updateNodeData);
  const setSelectedNode = useCanvasStore((state) => state.setSelectedNode);
  const upstreamNodes = useUpstreamNodes(id);
  const scriptRef = upstreamNodes.find((node) => node.type === 'scriptNode')?.data?.remixScriptRef;
  const wordRef = upstreamNodes.find((node) => node.type === 'transcriptNode')?.data?.artifactRef;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [overrides, setOverrides] = useState<Record<string, TimelineEventOverride>>(data.eventOverrides ?? {});
  const project = readUrl().project;

  const changeOverride = (eventId: string, key: keyof TimelineEventOverride, raw: string) => {
    const nextValue = raw === '' ? undefined : Number(raw);
    const current = overrides;
    const next = { ...current, [eventId]: { ...current[eventId], [key]: nextValue } };
    setOverrides(next);
    updateNodeData(id, {
      eventOverrides: next,
      artifactRef: null,
    });
  };

  const compile = async () => {
    if (!project || busy) return;
    const exactScriptRef = asRef(scriptRef);
    const exactWordRef = asRef(wordRef);
    if (!exactScriptRef || !exactWordRef) {
      setError('请先连接脚本节点和已对齐语音的转写节点。');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const aligned = await fetchRemixArtifact(project, exactWordRef);
      if (aligned.schema !== 'word_timeline.v1' || aligned.payload.status !== 'completed') throw new Error('转写节点需要完成脚本与语音对齐。');
      const editMap = buildTimelineEditMap(Array.isArray(aligned.payload.words) ? aligned.payload.words : []);
      if (!editMap.length) throw new Error('对齐结果没有有效语音时间。');
      const nodeRevision = (data.nodeRevision ?? 0) + 1;
      const task = await submitRemixProcessAction(project, {
        action: 'compile_semantic_timeline', scriptRef: exactScriptRef, wordTimelineRef: exactWordRef,
        editMap, overrides: buildTimelineOverrides(overrides), fps: data.fps ?? 30,
        idempotencyKey: crypto.randomUUID(), nodeRevision,
      });
      updateNodeData(id, { nodeRevision, compileOperationId: task.operation_id, artifactRef: null });
      for (let attempt = 0; attempt < 120; attempt++) {
        const operation = await fetchRemixOperation(project, task.operation_id);
        if (operation.status === 'needs_confirmation' || operation.status === 'completed') {
          const ref = operation.output_refs.find((item) => typeof item.artifact_id === 'string' && Number.isInteger(item.revision));
          if (!ref?.artifact_id || !ref.revision) throw new Error('编译完成，但没有返回时间轴产物。');
          const artifactRef = { artifactId: ref.artifact_id, revision: ref.revision };
          const timing = await fetchRemixArtifact(project, artifactRef);
          if (timing.schema !== 'performance_timing.v1') throw new Error('时间轴产物格式不正确。');
          updateNodeData(id, { artifactRef, events: readTimingEvents(timing.payload), compileOperationId: null, compileError: null });
          return;
        }
        if (operation.status === 'failed' || operation.status === 'cancelled') throw new Error('时间轴编译失败。');
        if (operation.task_type && operation.task_scope) {
          const taskState = await fetchRemixTaskStatus(project, operation.task_type, operation.task_scope);
          if (taskState?.status === 'failed') throw new Error(taskState.error || '时间轴编译失败。');
        }
        await sleep(1000);
      }
      throw new Error('编译仍在运行，请稍后重试。');
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : '时间轴编译失败。';
      setError(message);
      updateNodeData(id, { compileError: message });
    } finally {
      setBusy(false);
    }
  };

  return <div className="group relative w-[420px]" onClick={() => setSelectedNode(id)}>
    <Handle type="target" position={Position.Left} id="timing" className="!h-2 !w-2 !border-0 !bg-[rgb(148,163,184)]" />
    <Handle type="source" position={Position.Right} id="composition" className="!h-2 !w-2 !border-0 !bg-[rgb(45,212,191)]" />
    <NodeHeader className={NODE_HEADER_FLOATING_POSITION_CLASS} icon={<ScrollText className="h-4 w-4" />} titleText={data.displayName || '语义时间轴'} editable={false} />
    <div className={`max-h-[500px] overflow-auto rounded-[var(--node-radius)] border p-3 ${CANVAS_NODE_PANEL_SURFACE_CLASS} ${canvasNodeFrameClass({ selected })}`}>
      <div className="mb-2 flex items-center justify-between gap-2 text-[12px] text-text-muted">
        <span>{data.artifactRef ? `时间轴 r${data.artifactRef.revision}` : '待编译'}</span>
        <button type="button" disabled={busy || !project} onClick={(event) => { event.stopPropagation(); void compile(); }} className="nodrag rounded border border-white/15 px-2 py-1 text-text-dark disabled:opacity-50">{busy ? '编译中…' : '重新编译'}</button>
      </div>
      {!asRef(scriptRef) || !asRef(wordRef) ? <p className="mb-2 text-[12px] text-amber-300">连接脚本和已对齐语音的转写节点后可编译。</p> : null}
      {(error || data.compileError) && <p role="alert" className="mb-2 text-[12px] text-red-300">{error || data.compileError}</p>}
      {data.events.map((event, index) => {
        const override = overrides[event.selectionId] ?? {};
        return <div key={`${event.selectionId}-${index}`} className="border-b border-white/[0.06] py-2 text-[12px] text-text-dark">
          <div>{event.label}</div>
          {event.invalidReason ? <div className="text-amber-300">未解决：{event.invalidReason}</div> : <div className="text-text-muted">{event.startFrame !== undefined ? `帧 ${event.startFrame}–${event.endFrame}` : '等待编译'}</div>}
          <div className="mt-1 grid grid-cols-3 gap-1">
            {([['offsetMs', '偏移毫秒'], ['startFrame', '固定起始帧'], ['endFrame', '固定结束帧']] as const).map(([key, label]) => <label key={key} className="text-[10px] text-text-muted">{label}<input aria-label={`${event.label} ${label}`} type="number" value={override[key] ?? ''} onChange={(change) => changeOverride(event.selectionId, key, change.target.value)} className="nodrag mt-1 w-full rounded border border-white/10 bg-black/20 px-1 py-1 text-[11px] text-text-dark" /></label>)}
          </div>
        </div>;
      })}
      {data.events.length === 0 && <div className="text-[12px] text-text-muted">没有时间事件；先从脚本节点定义 Selection。</div>}
    </div>
  </div>;
});
SemanticTimelineNode.displayName = 'SemanticTimelineNode';
