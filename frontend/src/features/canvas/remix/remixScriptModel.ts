import { rebaseNamedSelections } from './model';

export interface RemixSelectionDraft {
  id: string;
  label: string;
  start: number;
  end: number;
  /** How this script event was created in the editor. Missing means AI-generated for legacy drafts. */
  source?: 'ai' | 'manual';
  visualNodeId?: string;
  /** Immutable content hash captured when this visual was selected. */
  visualMediaRevision?: string;
  sfxNodeId?: string;
  invalidReason?: string;
}

export interface RemixScriptDraft {
  dialogue: string;
  selections: RemixSelectionDraft[];
}

export function editRemixDialogue(draft: RemixScriptDraft, dialogue: string): RemixScriptDraft {
  return { dialogue, selections: rebaseNamedSelections(draft.dialogue, dialogue, draft.selections) };
}

export function buildRemixScriptPayload(draft: RemixScriptDraft, revision: number): Record<string, unknown> {
  const length = Array.from(draft.dialogue).length;
  if (!draft.dialogue.trim()) throw new Error('改编台词不能为空');
  if (!Number.isInteger(revision) || revision < 1) throw new Error('脚本版本无效');
  const ids = new Set<string>();
  for (const selection of draft.selections) {
    if (!selection.id.trim() || ids.has(selection.id) || selection.invalidReason || !Number.isInteger(selection.start) || !Number.isInteger(selection.end) || selection.start < 0 || selection.end <= selection.start || selection.end > length) throw new Error('语义区间无效，请重新标记');
    if (selection.visualNodeId && !/^[a-f0-9]{64}$/u.test(selection.visualMediaRevision ?? '')) throw new Error('B-roll 素材尚未生成或上传，请先完成素材绑定');
    ids.add(selection.id);
  }
  return {
    dialogue: draft.dialogue, segments: [],
    selections: draft.selections.map((item) => ({ selection_id: item.id, start_char: item.start, end_char: item.end, script_revision: revision })),
    events: draft.selections.map((item) => ({ event_id: item.id, selection_id: item.id, required: true, caption: Array.from(draft.dialogue).slice(item.start, item.end).join(''), ...(item.visualNodeId ? { visual_node_id: item.visualNodeId, visual_media_revision: item.visualMediaRevision } : {}), ...(item.sfxNodeId ? { sfx_node_id: item.sfxNodeId } : {}) })),
    blueprint_mappings: [],
    media_requirements: draft.selections.filter((item) => item.visualNodeId).map((item) => ({ event_id: item.id, visual_node_id: item.visualNodeId, visual_media_revision: item.visualMediaRevision })),
  };
}
