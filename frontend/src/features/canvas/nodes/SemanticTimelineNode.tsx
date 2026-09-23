import { memo, useEffect, useRef, useState } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { ScrollText } from 'lucide-react';
import { fetchRemixArtifact, fetchRemixOperation, fetchRemixTaskStatus, submitRemixProcessAction, type RemixArtifactRef } from '@/api/remix';
import { useUpstreamNodes } from '@/features/canvas/application/useUpstreamGraph';
import type { SemanticTimelineNodeData } from '@/features/canvas/domain/canvasNodes';
import { buildTimelineEditMap, buildTimelineOverrides, operationMatchesTimelineInputs, readTimingEvents, type TimelineEventOverride } from '@/features/canvas/remix/semanticTimelineModel';
import { NodeHeader, NODE_HEADER_FLOATING_POSITION_CLASS } from '@/features/canvas/ui/NodeHeader';
import { CANVAS_NODE_PANEL_SURFACE_CLASS, canvasNodeFrameClass } from '@/features/canvas/ui/nodeFrameStyles';
import { readUrl } from '@/lib/url-params';
import { useCanvasStore } from '@/stores/canvasStore';
import { canvasEventBus } from '@/features/canvas/application/canvasServices';
import { createComposeFromTimeline } from '@/features/canvas/remix/spawnRemixDownstream';
import { toast } from 'sonner';

