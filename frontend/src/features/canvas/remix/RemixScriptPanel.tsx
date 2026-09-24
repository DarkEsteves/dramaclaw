import { useEffect, useRef, useState, type ReactNode } from 'react';
import { fetchRemixArtifact, generateRemixScriptDraft, type RemixArtifactRef } from '@/api/remix';
import { editRemixDialogue, seedPerformanceSegments, type RemixPerformanceSegment, type RemixScriptDraft } from './remixScriptModel';
import { CANVAS_NODE_OPS_PANEL_CLASS } from '@/features/canvas/ui/nodeFrameStyles';
import { NODE_CONTEXT_CONTROL_TRIGGER_CLASS } from '@/features/canvas/ui/nodeControlStyles';
import type { BrollVisualAttachment } from './brollCanvasBinding';

const fieldClass = 'nodrag mt-1 w-full rounded-[12px] border border-white/10 bg-surface-field px-3 py-2 text-text-dark outline-none focus:border-white/25';

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
  referenceNodes?: Array<{ id: string; label: string }>;
  voiceNodes?: Array<{ id: string; label: string }>;
  motionNodes?: Array<{ id: string; label: string }>;
  onCreateReference?: (kind: 'person' | 'product' | 'setting', mode: 'upload' | 'generate') => string;
  onConnectReference?: (nodeId: string) => void;
  onCreateTake?: (segment: RemixPerformanceSegment) => string;
  onAttachTake?: (nodeId: string, source: RemixPerformanceSegment['source']) => Promise<string>;
  sfxNodes: Array<{ id: string; label: string }>;
  brollRequirements?: Array<{ event_id: string; required: boolean; purpose: string; suggested_query: string }>;
  onCreateVisual?: (mode: 'generate' | 'generateVideo' | 'upload', selectionId: string, eventName: string, suggestedPrompt?: string) => { id: string; label: string; mediaRevision?: string } | null;
  onAttachVisual?: (nodeId: string) => Promise<string | BrollVisualAttachment>;
  onAutoSave?: (draft: RemixScriptDraft) => Promise<void>;
  onTopicChange?: (topic: string) => void;
  onDraftChange: (draft: RemixScriptDraft, changeKind?: 'content' | 'media-binding') => void;
  onSave: (ref: RemixArtifactRef, blueprintRef?: RemixArtifactRef) => void;
  onClose: () => void;
}

