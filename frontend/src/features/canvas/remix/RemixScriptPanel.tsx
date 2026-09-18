import { useEffect, useRef, useState } from 'react';
import { fetchRemixArtifact, type RemixArtifactRef } from '@/api/remix';
import { saveRemixScriptArtifact } from './remixScriptArtifact';
import { editRemixDialogue, type RemixScriptDraft } from './remixScriptModel';

interface Props {
  project: string;
  initialDraft: RemixScriptDraft;
  existingRef: RemixArtifactRef | null;
  blueprintRef: RemixArtifactRef | null;
  visualNodes: Array<{ id: string; label: string }>;
  onDraftChange: (draft: RemixScriptDraft) => void;
  onSave: (ref: RemixArtifactRef) => void;
  onClose: () => void;
}

export function RemixScriptPanel({ project, initialDraft, existingRef, blueprintRef, visualNodes, onDraftChange, onSave, onClose }: Props) {
  const [draft, setDraft] = useState(initialDraft);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [blueprintRole, setBlueprintRole] = useState<string | null>(null);
  const dialogueRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (!blueprintRef || !project) return;
    let active = true;
    fetchRemixArtifact(project, blueprintRef).then((artifact) => {
      if (active && artifact.schema === 'reference_blueprint.v1' && typeof artifact.payload.narrative_role === 'string') setBlueprintRole(artifact.payload.narrative_role);
    }).catch(() => { if (active) setBlueprintRole(null); });
    return () => { active = false; };
  }, [blueprintRef?.artifactId, blueprintRef?.revision, project]);

  const changeDraft = (next: RemixScriptDraft) => { setDraft(next); onDraftChange(next); setError(null); };
  const markSelected = () => {
    const field = dialogueRef.current;
    if (!field || field.selectionStart === field.selectionEnd) { setError('先在台词里选中一段文字。'); return; }
    const start = Array.from(field.value.slice(0, field.selectionStart)).length;
    const end = Array.from(field.value.slice(0, field.selectionEnd)).length;
    const index = draft.selections.length + 1;
    changeDraft({ ...draft, selections: [...draft.selections, { id: `event-${crypto.randomUUID()}`, label: index === 1 ? '产品登场' : `语义事件 ${index}`, start, end }] });
  };
  const save = async () => {
    if (saving) return;
    setSaving(true);
    setError(null);
    try {
      const ref = await saveRemixScriptArtifact(project, draft, existingRef, blueprintRef);
      onSave(ref);
    } catch (cause) { setError(cause instanceof Error ? cause.message : '保存脚本失败'); }
    finally { setSaving(false); }
  };

  return <div className="nodrag flex max-h-[82vh] w-[min(720px,94vw)] flex-col overflow-hidden rounded-xl border border-white/15 bg-[#242426] text-text-dark shadow-2xl" onClick={(event) => event.stopPropagation()}>
    <div className="flex items-center justify-between border-b border-white/10 px-4 py-3"><h2 className="text-sm font-semibold">改编脚本与语义区间</h2><button type="button" onClick={onClose} className="rounded px-2 py-1 text-xs text-text-muted hover:bg-white/10">关闭</button></div>
    <div className="ui-scrollbar space-y-3 overflow-auto p-4 text-xs">
      {blueprintRef && <p className="text-text-muted">参考蓝图：{blueprintRef.artifactId} · r{blueprintRef.revision}</p>}
      {blueprintRole && <p className="rounded border border-white/10 bg-black/15 p-2 text-text-muted">{blueprintRole}</p>}
      <label className="block">改编台词<textarea ref={dialogueRef} aria-label="改编台词" value={draft.dialogue} onChange={(event) => changeDraft(editRemixDialogue(draft, event.target.value))} className="nodrag mt-1 min-h-28 w-full rounded border border-white/15 bg-black/25 p-2 text-sm outline-none" /></label>
      <button type="button" onClick={markSelected} className="rounded border border-white/15 px-2 py-1">标记选中文本</button>
      <p className="text-text-muted">在台词中选中文字后标记；填写替换画面节点可作为 B-roll 指令。</p>
      {draft.selections.map((selection, index) => <div key={selection.id} className="grid grid-cols-[1fr_100px_100px_1fr_auto] items-end gap-2 rounded border border-white/10 p-2">
        <label>事件名称<input aria-label={`事件 ${index + 1} 名称`} value={selection.label} onChange={(event) => changeDraft({ ...draft, selections: draft.selections.map((item, position) => position === index ? { ...item, label: event.target.value } : item) })} className="nodrag mt-1 w-full rounded border border-white/10 bg-black/20 p-1" /></label>
        <label>起始字<input aria-label={`${selection.label} 起始字`} type="number" value={selection.start} onChange={(event) => changeDraft({ ...draft, selections: draft.selections.map((item, position) => position === index ? { ...item, start: Number(event.target.value), invalidReason: undefined } : item) })} className="nodrag mt-1 w-full rounded border border-white/10 bg-black/20 p-1" /></label>
        <label>结束字<input aria-label={`${selection.label} 结束字`} type="number" value={selection.end} onChange={(event) => changeDraft({ ...draft, selections: draft.selections.map((item, position) => position === index ? { ...item, end: Number(event.target.value), invalidReason: undefined } : item) })} className="nodrag mt-1 w-full rounded border border-white/10 bg-black/20 p-1" /></label>
        <label>B-roll 画面<select aria-label={`${selection.label}画面`} value={selection.visualNodeId ?? ''} onChange={(event) => changeDraft({ ...draft, selections: draft.selections.map((item, position) => position === index ? { ...item, visualNodeId: event.target.value || undefined } : item) })} className="nodrag mt-1 w-full rounded border border-white/10 bg-[#242426] p-1"><option value="">仅字幕</option>{visualNodes.map((node) => <option key={node.id} value={node.id}>{node.label}</option>)}</select></label>
        <button type="button" onClick={() => changeDraft({ ...draft, selections: draft.selections.filter((_, position) => position !== index) })} className="rounded border border-white/15 px-2 py-1">删除</button>
        {selection.invalidReason && <p className="col-span-5 text-amber-300">台词已改变，请重新标记这个区间。</p>}
      </div>)}
      {error && <p role="alert" className="text-red-300">{error}</p>}
    </div>
    <div className="flex justify-end border-t border-white/10 px-4 py-3"><button type="button" disabled={saving || !project} onClick={() => void save()} className="rounded bg-teal-600 px-3 py-2 text-xs text-white disabled:opacity-50">{saving ? '保存中…' : '保存脚本版本'}</button></div>
  </div>;
}
