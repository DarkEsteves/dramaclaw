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
  segments?: RemixPerformanceSegment[];
}

export interface RemixPerformanceSegment {
  id: string;
  start: number;
  end: number;
  speaker: string;
  source: 'speaking_video' | 'voiceover' | 'original_sound';
  referenceBindings?: RemixReferenceBinding[];
  personNodeId?: string;
  productNodeId?: string;
  settingNodeId?: string;
  motionNodeId?: string;
  voiceNodeId?: string;
  takeNodeId?: string;
  takeMediaRevision?: string;
  primaryNodeId?: string;
  primaryMediaRevision?: string;
}

export interface RemixReferenceBinding {
  nodeId: string;
  purpose: string;
}

const LEGACY_REFERENCE_FIELDS = [
  ['personNodeId', '保持人物形象'],
  ['productNodeId', '保持物件外观'],
  ['settingNodeId', '保持场景环境'],
  ['motionNodeId', '参考镜头动作'],
  ['voiceNodeId', '参考说话音色'],
] as const;

export function referenceBindingsForSegment(segment: RemixPerformanceSegment): RemixReferenceBinding[] {
  if (segment.referenceBindings) return segment.referenceBindings;
  const seen = new Set<string>();
  return LEGACY_REFERENCE_FIELDS.flatMap(([key, purpose]) => {
    const nodeId = segment[key];
    if (!nodeId || seen.has(nodeId)) return [];
    seen.add(nodeId);
    return [{ nodeId, purpose }];
  });
}

const speakerLine = /^\s*([^：:\n]{1,24})[：:]\s*(\S.*)$/u;

export function seedPerformanceSegments(dialogue: string): RemixPerformanceSegment[] {
  if (!dialogue.trim()) return [];
  const lines = dialogue.split('\n');
  const nonblank = lines.map((line) => line.replace(/\r$/u, '')).filter((line) => line.trim());
  if (!nonblank.length || nonblank.some((line) => !speakerLine.test(line))) {
    return [{ id: 'segment-1', start: 0, end: Array.from(dialogue).length, speaker: '旁白', source: 'voiceover' }];
  }
  const segments: RemixPerformanceSegment[] = [];
  let offset = 0;
  for (const raw of lines) {
    const line = raw.replace(/\r$/u, '');
    if (line.trim()) {
      const match = speakerLine.exec(line)!;
      const spokenStart = line.indexOf(match[2], match.index + match[0].length - match[2].length);
      const start = offset + Array.from(line.slice(0, spokenStart)).length;
      segments.push({ id: `segment-${segments.length + 1}`, start, end: start + Array.from(match[2]).length, speaker: match[1].trim(), source: 'speaking_video' });
    }
    offset += Array.from(raw).length + 1;
  }
  return segments;
}

function spoken(dialogue: string, segment: RemixPerformanceSegment): string {
  return Array.from(dialogue).slice(segment.start, segment.end).join('');
}

function rebasePerformanceSegments(draft: RemixScriptDraft, dialogue: string): RemixPerformanceSegment[] | undefined {
  if (!draft.segments) return undefined;
  const seeded = seedPerformanceSegments(dialogue);
  const unused = new Set(draft.segments);
  return seeded.map((next, index) => {
    const exact = [...unused].find((old) => old.speaker === next.speaker && spoken(draft.dialogue, old) === spoken(dialogue, next));
    const prior = exact ?? (draft.segments?.[index]?.speaker === next.speaker ? draft.segments[index] : undefined);
    if (!prior || !unused.has(prior)) return next;
    unused.delete(prior);
    const sameText = spoken(draft.dialogue, prior) === spoken(dialogue, next);
    const { takeNodeId, takeMediaRevision, ...choices } = prior;
    return { ...next, ...choices, start: next.start, end: next.end, ...(sameText ? { takeNodeId, takeMediaRevision } : {}) };
  });
}

export function editRemixDialogue(draft: RemixScriptDraft, dialogue: string): RemixScriptDraft {
  return { dialogue, selections: rebaseNamedSelections(draft.dialogue, dialogue, draft.selections), ...(draft.segments ? { segments: rebasePerformanceSegments(draft, dialogue) } : {}) };
}