type TimelineData = SemanticTimelineNodeData & {
  eventOverrides?: Record<string, TimelineEventOverride>;
  fps?: number;
  compileError?: string | null;
  compileOperationId?: string | null;
  nodeRevision?: number;
  autoCompileRequested?: boolean;
  editMap?: Record<string, number>[];
  compiledMediaHash?: string;
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
  const scriptNode = upstreamNodes.find((node) => node.type === 'scriptNode');
  const transcriptNode = upstreamNodes.find((node) => node.type === 'transcriptNode');
  const scriptRef = scriptNode?.data?.remixScriptRef;
  const wordRef = transcriptNode?.data?.artifactRef;
  const blueprintRef = scriptNode?.data?.remixBlueprintRef;
  const narrationPath = typeof transcriptNode?.data?.sourceMediaPath === 'string' ? transcriptNode.data.sourceMediaPath : null;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [overrides, setOverrides] = useState<Record<string, TimelineEventOverride>>(data.eventOverrides ?? {});
  const project = readUrl().project;
  const currentOperationRef = useRef(data.compileOperationId ?? null);
  const currentInputsRef = useRef({ script: asRef(scriptRef), words: asRef(wordRef), blueprint: asRef(blueprintRef), narrationPath, revision: data.nodeRevision ?? 0 });
  const watchingRef = useRef<string | null>(null);
  const compileClaimRef = useRef(false);
  const mountedRef = useRef(true);
  currentInputsRef.current = { script: asRef(scriptRef), words: asRef(wordRef), blueprint: asRef(blueprintRef), narrationPath, revision: data.nodeRevision ?? currentInputsRef.current.revision };

  useEffect(() => { mountedRef.current = true; return () => { mountedRef.current = false; }; }, []);

  const changeOverride = (eventId: string, key: keyof TimelineEventOverride, raw: string) => {
    const nextValue = raw === '' ? undefined : Number(raw);
    const current = overrides;
    const next = { ...current, [eventId]: { ...current[eventId], [key]: nextValue } };
    setOverrides(next);
    currentOperationRef.current = null;
    currentInputsRef.current.revision += 1;
    updateNodeData(id, {
      eventOverrides: next,
      artifactRef: null,
      compileOperationId: null,
      nodeRevision: currentInputsRef.current.revision,
    });
  };

  const watchOperation = async (operationId: string) => {
    if (!project || watchingRef.current === operationId) return;
    watchingRef.current = operationId;
    setBusy(true);
    setError(null);
    try {
      for (let attempt = 0; attempt < 120; attempt++) {
        if (!mountedRef.current || currentOperationRef.current !== operationId) return;
        const operation = await fetchRemixOperation(project, operationId);
        if (!mountedRef.current || currentOperationRef.current !== operationId) return;
        const inputs = currentInputsRef.current;
        if (!operationMatchesTimelineInputs(operation, inputs.script, inputs.words, inputs.blueprint, inputs.revision)) {
          currentOperationRef.current = null;
          updateNodeData(id, { artifactRef: null, compileOperationId: null, compileError: '上游脚本或语音版本已变化，请重新编译。' });
          setError('上游脚本或语音版本已变化，请重新编译。');
          return;
        }
        if (operation.status === 'needs_confirmation' || operation.status === 'completed') {
          const ref = operation.output_refs.find((item) => typeof item.artifact_id === 'string' && Number.isInteger(item.revision));
          if (!ref?.artifact_id || !ref.revision) throw new Error('编译完成，但没有返回时间轴产物。');
          const artifactRef = { artifactId: ref.artifact_id, revision: ref.revision };
          const timing = await fetchRemixArtifact(project, artifactRef);
          if (timing.schema !== 'performance_timing.v1') throw new Error('时间轴产物格式不正确。');
          const current = currentInputsRef.current;
          if (!mountedRef.current || currentOperationRef.current !== operationId || !operationMatchesTimelineInputs(operation, current.script, current.words, current.blueprint, current.revision)) return;
          currentOperationRef.current = null;
          updateNodeData(id, {
            artifactRef,
            events: readTimingEvents(timing.payload),
            compiledMediaHash: typeof timing.payload.media_hash === 'string' ? timing.payload.media_hash : undefined,
            compiledScriptRef: current.script,
            compiledAlignmentRef: current.words,
            compileOperationId: null,
            compileError: null,
          });
          return;
        }
        if (operation.status === 'failed' || operation.status === 'cancelled') {
          currentOperationRef.current = null;
          updateNodeData(id, { compileOperationId: null });
          throw new Error('时间轴编译失败。');
        }
        if (operation.task_type && operation.task_scope) {
          const taskState = await fetchRemixTaskStatus(project, operation.task_type, operation.task_scope);
          if (taskState?.status === 'failed') {
            currentOperationRef.current = null;
            updateNodeData(id, { compileOperationId: null });
            throw new Error(taskState.error || '时间轴编译失败。');
          }
        }
        await sleep(1000);
      }
      throw new Error('编译仍在运行，请稍后继续检查。');
    } catch (cause) {
      if (!mountedRef.current) return;
      const message = cause instanceof Error ? cause.message : '时间轴编译状态暂时不可用。';
      setError(message);
      updateNodeData(id, { compileError: message });
    } finally {
      watchingRef.current = null;
      if (mountedRef.current) setBusy(false);
    }
  };

  useEffect(() => {
    if (data.compileOperationId) {
      currentOperationRef.current = data.compileOperationId;
      void watchOperation(data.compileOperationId);
    }
  }, [data.compileOperationId, project]);

  const compile = async () => {
    if (!project || compileClaimRef.current) return;
    if (currentOperationRef.current) { void watchOperation(currentOperationRef.current); return; }
    const exactScriptRef = asRef(scriptRef);
    const exactWordRef = asRef(wordRef);
    const exactBlueprintRef = asRef(blueprintRef);
    if (!exactScriptRef || !exactWordRef || !exactBlueprintRef || !narrationPath) {
      setError('请先连接脚本、参考蓝图和已对齐旁白的转写节点。');
      return;
    }
    compileClaimRef.current = true;
    setBusy(true);
    setError(null);
    try {
      const compiledOverrides = buildTimelineOverrides(overrides);
      const aligned = await fetchRemixArtifact(project, exactWordRef);
      if (aligned.schema !== 'word_timeline.v1' || aligned.payload.status !== 'completed') throw new Error('转写节点需要完成脚本与语音对齐。');
      const editMap = buildTimelineEditMap(Array.isArray(aligned.payload.words) ? aligned.payload.words : [], data.editMap);
      if (!editMap.length) throw new Error('对齐结果没有有效语音时间。');
      const nodeRevision = currentInputsRef.current.revision + 1;
      const task = await submitRemixProcessAction(project, {
        action: 'compile_semantic_timeline', mediaPath: narrationPath, scriptRef: exactScriptRef, wordTimelineRef: exactWordRef, blueprintRef: exactBlueprintRef,
        editMap, overrides: compiledOverrides, fps: data.fps ?? 30,
        idempotencyKey: crypto.randomUUID(), nodeRevision,
      });
      currentInputsRef.current.revision = nodeRevision;
      currentOperationRef.current = task.operation_id;
      updateNodeData(id, { nodeRevision, compileOperationId: task.operation_id, artifactRef: null });
      await watchOperation(task.operation_id);
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : '时间轴编译失败。';
      setError(message);
      updateNodeData(id, { compileError: message });
    } finally {
      compileClaimRef.current = false;
      if (!watchingRef.current && mountedRef.current) setBusy(false);
    }
  };
  useEffect(() => canvasEventBus.subscribe('remix-node/action', ({ nodeId, action }) => {
    if (nodeId === id && action === 'compile') void compile();
    if (nodeId === id && action === 'create_compose') {
      try { createComposeFromTimeline(id); }
      catch (cause) { toast.error(cause instanceof Error ? cause.message : '无法创建视频合成节点'); }
    }
  }), [id, compile]);

  useEffect(() => {
    if (!data.autoCompileRequested || !asRef(scriptRef) || !asRef(wordRef)) return;
    updateNodeData(id, { autoCompileRequested: false });
    void compile();
  }, [data.autoCompileRequested, scriptRef, wordRef, id]);

  return <div className="group relative w-[420px]" onClick={() => setSelectedNode(id)}>
    <Handle type="target" position={Position.Left} id="target" className="!h-2 !w-2 !border-0 !bg-[rgb(148,163,184)]" />
    <Handle type="source" position={Position.Right} id="source" className="!h-2 !w-2 !border-0 !bg-[rgb(45,212,191)]" />
    <NodeHeader className={NODE_HEADER_FLOATING_POSITION_CLASS} icon={<ScrollText className="h-4 w-4" />} titleText={data.displayName || '语义时间轴'} editable={false} />
    <div className={`max-h-[500px] overflow-auto rounded-[var(--node-radius)] border p-3 ${CANVAS_NODE_PANEL_SURFACE_CLASS} ${canvasNodeFrameClass({ selected })}`}>
      <div className="mb-2 flex items-center justify-between gap-2 text-[12px] text-text-muted">
        <span>{busy || data.compileOperationId ? '正在计算时间区间…' : data.artifactRef ? `时间轴 r${data.artifactRef.revision}` : '待计算时间区间'}</span>
      </div>
      {!asRef(scriptRef) || !asRef(wordRef) ? <p className="mb-2 text-[12px] text-amber-300">连接脚本和已对齐语音的转写节点后可编译。</p> : null}
      {(error || data.compileError) && <p role="alert" className="mb-2 text-[12px] text-red-300">{error || data.compileError}</p>}
      {data.events.map((event, index) => {
        const override = overrides[event.selectionId] ?? {};
        return <div key={`${event.selectionId}-${index}`} className="border-b border-white/[0.06] py-2 text-[12px] text-text-dark">
          <div>{event.label}</div>
          {event.invalidReason ? <div className="text-amber-300">未解决：{event.invalidReason}</div> : <div className="text-text-muted">{event.startFrame !== undefined ? `帧 ${event.startFrame}–${event.endFrame}` : '等待编译'}</div>}
          <div className="mt-1 grid grid-cols-4 gap-1">
            {([['offsetMs', '语义偏移毫秒'], ['minimumDurationFrames', '最短帧数'], ['startFrame', '固定起始帧'], ['endFrame', '固定结束帧']] as const).map(([key, label]) => <label key={key} className="text-[10px] text-text-muted">{label}<input aria-label={`${event.label} ${label}`} type="number" value={override[key] ?? ''} onChange={(change) => changeOverride(event.selectionId, key, change.target.value)} className="nodrag mt-1 h-8 w-full rounded-[12px] border border-white/10 bg-surface-field px-2 text-[11px] text-text-dark outline-none focus:border-white/25" /></label>)}
          </div>
        </div>;
      })}
      {data.events.length === 0 && <div className="text-[12px] text-text-muted">没有时间事件；先从脚本节点定义 Selection。</div>}
    </div>
  </div>;
});
SemanticTimelineNode.displayName = 'SemanticTimelineNode';
