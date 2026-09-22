import { useEffect, useRef, useState, type ReactNode } from 'react';
import { fetchRemixArtifact, generateRemixScriptDraft, type RemixArtifactRef } from '@/api/remix';
import { saveRemixScriptArtifact } from './remixScriptArtifact';
import { editRemixDialogue, type RemixScriptDraft } from './remixScriptModel';
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
  visualNodes: Array<{ id: string; label: string; mediaRevision?: string }>;
  sfxNodes: Array<{ id: string; label: string }>;
  brollRequirements?: Array<{ event_id: string; required: boolean; purpose: string; suggested_query: string }>;
  onCreateVisual?: (mode: 'generate' | 'upload', selectionId: string) => { id: string; label: string; mediaRevision?: string } | null;
  onAttachVisual?: (nodeId: string) => Promise<string | BrollVisualAttachment>;
  onTopicChange?: (topic: string) => void;
  onDraftChange: (draft: RemixScriptDraft) => void;
  onSave: (ref: RemixArtifactRef, blueprintRef?: RemixArtifactRef) => void;
  onClose: () => void;
}

export function RemixScriptPanel({ project, initialDraft, initialTopic = '', existingRef, blueprintRef, blueprintSettings, onEnsureBlueprint, visualNodes, sfxNodes, brollRequirements = [], onCreateVisual, onAttachVisual, onTopicChange, onDraftChange, onSave, onClose }: Props) {
  const [draft, setDraft] = useState(initialDraft);
  const [saving, setSaving] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [newTopic, setNewTopic] = useState(initialTopic);
  const [error, setError] = useState<string | null>(null);
  const [blueprintRole, setBlueprintRole] = useState<string | null>(null);
  const dialogueRef = useRef<HTMLTextAreaElement>(null);
  const draftEditCount = useRef(0);
  const draftRef = useRef(draft);
  const visualBindingRequest = useRef(new Map<string, number>());
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

  const changeDraft = (next: RemixScriptDraft) => { draftEditCount.current += 1; draftRef.current = next; setDraft(next); onDraftChange(next); setError(null); };
  const generate = async () => {
    if (generating || saving || (!blueprintRef && !onEnsureBlueprint) || !newTopic.trim()) return;
    const editCount = draftEditCount.current;
    setGenerating(true); setError(null);
    try {
      const source = onEnsureBlueprint ? await onEnsureBlueprint() : blueprintRef;
      if (!source) throw new Error('请先完成当前脚本的改编规则。');
      currentBlueprintRef.current = source;
      const next = await generateRemixScriptDraft(project, source, newTopic.trim());
      const current = currentBlueprintRef.current;
      if (!current || current.artifactId !== source.artifactId || current.revision !== source.revision) throw new Error('参考蓝图已更新，请重新生成脚本');
      if (draftEditCount.current !== editCount) throw new Error('生成期间台词已修改，请确认后重新生成');
      changeDraft(next);
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
  const save = async () => {
    if (saving) return;
    setSaving(true);
    setError(null);
    try {
      const branchRef = onEnsureBlueprint ? await onEnsureBlueprint() : blueprintRef;
      const ref = await saveRemixScriptArtifact(project, draft, existingRef, branchRef);
      if (onEnsureBlueprint && branchRef) onSave(ref, branchRef);
      else onSave(ref);
    } catch (cause) { setError(cause instanceof Error ? cause.message : '保存脚本失败'); }
    finally { setSaving(false); }
  };
  const bindVisual = async (index: number, nodeId: string) => {
    setError(null);
    const selectionId = draft.selections[index]?.id;
    if (!selectionId) return;
    const request = (visualBindingRequest.current.get(selectionId) ?? 0) + 1;
    visualBindingRequest.current.set(selectionId, request);
    changeDraft({ ...draftRef.current, selections: draftRef.current.selections.map((item) => item.id === selectionId
      ? { ...item, visualNodeId: nodeId || undefined, visualMediaRevision: undefined }
      : item) });
    try {
      const knownRevision = visualNodes.find((node) => node.id === nodeId)?.mediaRevision;
      const attachment = nodeId && onAttachVisual ? await onAttachVisual(nodeId) : knownRevision;
      if (visualBindingRequest.current.get(selectionId) !== request) return;
      const latest = draftRef.current;
      const selectionStillUsesNode = latest.selections.some((item) => item.id === selectionId && item.visualNodeId === nodeId);
      if (!selectionStillUsesNode) return;
      const mediaRevision = typeof attachment === 'string' ? attachment : attachment?.mediaRevision;
      if (attachment && typeof attachment !== 'string') attachment.commit();
      changeDraft({ ...latest, selections: latest.selections.map((item) => item.id === selectionId && item.visualNodeId === nodeId
        ? { ...item, visualMediaRevision: nodeId ? mediaRevision : undefined }
        : item) });
    } catch (cause) {
      if (visualBindingRequest.current.get(selectionId) !== request) return;
      setError(cause instanceof Error ? cause.message : '无法引用当前画面素材');
    }
  };
  const createVisual = (index: number, mode: 'generate' | 'upload') => {
    const selection = draft.selections[index];
    const created = selection && onCreateVisual?.(mode, selection.id);
    if (!created) return;
    changeDraft({ ...draft, selections: draft.selections.map((item, position) => position === index
      ? { ...item, visualNodeId: created.id, visualMediaRevision: created.mediaRevision }
      : item) });
  };

  return <div role="dialog" aria-modal="true" aria-label="改编脚本与语义区间" className={`nodrag flex max-h-[88vh] w-[min(840px,94vw)] flex-col overflow-hidden rounded-[12px] text-text-dark shadow-2xl ${CANVAS_NODE_OPS_PANEL_CLASS}`} onClick={(event) => event.stopPropagation()}>
    <div className="flex items-center justify-between border-b border-white/10 px-4 py-3"><h2 className="text-sm font-semibold">改编脚本与语义区间</h2><button type="button" onClick={onClose} className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS}>关闭</button></div>
    <div className="ui-scrollbar space-y-3 overflow-auto p-4 text-xs">
      {blueprintSettings}
      {!blueprintSettings && blueprintRef && <p className="text-text-muted">已关联参考蓝图 · 版本 {blueprintRef.revision}</p>}
      {!blueprintSettings && blueprintRole && <p className="rounded-[12px] border border-white/10 bg-surface-field p-3 text-text-muted">{blueprintRole}</p>}
      {(blueprintRef || onEnsureBlueprint) && <>
        <section className="rounded-[12px] border border-white/10 bg-white/[0.03] p-3">
          <h3 className="font-medium text-text-dark">AI 生成改编台词</h3>
          <p className="mt-1 text-text-muted">输入新主题，AI 会按照上方规则生成台词，并替换下方当前内容。</p>
          <div className="mt-3 flex items-end gap-2"><label className="min-w-0 flex-1">新主题<input aria-label="新主题" value={newTopic} onChange={(event) => { const next = event.target.value; setNewTopic(next); onTopicChange?.(next); }} placeholder="例如：把咖啡改编为新品饮料" className={fieldClass} /></label><button type="button" disabled={generating || saving || !newTopic.trim()} onClick={() => void generate()} className={`${NODE_CONTEXT_CONTROL_TRIGGER_CLASS} mb-1 disabled:opacity-50`}>{generating ? '生成中…' : '生成改编台词'}</button></div>
        </section>
        <div className="flex items-center gap-3 text-[11px] text-text-muted" aria-hidden="true"><span className="h-px flex-1 bg-white/10" /><span>或者</span><span className="h-px flex-1 bg-white/10" /></div>
      </>}
      <section>
        <h3 className="font-medium text-text-dark">手动编辑台词</h3>
        <p className="mt-1 text-text-muted">也可以直接修改或粘贴完整台词，完成后保存脚本版本。</p>
        <label className="mt-2 block"><span className="sr-only">改编台词</span><textarea ref={dialogueRef} aria-label="改编台词" value={draft.dialogue} onChange={(event) => changeDraft(editRemixDialogue(draft, event.target.value))} className={`${fieldClass} min-h-28 text-sm`} /></label>
      </section>
      <button type="button" onClick={markSelected} className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS}>将选中台词添加为事件</button>
      <p className="text-text-muted">选中一段台词后添加为语义事件，用来指定这段台词需要搭配的补充画面（B-roll）或音效。</p>
      <div className="border-t border-white/10 pt-3">
        <h3 className="font-medium text-text-dark">台词事件（{draft.selections.length}）</h3>
        <p className="mt-1 text-text-muted">AI 已根据台词自动拆分，你也可以选中台词手动补充事件。</p>
      </div>
      {draft.selections.map((selection, index) => {
        const requirement = brollRequirements.find((item) => item.event_id === selection.id);
        return <div key={selection.id} className="grid grid-cols-[1fr_100px_100px_1fr_1fr_auto] items-end gap-2 rounded-[12px] border border-white/10 bg-white/[0.03] p-3">
        <label><span className="flex items-center justify-between gap-2">事件名称<span className="rounded-full border border-white/10 bg-white/[0.04] px-2 py-0.5 text-[10px] text-text-muted">{selection.source === 'manual' || (!selection.source && selection.id.startsWith('event-')) ? '手动添加' : 'AI 生成'}</span></span><input aria-label={`事件 ${index + 1} 名称`} value={selection.label} onChange={(event) => changeDraft({ ...draft, selections: draft.selections.map((item, position) => position === index ? { ...item, label: event.target.value } : item) })} className={fieldClass} /></label>
        <label>起始字<input aria-label={`${selection.label} 起始字`} type="number" value={selection.start} onChange={(event) => changeDraft({ ...draft, selections: draft.selections.map((item, position) => position === index ? { ...item, start: Number(event.target.value), invalidReason: undefined } : item) })} className={fieldClass} /></label>
        <label>结束字<input aria-label={`${selection.label} 结束字`} type="number" value={selection.end} onChange={(event) => changeDraft({ ...draft, selections: draft.selections.map((item, position) => position === index ? { ...item, end: Number(event.target.value), invalidReason: undefined } : item) })} className={fieldClass} /></label>
        <label>补充画面（B-roll）<select id={`remix-visual-${selection.id}`} aria-label={`${selection.label}画面`} value={selection.visualNodeId ?? ''} onChange={(event) => void bindVisual(index, event.target.value)} className={fieldClass}><option value="">仅字幕</option>{visualNodes.map((node) => <option key={node.id} value={node.id}>{node.label}{node.mediaRevision ? '' : ' · 待生成/上传'}</option>)}</select></label>
        <label>音效<select aria-label={`${selection.label}音效`} value={selection.sfxNodeId ?? ''} onChange={(event) => changeDraft({ ...draft, selections: draft.selections.map((item, position) => position === index ? { ...item, sfxNodeId: event.target.value || undefined } : item) })} className={fieldClass}><option value="">无音效</option>{sfxNodes.map((node) => <option key={node.id} value={node.id}>{node.label}</option>)}</select></label>
        <button type="button" onClick={() => changeDraft({ ...draft, selections: draft.selections.filter((_, position) => position !== index) })} className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS}>删除</button>
        <div className="col-span-6 flex items-center gap-2">
          <button type="button" aria-label={`生成 ${selection.label} 素材`} onClick={() => createVisual(index, 'generate')} className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS}>生成</button>
          <button type="button" aria-label={`上传 ${selection.label} 素材`} onClick={() => createVisual(index, 'upload')} className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS}>上传</button>
          <button type="button" aria-label={`替换 ${selection.label} 素材`} onClick={() => selection.visualNodeId ? void bindVisual(index, selection.visualNodeId) : document.getElementById(`remix-visual-${selection.id}`)?.focus()} className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS}>替换素材</button>
          {selection.visualNodeId && !selection.visualMediaRevision && <span className="text-amber-300">补充画面节点已创建，可以先保存脚本。稍后在画布完成素材后会自动关联。</span>}
        </div>
        {requirement?.required && <p className="col-span-6 text-amber-300">必需补充画面（B-roll）· {requirement.purpose} · {requirement.suggested_query}</p>}
        {selection.invalidReason && <p className="col-span-6 text-amber-300">台词已改变，请重新标记这个区间。</p>}
      </div>})}
      {error && <p role="alert" className="text-red-300">{error}</p>}
    </div>
    <div className="flex justify-end border-t border-white/10 px-4 py-3"><button type="button" disabled={saving || !project} onClick={() => void save()} className="nodrag h-9 rounded-[12px] bg-primary px-4 text-xs font-semibold text-primary-foreground disabled:opacity-50">{saving ? '保存中…' : '保存脚本版本'}</button></div>
  </div>;
}
