import { memo, useEffect, useRef, useState } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { ChevronDown, ChevronRight, ScrollText } from 'lucide-react';
import { fetchRemixArtifact, fetchRemixOperation, fetchRemixTaskStatus, submitRemixProcessAction, type RemixArtifactRef } from '@/api/remix';
import { useUpstreamNodes } from '@/features/canvas/application/useUpstreamGraph';
import type { SemanticTimelineNodeData } from '@/features/canvas/domain/canvasNodes';
import { buildTimelineEditMap, buildTimelineOverrides, operationMatchesTimelineInputs, readTimingEvents, type TimelineEventOverride } from '@/features/canvas/remix/semanticTimelineModel';
import { NodeHeader, NODE_HEADER_FLOATING_POSITION_CLASS } from '@/features/canvas/ui/NodeHeader';
import { NODE_CONTEXT_CONTROL_TRIGGER_CLASS } from '@/features/canvas/ui/nodeControlStyles';
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

type ScriptSelection = {
  id?: unknown;
  label?: unknown;
  start?: unknown;
  end?: unknown;
};

function formatFrameTime(frame: number | undefined, fps: number): string | null {
  if (frame === undefined || !Number.isFinite(frame)) return null;
  const seconds = Math.max(0, frame / fps);
  const minutes = Math.floor(seconds / 60);
  const remainder = (seconds % 60).toFixed(1).padStart(4, '0');
  return `${String(minutes).padStart(2, '0')}:${remainder}`;
}

function readScriptPresentation(draft: unknown, selectionId: string, fallbackLabel: string, index: number) {
  if (!draft || typeof draft !== 'object') return { label: fallbackLabel === selectionId ? `片段 ${index + 1}` : fallbackLabel, dialogue: '' };
  const item = draft as { dialogue?: unknown; selections?: unknown };
  const selection = Array.isArray(item.selections)
    ? (item.selections as ScriptSelection[]).find((candidate) => candidate?.id === selectionId)
    : undefined;
  const label = typeof selection?.label === 'string' && selection.label.trim()
    ? selection.label.trim()
    : fallbackLabel === selectionId ? `片段 ${index + 1}` : fallbackLabel;
  if (typeof item.dialogue !== 'string' || !Number.isFinite(selection?.start) || !Number.isFinite(selection?.end)) return { label, dialogue: '' };
  const characters = Array.from(item.dialogue);
  const start = Math.max(0, Math.floor(Number(selection?.start)));
  const end = Math.max(start, Math.floor(Number(selection?.end)));
  return { label, dialogue: characters.slice(start, end).join('').trim() };
}

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
  const [expandedEventId, setExpandedEventId] = useState<string | null>(null);
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
    <div className={`ui-scrollbar nowheel max-h-[500px] overflow-auto rounded-[var(--node-radius)] border p-3 ${CANVAS_NODE_PANEL_SURFACE_CLASS} ${canvasNodeFrameClass({ selected })}`}>
      <div className="mb-3 flex items-center justify-between gap-2 border-b border-white/[0.06] pb-3 text-[12px]">
        <div className="flex items-center gap-2">
          <span className={`rounded-full px-2 py-0.5 text-[11px] ${busy || data.compileOperationId ? 'bg-amber-300/10 text-amber-300' : data.artifactRef ? 'bg-emerald-300/10 text-emerald-300' : 'bg-white/[0.06] text-text-muted'}`}>
            {busy || data.compileOperationId ? '正在计算…' : data.artifactRef ? '已计算' : '待计算'}
          </span>
          <span className="font-medium text-text-dark">{data.events.length} 个片段</span>
        </div>
        {data.artifactRef ? <span className="text-[11px] text-text-muted">版本 {data.artifactRef.revision}</span> : null}
      </div>
      {!asRef(scriptRef) || !asRef(wordRef) ? <p className="mb-2 text-[12px] text-amber-300">连接脚本和已对齐语音的转写节点后可编译。</p> : null}
      {(error || data.compileError) && <p role="alert" className="mb-2 text-[12px] text-red-300">{error || data.compileError}</p>}
      {data.events.map((event, index) => {
        const override = overrides[event.selectionId] ?? {};
        const presentation = readScriptPresentation(scriptNode?.data?.remixScriptDraft, event.selectionId, event.label, index);
        const startTime = formatFrameTime(event.startFrame, data.fps ?? 30);
        const endTime = formatFrameTime(event.endFrame, data.fps ?? 30);
        const expanded = expandedEventId === event.selectionId;
        return <section key={`${event.selectionId}-${index}`} className="border-b border-white/[0.06] py-3 last:border-b-0">
          <div className="flex items-start gap-3">
            <span className="mt-0.5 flex h-6 min-w-6 items-center justify-center rounded-full bg-white/[0.06] px-1 text-[10px] font-medium tabular-nums text-text-muted">{String(index + 1).padStart(2, '0')}</span>
            <div className="min-w-0 flex-1">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0 font-medium text-[13px] leading-5 text-text-dark">{presentation.label}</div>
                <div className="shrink-0 rounded-md bg-white/[0.05] px-2 py-1 text-[10px] tabular-nums text-text-muted">
                  {startTime && endTime ? `${startTime} – ${endTime}` : '等待计算'}
                </div>
              </div>
              {presentation.dialogue ? <p className="mt-1 line-clamp-2 text-[11px] leading-4 text-text-muted">{presentation.dialogue}</p> : null}
              {event.invalidReason ? <p className="mt-1 rounded-md bg-amber-300/10 px-2 py-1 text-[11px] text-amber-300">需要处理：{event.invalidReason}</p> : null}
              <button
                type="button"
                aria-label={`微调 ${presentation.label}`}
                aria-expanded={expanded}
                onClick={() => setExpandedEventId(expanded ? null : event.selectionId)}
                className={`${NODE_CONTEXT_CONTROL_TRIGGER_CLASS} nodrag mt-2 !h-7 !gap-1 !px-2 !text-[11px]`}
              >
                {expanded ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
                微调
              </button>
            </div>
          </div>
          {expanded ? <div className="nodrag ml-9 mt-2 rounded-[10px] border border-white/[0.07] bg-black/10 p-2">
            <div className="mb-2 text-[10px] leading-4 text-text-muted">正数向后移动，负数向前移动；固定开始和结束需同时填写。</div>
            <div className="grid grid-cols-2 gap-2">
              {([['offsetMs', '整体移动（毫秒）'], ['minimumDurationFrames', '最短时长（帧）'], ['startFrame', '固定开始（帧）'], ['endFrame', '固定结束（帧）']] as const).map(([key, label]) => <label key={key} className="text-[10px] text-text-muted">{label}<input aria-label={`${presentation.label} ${label}`} type="number" value={override[key] ?? ''} onChange={(change) => changeOverride(event.selectionId, key, change.target.value)} className="nodrag mt-1 h-8 w-full rounded-[10px] border border-white/10 bg-surface-field px-2 text-[11px] text-text-dark outline-none focus:border-white/25" /></label>)}
            </div>
            {event.startFrame !== undefined && event.endFrame !== undefined ? <div className="mt-2 text-[10px] text-text-muted">当前帧：{event.startFrame}–{event.endFrame} · {data.fps ?? 30} fps</div> : null}
          </div> : null}
        </section>;
      })}
      {data.events.length === 0 && <div className="rounded-[10px] border border-dashed border-white/10 px-3 py-6 text-center text-[12px] text-text-muted">还没有可计算的片段，请先在脚本节点添加语义事件。</div>}
    </div>
  </div>;
});
SemanticTimelineNode.displayName = 'SemanticTimelineNode';