export function RemixScriptPanel({ project, initialDraft, initialTopic = '', blueprintRef, blueprintSettings, onEnsureBlueprint, onGenerateDraft, generationRunning = false, generationPersistsDraft = false, visualNodes, performanceNodes = [], referenceNodes = [], voiceNodes = [], motionNodes = [], onCreateReference, onConnectReference, onCreateTake, onAttachTake, sfxNodes, brollRequirements = [], onCreateVisual, onAttachVisual, onAutoSave, onTopicChange, onDraftChange, onClose }: Props) {
  const [draft, setDraft] = useState(initialDraft);
  const [autoSaving, setAutoSaving] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [newTopic, setNewTopic] = useState(initialTopic);
  const [error, setError] = useState<string | null>(null);
  const [blueprintRole, setBlueprintRole] = useState<string | null>(null);
  const dialogueRef = useRef<HTMLTextAreaElement>(null);
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
    const field = dialogueRef.current;
    if (!field || field.selectionStart === field.selectionEnd) { setError('先在台词里选中一段文字。'); return; }
    const start = Array.from(field.value.slice(0, field.selectionStart)).length;
    const end = Array.from(field.value.slice(0, field.selectionEnd)).length;
    const index = draft.selections.length + 1;
    const added = { id: `event-${crypto.randomUUID()}`, label: index === 1 ? '产品登场' : `语义事件 ${index}`, start, end, source: 'manual' as const };
    const selections = [...draft.selections, added].sort((left, right) => left.start - right.start || left.end - right.end);
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
  const chooseReference = (segment: RemixPerformanceSegment, key: 'personNodeId' | 'productNodeId' | 'settingNodeId' | 'motionNodeId' | 'voiceNodeId', nodeId: string) => {
    if (nodeId) onConnectReference?.(nodeId);
    updateSegment(segment.id, { [key]: nodeId || undefined, takeNodeId: undefined, takeMediaRevision: undefined });
  };
  const createReference = (segment: RemixPerformanceSegment, kind: 'person' | 'product' | 'setting', mode: 'upload' | 'generate') => {
    const id = onCreateReference?.(kind, mode);
    if (!id) return;
    updateSegment(segment.id, { [`${kind}NodeId`]: id, takeNodeId: undefined, takeMediaRevision: undefined });
  };
  const chooseTake = async (segment: RemixPerformanceSegment, nodeId: string) => {
    updateSegment(segment.id, { takeNodeId: nodeId || undefined, takeMediaRevision: undefined });
    if (!nodeId || !onAttachTake) return;
    try {
      const revision = await onAttachTake(nodeId, segment.source);
      if (draftRef.current.segments?.find((item) => item.id === segment.id)?.takeNodeId === nodeId) updateSegment(segment.id, { takeMediaRevision: revision });
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
      <label className="block">改编台词<textarea ref={dialogueRef} aria-label="改编台词" value={draft.dialogue} onChange={(event) => changeDraft(editRemixDialogue(draft, event.target.value))} className={`${fieldClass} min-h-28 text-sm`} /></label>
      <section className="rounded-[12px] border border-white/10 bg-white/[0.03] p-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div><h3 className="font-medium">主画面与声音</h3><p className="mt-1 text-text-muted">决定谁说这段台词，以及成片显示什么画面。补充镜头（B-roll）在下方“台词事件”设置；实际生成在画布节点完成。</p></div>
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
        </div>}
        {draft.segments?.map((segment, index) => {
          const text = Array.from(draft.dialogue).slice(segment.start, segment.end).join('').trim();
          const isWhole = segment.start === 0 && segment.end === Array.from(draft.dialogue).length;
          const choices = performanceNodes.filter((node) => node.kind === (segment.source === 'voiceover' ? 'audio' : 'video'));
          const referenceCount = [segment.personNodeId, segment.productNodeId, segment.settingNodeId, segment.voiceNodeId, segment.motionNodeId].filter(Boolean).length;
          const modeHint = segment.source === 'speaking_video'
            ? '人物在视频里说这段台词，成片使用视频自带的声音。'
            : segment.source === 'voiceover'
              ? '单独录制或生成声音，并指定旁白播放时显示的主画面。'
              : '直接使用原片片段及原声；请确认原声台词与改编台词相符。';
          return <div key={segment.id} className="mt-3 rounded-[10px] border border-white/[0.08] bg-surface-field p-3">
            <div className="flex items-center justify-between gap-2"><div className="flex items-baseline gap-2"><span className="font-mono text-[11px] text-text-muted">{String(index + 1).padStart(2, '0')}</span><strong>{isWhole ? '整段台词' : segment.speaker}</strong></div><span className="text-text-muted">{Array.from(text).length} 字</span></div>
            <details className="mt-2 text-text-muted"><summary className="cursor-pointer">查看本段台词</summary><p className="mt-2 whitespace-pre-wrap break-words text-text-dark/80">{text || '当前段落没有台词'}</p></details>
            <div className="mt-3 border-t border-white/[0.08] pt-3">
              <label>这段台词怎么呈现？<select aria-label={`${segment.speaker} 声音来源`} value={segment.source} onChange={(event) => updateSegment(segment.id, { source: event.target.value as RemixPerformanceSegment['source'], takeNodeId: undefined, takeMediaRevision: undefined })} className={fieldClass}>
                <option value="speaking_video">人物出镜说话 · 视频自带声音</option><option value="voiceover">画面配旁白 · 单独制作声音</option><option value="original_sound">沿用原片片段和原声</option>
              </select></label>
              <p className="mt-1 text-text-muted">{modeHint}</p>
            </div>
            {segment.source === 'speaking_video' && <details className="mt-3 rounded-[8px] border border-white/[0.08] px-3 py-2">
              <summary className="cursor-pointer text-text-dark">更换人物、产品和场景（可选）{referenceCount ? ` · 已选 ${referenceCount} 项参考` : ''}</summary>
              <p className="mt-1 text-text-muted">为新视频提供参考素材；不选则由视频节点中的提示词决定画面。</p>
              <div className="mt-2 grid gap-2 sm:grid-cols-3">{([['personNodeId', '人物', 'person'], ['productNodeId', '产品', 'product'], ['settingNodeId', '场景', 'setting']] as const).map(([key, label, kind]) => <div key={key}>
                <label>{label}参考<select aria-label={`${segment.speaker} ${label}参考`} value={segment[key] ?? ''} onChange={(event) => chooseReference(segment, key, event.target.value)} className={fieldClass}><option value="">不指定</option>{referenceNodes.map((node) => <option key={node.id} value={node.id}>{node.label}</option>)}</select></label>
                <div className="mt-1 flex gap-1"><button type="button" onClick={() => createReference(segment, kind, 'upload')} className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS}>上传</button><button type="button" onClick={() => createReference(segment, kind, 'generate')} className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS}>生成</button></div>
              </div>)}</div>
              <div className="mt-2 grid gap-2 sm:grid-cols-2">
                <label>声音参考（可选）<select aria-label={`${segment.speaker} 声音参考`} value={segment.voiceNodeId ?? ''} onChange={(event) => chooseReference(segment, 'voiceNodeId', event.target.value)} className={fieldClass}>
                  <option value="">不指定</option>{voiceNodes.map((node) => <option key={node.id} value={node.id}>{node.label}</option>)}
                </select></label>
                <label>原片镜头参考（可选）<select aria-label={`${segment.speaker} 镜头参考`} value={segment.motionNodeId ?? ''} onChange={(event) => chooseReference(segment, 'motionNodeId', event.target.value)} className={fieldClass}>
                  <option value="">只用镜头描述</option>{motionNodes.map((node) => <option key={node.id} value={node.id}>{node.label}</option>)}
                </select></label>
              </div>
            </details>}
            {segment.source === 'voiceover' && <label className="mt-3 block">旁白播放时显示的主画面<select aria-label={`${segment.speaker} 主画面`} value={segment.primaryNodeId ?? ''} onChange={(event) => void choosePrimary(segment, event.target.value)} className={fieldClass}>
              <option value="">请选择生成或上传的视频／图片</option>{referenceNodes.map((node) => <option key={node.id} value={node.id}>{node.label}</option>)}
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
      </section>
      <button type="button" onClick={markSelected} className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS}>将选中台词添加为事件</button>
      <p className="text-text-muted">选中一段台词后添加为语义事件，用来指定这段台词需要搭配的补充画面（B-roll）或音效。</p>
      <div className="border-t border-white/10 pt-3">
        <h3 className="font-medium text-text-dark">台词事件（{draft.selections.length}）</h3>
        <p className="mt-1 text-text-muted">AI 已根据台词自动拆分，你也可以选中台词手动补充事件。</p>
      </div>
      {draft.selections.map((selection, index) => {
        const requirement = brollRequirements.find((item) => item.event_id === selection.id);
        const selectedDialogue = Array.from(draft.dialogue).slice(Math.max(0, selection.start), Math.max(0, selection.end)).join('');
        return <div key={selection.id} className="grid grid-cols-[1fr_100px_100px_1fr_1fr_auto] items-end gap-2 rounded-[12px] border border-white/10 bg-white/[0.03] p-3">
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
        {requirement?.required && <p className="col-span-6 text-amber-300">必需补充画面（B-roll）· {requirement.purpose} · 建议提示词：{requirement.suggested_query}</p>}
        {selection.invalidReason && <p className="col-span-6 text-amber-300">台词已改变，请重新标记这个区间。</p>}
      </div>})}
      {error && <p role="alert" className="text-red-300">{error}</p>}
    </div>
  </div>;
}