export function buildRemixScriptPayload(draft: RemixScriptDraft, revision: number): Record<string, unknown> {
  const length = Array.from(draft.dialogue).length;
  if (!draft.dialogue.trim()) throw new Error('改编台词不能为空');
  if (!Number.isInteger(revision) || revision < 1) throw new Error('脚本版本无效');
  const ids = new Set<string>();
  for (const selection of draft.selections) {
    if (!selection.id.trim() || ids.has(selection.id) || selection.invalidReason || !Number.isInteger(selection.start) || !Number.isInteger(selection.end) || selection.start < 0 || selection.end <= selection.start || selection.end > length) throw new Error('语义区间无效，请重新标记');
    if (selection.visualMediaRevision && !/^[a-f0-9]{64}$/u.test(selection.visualMediaRevision)) throw new Error('B-roll 素材版本无效，请重新选择素材');
    ids.add(selection.id);
  }
  const segmentIds = new Set<string>();
  let previousEnd = 0;
  for (const segment of draft.segments ?? []) {
    if (!segment.id.trim() || segmentIds.has(segment.id) || !segment.speaker.trim()
      || !Number.isInteger(segment.start) || !Number.isInteger(segment.end)
      || segment.start < previousEnd || segment.end <= segment.start || segment.end > length
      || !['speaking_video', 'voiceover', 'original_sound'].includes(segment.source)) throw new Error('表演段落范围或来源无效');
    if (segment.takeMediaRevision && (!segment.takeNodeId || !/^[a-f0-9]{64}$/u.test(segment.takeMediaRevision))) throw new Error('表演段落素材版本无效');
    if (segment.primaryMediaRevision && (!segment.primaryNodeId || !/^[a-f0-9]{64}$/u.test(segment.primaryMediaRevision))) throw new Error('主画面素材版本无效');
    if (segment.referenceBindings) {
      const bindingIds = new Set<string>();
      for (const binding of segment.referenceBindings) {
        if (!binding.nodeId.trim() || !binding.purpose.trim()) throw new Error('参考用途和素材不能为空');
        if (bindingIds.has(binding.nodeId)) throw new Error('参考素材重复');
        bindingIds.add(binding.nodeId);
      }
    }
    previousEnd = segment.end;
    segmentIds.add(segment.id);
  }
  return {
    dialogue: draft.dialogue, segments: (draft.segments ?? []).map((item) => ({
      segment_id: item.id, start_char: item.start, end_char: item.end,
      speaker: item.speaker, source: item.source,
      ...(item.referenceBindings ? { reference_bindings: item.referenceBindings.map(({ nodeId, purpose }) => ({ node_id: nodeId, purpose: purpose.trim() })) } : {}),
      ...(item.personNodeId ? { person_node_id: item.personNodeId } : {}),
      ...(item.productNodeId ? { product_node_id: item.productNodeId } : {}),
      ...(item.settingNodeId ? { setting_node_id: item.settingNodeId } : {}),
      ...(item.motionNodeId ? { motion_node_id: item.motionNodeId } : {}),
      ...(item.voiceNodeId ? { voice_node_id: item.voiceNodeId } : {}),
      ...(item.takeNodeId ? { take_node_id: item.takeNodeId } : {}),
      ...(item.takeMediaRevision ? { take_media_revision: item.takeMediaRevision } : {}),
      ...(item.primaryNodeId ? { primary_node_id: item.primaryNodeId } : {}),
      ...(item.primaryMediaRevision ? { primary_media_revision: item.primaryMediaRevision } : {}),
    })),
    selections: draft.selections.map((item) => ({ selection_id: item.id, start_char: item.start, end_char: item.end, script_revision: revision })),
    events: draft.selections.map((item) => ({ event_id: item.id, selection_id: item.id, required: true, caption: Array.from(draft.dialogue).slice(item.start, item.end).join(''), ...(item.visualNodeId ? { visual_node_id: item.visualNodeId } : {}), ...(item.visualMediaRevision ? { visual_media_revision: item.visualMediaRevision } : {}), ...(item.sfxNodeId ? { sfx_node_id: item.sfxNodeId } : {}) })),
    blueprint_mappings: [],
    media_requirements: draft.selections.filter((item) => item.visualNodeId).map((item) => ({ event_id: item.id, visual_node_id: item.visualNodeId, ...(item.visualMediaRevision ? { visual_media_revision: item.visualMediaRevision } : {}) })),
  };
}
