import { useEffect, useRef, useState, type ReactNode } from 'react';
import { fetchRemixArtifact, generateRemixScriptDraft, type RemixArtifactRef } from '@/api/remix';
import { editRemixDialogue, mergeAdjacentPerformanceSegments, referenceBindingsForSegment, seedPerformanceSegments, spokenTextForSegment, type RemixPerformanceSegment, type RemixReferenceBinding, type RemixScriptDraft } from './remixScriptModel';
import { SegmentReferenceEditor, type ReferenceMediaNode } from './SegmentReferenceEditor';
import { CANVAS_NODE_OPS_PANEL_CLASS } from '@/features/canvas/ui/nodeFrameStyles';
import { NODE_CONTEXT_CONTROL_TRIGGER_CLASS } from '@/features/canvas/ui/nodeControlStyles';
import type { BrollVisualAttachment } from './brollCanvasBinding';

const fieldClass = 'nodrag mt-1 w-full rounded-[12px] border border-white/10 bg-surface-field px-3 py-2 text-text-dark outline-none focus:border-white/25';
const SELECTION_REQUIRED_ERROR = '先在台词里选中一段文字。';

interface Props {
  project: string;
  initialDraft: RemixScriptDraft;
  initialTopic?: string;
  existingRef: RemixArtifactRef | null;
  blueprintRef: RemixArtifactRef | null;
  blueprintSettings?: ReactNode;
  onEnsureBlueprint?: () => Promise<RemixArtifactRef>;
  onGenerateDraft?: (blueprintRef: RemixArtifactRef, topic: string) => Promise<RemixScriptDraft>;
  generationRunning?: boolean;
  generationPersistsDraft?: boolean;
  visualNodes: Array<{ id: string; label: string; mediaRevision?: string }>;
  performanceNodes?: Array<{ id: string; label: string; kind: 'video' | 'audio'; mediaRevision?: string }>;
  referenceNodes?: ReferenceMediaNode[];
  onCreateReference?: (mode: 'upload' | 'generate', segment: RemixPerformanceSegment, segmentIndex: number) => string;
  onConnectReference?: (nodeId: string) => void;
  onResolveTakeReferences?: (nodeId: string, segment: RemixPerformanceSegment) => RemixReferenceBinding[];
  onSetTakeReferences?: (nodeId: string, bindings: RemixReferenceBinding[]) => void;
  onCreateTake?: (segment: RemixPerformanceSegment) => string;
  onAttachTake?: (nodeId: string, source: RemixPerformanceSegment['source'], segment: RemixPerformanceSegment) => Promise<string>;
  missingPrimaryVisualCount?: number;
  onCreatePrimaryVisuals?: () => boolean;
  sfxNodes: Array<{ id: string; label: string }>;
  brollRequirements?: Array<{ event_id: string; required: boolean; purpose: string; suggested_query: string }>;
  brollDecisions?: Record<string, boolean>;
  brollPlanStatus?: 'running' | 'failed' | 'completed' | null;
  brollPlanError?: string | null;
  brollPlanCurrent?: boolean;
  onPlanBroll?: () => void;
  onBrollDecisionChange?: (eventId: string, required: boolean) => void;
  onCreateVisual?: (mode: 'generate' | 'generateVideo' | 'upload', selectionId: string, eventName: string, suggestedPrompt?: string) => { id: string; label: string; mediaRevision?: string } | null;
  onAttachVisual?: (nodeId: string) => Promise<string | BrollVisualAttachment>;
  onAutoSave?: (draft: RemixScriptDraft) => Promise<void>;
  onTopicChange?: (topic: string) => void;
  onDraftChange: (draft: RemixScriptDraft, changeKind?: 'content' | 'media-binding') => void;
  onSave: (ref: RemixArtifactRef, blueprintRef?: RemixArtifactRef) => void;
  onClose: () => void;
}

export function RemixScriptPanel({ project, initialDraft, initialTopic = '', existingRef, blueprintRef, blueprintSettings, onEnsureBlueprint, onGenerateDraft, generationRunning = false, generationPersistsDraft = false, visualNodes, performanceNodes = [], referenceNodes = [], onCreateReference, onConnectReference, onResolveTakeReferences, onSetTakeReferences, onCreateTake, onAttachTake, missingPrimaryVisualCount = 0, onCreatePrimaryVisuals, sfxNodes, brollRequirements = [], brollDecisions = {}, brollPlanStatus, brollPlanError, brollPlanCurrent = false, onPlanBroll, onBrollDecisionChange, onCreateVisual, onAttachVisual, onAutoSave, onTopicChange, onDraftChange, onClose }: Props) {
  const [draft, setDraft] = useState(initialDraft);
  const [autoSaving, setAutoSaving] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [newTopic, setNewTopic] = useState(initialTopic);
  const [error, setError] = useState<string | null>(null);
  const [batchSource, setBatchSource] = useState<{ segmentId: string; bindingIndex: number } | null>(null);
  const [batchTargets, setBatchTargets] = useState<string[]>([]);
  const [blueprintRole, setBlueprintRole] = useState<string | null>(null);
  const [eventPickerOpen, setEventPickerOpen] = useState(false);
  const [eventSelection, setEventSelection] = useState<{ start: number; end: number } | null>(null);
  const [pendingEventFocusId, setPendingEventFocusId] = useState<string | null>(null);
  const eventPickerRef = useRef<HTMLTextAreaElement>(null);
  const draftEditCount = useRef(0);
  const draftRef = useRef(draft);
  const visualBindingRequest = useRef(new Map<string, number>());
  const autoSaveTimer = useRef<number | null>(null);
  const pendingAutoSaveDraft = useRef<RemixScriptDraft | null>(null);
  const activeAutoSaves = useRef(0);
  const onAutoSaveRef = useRef(onAutoSave);
  onAutoSaveRef.current = onAutoSave;
  draftRef.current = draft;
  const currentBlueprintRef = useRef(blueprintRef);
  if (!onEnsureBlueprint) currentBlueprintRef.current = blueprintRef;

  useEffect(() => {
    if (!blueprintRef || !project) return;
    let active = true;
    fetchRemixArtifact(project, blueprintRef).then((artifact) => {
      if (active && artifact.schema === 'reference_blueprint.v1' && typeof artifact.payload.narrative_role === 'string') setBlueprintRole(artifact.payload.narrative_role);
    }).catch(() => { if (active) setBlueprintRole(null); });
    return () => { active = false; };
  }, [blueprintRef?.artifactId, blueprintRef?.revision, project]);

  useEffect(() => {
    if (draftRef.current === initialDraft) return;
    draftRef.current = initialDraft;
    setDraft(initialDraft);
  }, [initialDraft]);

  useEffect(() => { setEventSelection(null); }, [draft.dialogue]);
  useEffect(() => { if (eventPickerOpen) eventPickerRef.current?.focus(); }, [eventPickerOpen]);
  useEffect(() => {
    if (!pendingEventFocusId) return;
    const card = document.getElementById(`remix-event-${pendingEventFocusId}`);
    if (!card) return;
    card.scrollIntoView?.({ block: 'nearest' });
    card.focus({ preventScroll: true });
    setPendingEventFocusId(null);
  }, [draft.selections, pendingEventFocusId]);

  const persistDraft = async (next: RemixScriptDraft) => {
    const persist = onAutoSaveRef.current;
    if (!persist) return;
    pendingAutoSaveDraft.current = null;
    activeAutoSaves.current += 1;
    try { await persist(next); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '自动保存脚本失败'); }
    finally {
      activeAutoSaves.current -= 1;
      if (activeAutoSaves.current === 0 && !pendingAutoSaveDraft.current) setAutoSaving(false);
    }
  };
  const scheduleAutoSave = (next: RemixScriptDraft, immediate: boolean) => {
    if (!onAutoSaveRef.current) return;
    pendingAutoSaveDraft.current = next;
    setAutoSaving(true);
    if (autoSaveTimer.current !== null) window.clearTimeout(autoSaveTimer.current);
    autoSaveTimer.current = null;
    if (immediate) void persistDraft(next);
    else autoSaveTimer.current = window.setTimeout(() => {
      autoSaveTimer.current = null;
      const pending = pendingAutoSaveDraft.current;
      if (pending) void persistDraft(pending);
    }, 450);
  };
  useEffect(() => () => {
    if (autoSaveTimer.current !== null) window.clearTimeout(autoSaveTimer.current);
    const pending = pendingAutoSaveDraft.current;
    if (pending && onAutoSaveRef.current) void onAutoSaveRef.current(pending);
  }, []);
  const changeDraft = (next: RemixScriptDraft, changeKind: 'content' | 'media-binding' = 'content') => {
    draftEditCount.current += 1;
    draftRef.current = next;
    setDraft(next);
    if (changeKind === 'media-binding') onDraftChange(next, changeKind);
    else onDraftChange(next);
    scheduleAutoSave(next, changeKind === 'media-binding');
    setError(null);
  };
  const generate = async () => {
    if (generating || generationRunning || (!blueprintRef && !onEnsureBlueprint) || !newTopic.trim()) return;
    const editCount = draftEditCount.current;
    setGenerating(true); setError(null);
    try {
      const source = onEnsureBlueprint ? await onEnsureBlueprint() : blueprintRef;
      if (!source) throw new Error('请先完成当前脚本的改编规则。');
      currentBlueprintRef.current = source;
      const next = onGenerateDraft
        ? await onGenerateDraft(source, newTopic.trim())
        : await generateRemixScriptDraft(project, source, newTopic.trim());
      const current = currentBlueprintRef.current;
      if (!current || current.artifactId !== source.artifactId || current.revision !== source.revision) throw new Error('参考蓝图已更新，请重新生成脚本');
      if (draftEditCount.current !== editCount) throw new Error('生成期间台词已修改，请确认后重新生成');
      if (generationPersistsDraft) {
        draftRef.current = next;
        setDraft(next);
        setError(null);
      } else changeDraft(next);
    } catch (cause) { setError(cause instanceof Error ? cause.message : '生成脚本失败'); }
    finally { setGenerating(false); }
  };
  const markSelected = () => {
    const range = eventSelection;
    if (!range || !draft.dialogue.slice(range.start, range.end).trim()) { setError(SELECTION_REQUIRED_ERROR); return; }
    const start = Array.from(draft.dialogue.slice(0, range.start)).length;
    const end = Array.from(draft.dialogue.slice(0, range.end)).length;
    const index = draft.selections.length + 1;
    const added = { id: `event-${crypto.randomUUID()}`, label: index === 1 ? '产品登场' : `语义事件 ${index}`, start, end, source: 'manual' as const };
    const selections = [...draft.selections, added].sort((left, right) => left.start - right.start || left.end - right.end);
    setEventPickerOpen(false);
    setEventSelection(null);
    setPendingEventFocusId(added.id);
    changeDraft({ ...draft, selections });
  };
  const bindVisual = async (index: number, nodeId: string) => {
    setError(null);
    const selectionId = draft.selections[index]?.id;
    if (!selectionId) return;
    const request = (visualBindingRequest.current.get(selectionId) ?? 0) + 1;
    visualBindingRequest.current.set(selectionId, request);
    changeDraft({ ...draftRef.current, selections: draftRef.current.selections.map((item) => item.id === selectionId
      ? { ...item, visualNodeId: nodeId || undefined, visualMediaRevision: undefined }
      : item) }, 'media-binding');
    try {
      const knownRevision = visualNodes.find((node) => node.id === nodeId)?.mediaRevision;
      const attachment = nodeId && onAttachVisual ? await onAttachVisual(nodeId) : knownRevision;
      if (visualBindingRequest.current.get(selectionId) !== request) return;
      const latest = draftRef.current;
      const selectionStillUsesNode = latest.selections.some((item) => item.id === selectionId && item.visualNodeId === nodeId);
      if (!selectionStillUsesNode) return;
      const mediaRevision = typeof attachment === 'string' ? attachment : attachment?.mediaRevision;
      if (attachment && typeof attachment !== 'string') attachment.commit();
      const next = { ...latest, selections: latest.selections.map((item) => item.id === selectionId && item.visualNodeId === nodeId
        ? { ...item, visualMediaRevision: nodeId ? mediaRevision : undefined }
        : item) };
      changeDraft(next, 'media-binding');
    } catch (cause) {
      if (visualBindingRequest.current.get(selectionId) !== request) return;
      setError(cause instanceof Error ? cause.message : '无法引用当前画面素材');
    }
  };
  const createVisual = (index: number, mode: 'generate' | 'generateVideo' | 'upload') => {
    const selection = draft.selections[index];
    const suggestedPrompt = mode === 'generate' || mode === 'generateVideo'
      ? brollRequirements.find((item) => item.event_id === selection?.id)?.suggested_query
      : undefined;
    const created = selection && onCreateVisual?.(mode, selection.id, selection.label, suggestedPrompt);
    if (!created) return;
    const next = { ...draft, selections: draft.selections.map((item, position) => position === index
      ? { ...item, visualNodeId: created.id, visualMediaRevision: created.mediaRevision }
      : item) };
    changeDraft(next, 'media-binding');
  };
  const bindSfx = (index: number, nodeId: string) => {
    const next = { ...draft, selections: draft.selections.map((item, position) => position === index ? { ...item, sfxNodeId: nodeId || undefined } : item) };
    changeDraft(next, 'media-binding');
  };
  const updateSegment = (segmentId: string, patch: Partial<RemixPerformanceSegment>) => {
    const latest = draftRef.current;
    changeDraft({ ...latest, segments: latest.segments?.map((item) => item.id === segmentId ? { ...item, ...patch } : item) }, 'media-binding');
  };
  const updateReferenceBindings = (segment: RemixPerformanceSegment, bindings: RemixReferenceBinding[]) => {
    const prior = new Set(referenceBindingsForSegment(segment).map((binding) => binding.nodeId));
    bindings.filter(({ nodeId }) => !prior.has(nodeId) && referenceNodes.some((node) => node.id === nodeId))
      .forEach(({ nodeId }) => onConnectReference?.(nodeId));
    const idsChanged = JSON.stringify([...prior]) !== JSON.stringify(bindings.map((binding) => binding.nodeId));
    if (segment.takeNodeId && idsChanged) onSetTakeReferences?.(segment.takeNodeId, bindings);
    updateSegment(segment.id, { referenceBindings: bindings, ...(idsChanged ? { takeMediaRevision: undefined } : {}) });
  };
  const createReference = (segment: RemixPerformanceSegment, segmentIndex: number, mode: 'upload' | 'generate') => {
    const id = onCreateReference?.(mode, segment, segmentIndex);
    if (id) updateReferenceBindings(segment, [...referenceBindingsForSegment(segment), { nodeId: id, purpose: '' }]);
  };
  const applyReferencesToTargets = (source: RemixPerformanceSegment, bindingIndex: number) => {
    const binding = referenceBindingsForSegment(source)[bindingIndex];
    if (!binding) return;
    if (referenceNodes.some((node) => node.id === binding.nodeId)) onConnectReference?.(binding.nodeId);
    const selected = new Set(batchTargets);
    const latest = draftRef.current;
    changeDraft({ ...latest, segments: latest.segments?.map((segment) => {
      if (!selected.has(segment.id)) return segment;
      const current = referenceBindingsForSegment(segment);
      const referenceBindings = current.some((item) => item.nodeId === binding.nodeId)
        ? current.map((item) => item.nodeId === binding.nodeId ? { ...binding } : item)
        : [...current, { ...binding }];
      if (JSON.stringify(current) === JSON.stringify(referenceBindings)) return segment;
      const idsChanged = JSON.stringify(current.map((item) => item.nodeId))
        !== JSON.stringify(referenceBindings.map((item) => item.nodeId));
      if (segment.takeNodeId && idsChanged) onSetTakeReferences?.(segment.takeNodeId, referenceBindings);
      return { ...segment, referenceBindings, ...(idsChanged ? { takeMediaRevision: undefined } : {}) };
    }) }, 'media-binding');
    setBatchSource(null);
    setBatchTargets([]);
  };
  const chooseTake = async (segment: RemixPerformanceSegment, nodeId: string) => {
    const referenceBindings = nodeId ? onResolveTakeReferences?.(nodeId, segment) : undefined;
    const selected = { ...segment, takeNodeId: nodeId || undefined, takeMediaRevision: undefined,
      ...(referenceBindings ? { referenceBindings } : {}) };
    updateSegment(segment.id, selected);
    if (!nodeId || !onAttachTake) return;
    try {
      const revision = await onAttachTake(nodeId, segment.source, selected);
      const current = draftRef.current.segments?.find((item) => item.id === segment.id);
      if (current?.takeNodeId === nodeId && current.source === segment.source
        && JSON.stringify(referenceBindingsForSegment(current)) === JSON.stringify(referenceBindingsForSegment(selected)))
        updateSegment(segment.id, { takeMediaRevision: revision });
    } catch (cause) { setError(cause instanceof Error ? cause.message : '片段还未准备好，请生成后再试'); }
  };
  const createTake = (segment: RemixPerformanceSegment) => {
    const id = onCreateTake?.(segment);
    if (id) updateSegment(segment.id, { takeNodeId: id, takeMediaRevision: undefined });
  };
  const choosePrimary = async (segment: RemixPerformanceSegment, nodeId: string) => {
    updateSegment(segment.id, { primaryNodeId: nodeId || undefined, primaryMediaRevision: undefined });
    if (!nodeId || !onAttachVisual) return;
    try {
      onConnectReference?.(nodeId);
      const attachment = await onAttachVisual(nodeId);
      if (typeof attachment !== 'string') attachment.commit();
      if (draftRef.current.segments?.find((item) => item.id === segment.id)?.primaryNodeId === nodeId)
        updateSegment(segment.id, { primaryMediaRevision: typeof attachment === 'string' ? attachment : attachment.mediaRevision });
    } catch (cause) { setError(cause instanceof Error ? cause.message : '主画面尚未完成'); }
  };
  const addWholePerformance = (source: RemixPerformanceSegment['source']) => {
    if (!draft.dialogue.trim()) return;
    changeDraft({ ...draft, segments: [{
      id: 'segment-1', start: 0, end: Array.from(draft.dialogue).length,
      speaker: source === 'voiceover' ? '旁白' : '口播', source,
    }] });
  };
  const speakerSegments = seedPerformanceSegments(draft.dialogue);
  const canSplitSpeakers = speakerSegments.length > 0 && speakerSegments.every((segment) => segment.source === 'speaking_video');
  const singleVoiceover = draft.segments?.length === 1 && draft.segments[0].source === 'voiceover';
  const arollStatus = draft.segments?.length
    ? `已选 ${draft.segments.filter((segment) => segment.takeNodeId).length}/${draft.segments.length} 个片段`
    : '整段配音 · 未分段';
  const brollStatus = brollPlanStatus === 'running' ? '正在规划'
    : brollPlanStatus === 'failed' ? '规划失败'
      : brollPlanCurrent ? `已规划 ${brollRequirements.length} 个台词事件`
        : draft.selections.length ? `待规划 · ${draft.selections.length} 个台词事件` : '暂无台词事件';
  const selectedEventText = eventSelection ? draft.dialogue.slice(eventSelection.start, eventSelection.end).trim() : '';
  const primaryVisualAction = missingPrimaryVisualCount > 0 && onCreatePrimaryVisuals && <div className="mt-3 border-t border-white/[0.08] pt-3">
    <p className="text-text-muted">{missingPrimaryVisualCount} 个台词事件还没有可用画面。{draft.segments?.length ? '创建后，请在旁白段落中选择要使用的主画面。' : '使用音频节点配音时，可为这些事件准备主画面。'}画面在新节点中生成。</p>
    <button type="button" onClick={() => { if (onCreatePrimaryVisuals()) onClose(); }} className={`${NODE_CONTEXT_CONTROL_TRIGGER_CLASS} mt-2`}>创建缺少的主画面视频节点</button>
  </div>;

  return <div role="dialog" aria-modal="true" aria-label="改编脚本与语义区间" className={`nodrag flex max-h-[88vh] w-[min(840px,94vw)] flex-col overflow-hidden rounded-[12px] text-text-dark shadow-2xl ${CANVAS_NODE_OPS_PANEL_CLASS}`} onClick={(event) => event.stopPropagation()}>
    <div className="flex items-center justify-between border-b border-white/10 px-4 py-3"><h2 className="text-sm font-semibold">改编脚本与语义区间</h2><button type="button" onClick={onClose} className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS}>关闭</button></div>
    <div className="ui-scrollbar space-y-3 overflow-auto p-4 text-xs">
      {blueprintSettings}
      {!blueprintSettings && blueprintRef && <p className="text-text-muted">已关联参考蓝图 · 版本 {blueprintRef.revision}</p>}
      {!blueprintSettings && blueprintRole && <p className="rounded-[12px] border border-white/10 bg-surface-field p-3 text-text-muted">{blueprintRole}</p>}
      {(blueprintRef || onEnsureBlueprint) && <div>
        <label htmlFor="remix-new-topic">新主题</label>
        <div className="flex items-center gap-2">
          <input id="remix-new-topic" aria-label="新主题" value={newTopic} onChange={(event) => { const next = event.target.value; setNewTopic(next); onTopicChange?.(next); }} placeholder="例如：把咖啡改编为新品饮料" className={`${fieldClass} min-w-0 flex-1`} />
          <button type="button" disabled={generating || generationRunning || !newTopic.trim()} onClick={() => void generate()} className="nodrag mt-1 inline-flex h-9 shrink-0 items-center justify-center rounded-[12px] bg-primary px-4 text-xs font-semibold text-primary-foreground transition-colors hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50">{generating || generationRunning ? '生成中…' : '生成改编台词'}</button>
        </div>
        <p className="mt-1.5 text-text-muted">填写新主题后，点击右侧按钮生成台词</p>
      </div>}
      <label className="block">改编台词<textarea aria-label="改编台词" value={draft.dialogue} onChange={(event) => changeDraft(editRemixDialogue(draft, event.target.value))} className={`${fieldClass} min-h-28 text-sm`} /></label>
      <section aria-label="A-roll 主画面与声音" className="rounded-[12px] border border-white/10 bg-white/[0.03] p-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div><div className="flex items-center gap-2"><span aria-hidden="true" className="rounded-md bg-[rgb(var(--accent-rgb)/0.14)] px-1.5 py-0.5 font-mono text-[11px] text-[rgb(var(--accent-rgb))]">01</span><h3 className="font-medium">主画面与声音</h3><span className="text-base font-semibold text-text-dark">A-roll</span></div><p className="mt-1 text-text-muted">先决定台词由谁说、成片使用哪段基础画面和声音。素材在对应的画布节点制作。</p><p className="mt-1 text-[11px] text-text-muted">{arollStatus}</p></div>
          {!!draft.segments?.length && <button type="button" onClick={() => changeDraft({ ...draft, segments: [] })} className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS}>不分段，使用音频节点</button>}
        </div>
        {!draft.segments?.length && <div className="mt-3 rounded-[10px] border border-white/[0.08] bg-surface-field p-3">
          <p className="font-medium">当前：整段配音</p>
          <p className="mt-1 text-text-muted">生成台词不会替你决定口播视频还是独立旁白。此时可在音频节点制作旁白；想让人物出镜说话，再选择下面的方式。</p>
          <div className="mt-3 flex flex-wrap gap-2">
            <button type="button" disabled={!draft.dialogue.trim()} onClick={() => addWholePerformance('speaking_video')} className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS}>制作整段口播视频</button>
            <button type="button" disabled={!draft.dialogue.trim()} onClick={() => addWholePerformance('voiceover')} className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS}>制作独立旁白片段</button>
            {canSplitSpeakers && <button type="button" onClick={() => changeDraft({ ...draft, segments: speakerSegments })} className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS}>按说话者拆段</button>}
          </div>
          {primaryVisualAction}
        </div>}
        {draft.segments?.map((segment, index) => {
          const text = spokenTextForSegment(draft.dialogue, segment).trim();
          const isWhole = segment.start === 0 && segment.end === Array.from(draft.dialogue).length;
          const choices = performanceNodes.filter((node) => node.kind === (segment.source === 'voiceover' ? 'audio' : 'video'));
          const referenceCount = referenceBindingsForSegment(segment).length;
          const modeHint = segment.source === 'speaking_video'
            ? '人物在视频里说这段台词，成片使用视频自带的声音。'
            : segment.source === 'voiceover'
              ? '单独录制或生成声音，并指定旁白播放时显示的主画面。'
              : '直接使用原片片段及原声；请确认原声台词与改编台词相符。';
          return <div key={segment.id} className="mt-3 rounded-[10px] border border-white/[0.08] bg-surface-field p-3">
            <div className="flex items-center justify-between gap-2"><div className="flex items-baseline gap-2"><span className="font-mono text-[11px] text-text-muted">{String(index + 1).padStart(2, '0')}</span><strong>{isWhole ? '整段台词' : segment.speaker}</strong></div><span className="text-text-muted">{Array.from(text).length} 字</span></div>
            <details className="mt-2 text-text-muted"><summary className="cursor-pointer">查看本段台词</summary><p className="mt-2 whitespace-pre-wrap break-words text-text-dark/80">{text || '当前段落没有台词'}</p></details>
            {segment.source === 'speaking_video' && draft.segments?.[index + 1]?.source === 'speaking_video'
              && <button type="button" onClick={() => changeDraft(mergeAdjacentPerformanceSegments(draft, segment.id), 'media-binding')} className={`${NODE_CONTEXT_CONTROL_TRIGGER_CLASS} mt-2`}>与下一段共用一个视频片段</button>}
            <div className="mt-3 border-t border-white/[0.08] pt-3">
              <label>这段台词怎么呈现？<select aria-label={`${segment.speaker} 声音来源`} value={segment.source} onChange={(event) => updateSegment(segment.id, { source: event.target.value as RemixPerformanceSegment['source'], takeNodeId: undefined, takeMediaRevision: undefined })} className={fieldClass}>
                <option value="speaking_video">人物出镜说话 · 视频自带声音</option><option value="voiceover">画面配旁白 · 单独制作声音</option><option value="original_sound">沿用原片片段和原声</option>
              </select></label>
              <p className="mt-1 text-text-muted">{modeHint}</p>
            </div>
            {segment.source !== 'original_sound' && <details className="mt-3 rounded-[8px] border border-white/[0.08] px-3 py-2">
              <summary className="cursor-pointer text-text-dark">参考素材（可选）{referenceCount ? ` · 已选 ${referenceCount} 项` : ''}</summary>
              <p className="mt-1 text-text-muted">这里同步当前片段节点连入的图片、视频和音频；用途备注可以留空。{segment.source === 'voiceover' ? '音色仍需在音频节点中选择。' : '也可以不添加，由视频节点的提示词决定画面。'}</p>
              <SegmentReferenceEditor bindings={referenceBindingsForSegment(segment)} nodes={referenceNodes.filter((node) => node.id !== segment.takeNodeId)} onChange={(bindings) => updateReferenceBindings(segment, bindings)}
                onApply={(bindingIndex) => { setBatchSource({ segmentId: segment.id, bindingIndex }); setBatchTargets([]); }} />
              {onCreateReference && <div className="mt-2 flex gap-1"><button type="button" onClick={() => createReference(segment, index, 'upload')} className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS}>创建上传节点</button><button type="button" onClick={() => createReference(segment, index, 'generate')} className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS}>创建图片节点</button></div>}
              {(draft.segments?.length ?? 0) > 1 && <div className="mt-2 border-t border-white/[0.08] pt-2">
                {batchSource?.segmentId === segment.id && <div className="mt-2 space-y-2">
                  <p className="text-text-muted">将参考 {batchSource.bindingIndex + 1} 添加到勾选的段落，保留它们已有的其他参考。发生变化的片段需要重新制作。</p>
                  <div className="flex flex-wrap gap-3">{(draft.segments ?? []).filter((candidate) => candidate.id !== segment.id && candidate.source === segment.source).map((candidate) => <label key={candidate.id} className="flex items-center gap-1"><input type="checkbox" aria-label={`应用到${candidate.speaker}`} checked={batchTargets.includes(candidate.id)} onChange={(event) => setBatchTargets(event.target.checked ? [...batchTargets, candidate.id] : batchTargets.filter((id) => id !== candidate.id))} />{candidate.speaker}</label>)}</div>
                  <button type="button" disabled={!batchTargets.length} onClick={() => applyReferencesToTargets(segment, batchSource.bindingIndex)} className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS}>应用到选中段落</button>
                </div>}
              </div>}
            </details>}
            {segment.source === 'voiceover' && <label className="mt-3 block">旁白播放时显示的主画面<select aria-label={`${segment.speaker} 主画面`} value={segment.primaryNodeId ?? ''} onChange={(event) => void choosePrimary(segment, event.target.value)} className={fieldClass}>
              <option value="">请选择生成或上传的视频／图片</option>{referenceNodes.filter((node) => node.kind !== 'audio').map((node) => <option key={node.id} value={node.id}>{node.label}</option>)}
            </select></label>}
            <div className="mt-3 border-t border-white/[0.08] pt-3">
              <label>使用哪个{segment.source === 'voiceover' ? '音频' : '视频'}片段？<select aria-label={`${segment.speaker} 采用片段`} value={segment.takeNodeId ?? ''} onChange={(event) => void chooseTake(segment, event.target.value)} className={fieldClass}>
                <option value="">尚未选择片段</option>{choices.map((node) => <option key={node.id} value={node.id}>{node.label}{node.mediaRevision ? '' : ' · 待完成'}</option>)}
              </select></label>
              <div className="mt-2 flex flex-wrap items-center gap-2"><button type="button" onClick={() => createTake(segment)} className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS}>{segment.source === 'voiceover' ? '创建旁白节点' : segment.source === 'original_sound' ? '创建原声上传节点' : '创建口播视频节点'}</button>
                {singleVoiceover && <button type="button" onClick={() => updateSegment(segment.id, { source: 'speaking_video', speaker: '口播', takeNodeId: undefined, takeMediaRevision: undefined, primaryNodeId: undefined, primaryMediaRevision: undefined })} className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS}>改成整段口播视频</button>}
                {segment.takeNodeId && <button type="button" onClick={() => void chooseTake(segment, segment.takeNodeId!)} className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS}>检查并绑定素材</button>}
                <span className={segment.takeMediaRevision ? 'text-emerald-300' : 'text-text-muted'}>{segment.takeMediaRevision ? '素材已绑定' : '尚未完成片段'}</span>
              </div>
              <p className="mt-1 text-text-muted">此按钮会在画布创建节点，生成{segment.source === 'voiceover' ? '音频' : '视频'}请到该节点操作。</p>
            </div>
          </div>;
        })}
        {draft.segments?.some((segment) => segment.source === 'voiceover') && primaryVisualAction}
      </section>
      <section aria-label="B-roll 补充镜头与音效" className="rounded-[12px] border border-white/10 bg-white/[0.03] p-3">
        <div className="flex items-center gap-2"><span aria-hidden="true" className="rounded-md bg-[rgb(var(--accent-rgb)/0.14)] px-1.5 py-0.5 font-mono text-[11px] text-[rgb(var(--accent-rgb))]">02</span><h3 className="font-medium">补充镜头与音效</h3><span className="text-base font-semibold text-text-dark">B-roll</span><span className="rounded-full border border-white/10 px-2 py-0.5 text-[11px] text-text-muted">可选</span></div>
        <p className="mt-1 text-text-muted">在基础画面的台词位置，决定是否插入补充镜头或音效。可以先规划建议，也可以直接手动设置。</p>
        <p className="mt-1 text-[11px] text-text-muted">{brollStatus}</p>
      <div className="mt-3 border-t border-white/10 pt-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className="font-medium text-text-dark">台词事件（{draft.selections.length}）</h3>
          <div className="flex flex-wrap items-center gap-2"><button type="button" disabled={!draft.dialogue.trim()} onClick={() => { setEventPickerOpen(!eventPickerOpen); setEventSelection(null); setError(null); }} className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS}>{eventPickerOpen ? '收起台词选择' : '从台词添加事件'}</button>
          {onPlanBroll && <button type="button"
            disabled={!existingRef || autoSaving || generating || generationRunning || !draft.selections.length || brollPlanStatus === 'running'}
            onClick={onPlanBroll} className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS}>
            {brollPlanStatus === 'running' ? '规划 B-roll 中…' : brollPlanStatus === 'failed' ? '重试规划 B-roll' : brollPlanCurrent ? '重新规划 B-roll' : '规划 B-roll'}
          </button>}</div>
        </div>
        {eventPickerOpen && <div className="mt-3 rounded-[10px] border border-white/[0.08] bg-surface-field p-3">
          <p className="text-text-muted">在这段台词里选中文字，添加后即可为事件设置补充画面或音效。台词内容与上方编辑框同步。</p>
          <textarea ref={eventPickerRef} aria-label="选择台词片段" readOnly value={draft.dialogue} onSelect={(event) => { const { selectionStart: start, selectionEnd: end } = event.currentTarget; setEventSelection(start < end ? { start, end } : null); }} className={`${fieldClass} min-h-40 max-h-60 resize-y text-sm`} />
          <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
            <p aria-live="polite" className="min-w-0 flex-1 truncate text-text-muted">{selectedEventText ? `已选台词：${selectedEventText}` : '尚未选中台词'}</p>
            <button type="button" disabled={!selectedEventText} onClick={markSelected} className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS}>添加选中台词</button>
          </div>
          {error === SELECTION_REQUIRED_ERROR && <p role="alert" className="mt-1 text-red-300">{error}</p>}
        </div>}
        <p className="mt-1 text-text-muted">AI 已根据台词自动拆分；手动事件可从这里选择台词添加。</p>
        {onPlanBroll && <p className="mt-1 text-text-muted">规划仅提供建议，素材仍由你在事件中选择或生成。</p>}
        {onPlanBroll && <p className="mt-1 text-text-muted">
          {generating || generationRunning ? '正在生成新台词，完成后可规划补充画面。'
            : autoSaving ? '正在保存当前台词，保存后可规划补充画面。'
            : !existingRef ? '保存当前台词后即可分析补充画面。'
              : brollPlanStatus === 'running' ? '正在逐个分析台词事件…'
                : brollPlanCurrent ? `已分析 ${brollRequirements.length} 个事件，建议 ${brollRequirements.filter((item) => item.required).length} 个补充画面。`
                  : '可让 AI 判断各事件是否需要补充画面，也可以直接在下方选择素材。'}
        </p>}
        {brollPlanStatus === 'failed' && brollPlanError && <p role="alert" className="mt-1 text-red-300">{brollPlanError}</p>}
      </div>
      <div className="mt-3 space-y-2">
      {draft.selections.map((selection, index) => {
        const requirement = brollRequirements.find((item) => item.event_id === selection.id);
        const selectedDialogue = Array.from(draft.dialogue).slice(Math.max(0, selection.start), Math.max(0, selection.end)).join('');
        return <div key={selection.id} id={`remix-event-${selection.id}`} tabIndex={-1} className="grid grid-cols-[1fr_100px_100px_1fr_1fr_auto] items-end gap-2 rounded-[12px] border border-white/10 bg-white/[0.03] p-3 outline-none focus:border-[rgb(var(--accent-rgb)/0.55)]">
        {brollPlanCurrent && requirement && <div className="col-span-6 space-y-1">
          <p className="text-text-muted">AI 建议：{requirement.required ? '需要补充画面' : '无需补充画面'}{requirement.purpose ? ` · ${requirement.purpose}` : ''}{requirement.suggested_query ? ` · 建议提示词：${requirement.suggested_query}` : ''}</p>
          {onBrollDecisionChange && <label className="flex items-center gap-2 text-text-dark"><input type="checkbox" aria-label={`${selection.label}需要 B-roll`} checked={brollDecisions[selection.id] ?? requirement.required} onChange={(event) => onBrollDecisionChange(selection.id, event.target.checked)} />此事件需要 B-roll</label>}
        </div>}
        <label><span className="flex items-center justify-between gap-2">事件名称<span className="rounded-full border border-white/10 bg-white/[0.04] px-2 py-0.5 text-[10px] text-text-muted">{selection.source === 'manual' || (!selection.source && selection.id.startsWith('event-')) ? '手动添加' : 'AI 生成'}</span></span><input aria-label={`事件 ${index + 1} 名称`} value={selection.label} onChange={(event) => changeDraft({ ...draft, selections: draft.selections.map((item, position) => position === index ? { ...item, label: event.target.value } : item) })} className={fieldClass} /></label>
        <label>起始字<input aria-label={`${selection.label} 起始字`} type="number" value={selection.start} onChange={(event) => changeDraft({ ...draft, selections: draft.selections.map((item, position) => position === index ? { ...item, start: Number(event.target.value), invalidReason: undefined } : item) })} className={fieldClass} /></label>
        <label>结束字<input aria-label={`${selection.label} 结束字`} type="number" value={selection.end} onChange={(event) => changeDraft({ ...draft, selections: draft.selections.map((item, position) => position === index ? { ...item, end: Number(event.target.value), invalidReason: undefined } : item) })} className={fieldClass} /></label>
        <label>补充画面（B-roll）<select id={`remix-visual-${selection.id}`} aria-label={`${selection.label}画面`} value={selection.visualNodeId ?? ''} onChange={(event) => void bindVisual(index, event.target.value)} className={fieldClass}><option value="">仅字幕</option>{visualNodes.map((node) => <option key={node.id} value={node.id}>{node.label}{node.mediaRevision ? '' : ' · 待生成/上传'}</option>)}</select></label>
        <label>音效<select aria-label={`${selection.label}音效`} value={selection.sfxNodeId ?? ''} onChange={(event) => bindSfx(index, event.target.value)} className={fieldClass}><option value="">无音效</option>{sfxNodes.map((node) => <option key={node.id} value={node.id}>{node.label}</option>)}</select></label>
        <button type="button" onClick={() => changeDraft({ ...draft, selections: draft.selections.filter((_, position) => position !== index) })} className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS}>删除</button>
        <p className="col-span-6 rounded-[8px] border border-white/[0.08] bg-surface-field px-3 py-2 leading-5 text-text-muted">对应台词<span className="ml-2 whitespace-pre-wrap break-words text-text-dark">{selectedDialogue || '当前区间没有台词'}</span></p>
        <div className="col-span-6 flex items-center gap-2">
          <button type="button" aria-label={`生成 ${selection.label} 图片素材`} onClick={() => createVisual(index, 'generate')} className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS}>生成图片</button>
          <button type="button" aria-label={`生成 ${selection.label} 视频素材`} onClick={() => createVisual(index, 'generateVideo')} className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS}>生成视频</button>
          <button type="button" aria-label={`上传 ${selection.label} 素材`} onClick={() => createVisual(index, 'upload')} className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS}>上传</button>
          <button type="button" aria-label={`替换 ${selection.label} 素材`} onClick={() => selection.visualNodeId ? void bindVisual(index, selection.visualNodeId) : document.getElementById(`remix-visual-${selection.id}`)?.focus()} className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS}>替换素材</button>
          {selection.visualNodeId && !selection.visualMediaRevision && <span className="text-amber-300">{autoSaving ? '补充画面节点已创建，正在自动保存脚本…' : '补充画面节点已创建，脚本已自动保存。稍后在画布完成素材后会自动关联。'}</span>}
        </div>
        {selection.invalidReason && <p className="col-span-6 text-amber-300">台词已改变，请重新标记这个区间。</p>}
      </div>})}
      </div>
      </section>
      {error && error !== SELECTION_REQUIRED_ERROR && <p role="alert" className="text-red-300">{error}</p>}
    </div>
  </div>;
}
