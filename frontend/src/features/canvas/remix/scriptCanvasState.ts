import { CANVAS_NODE_TYPES, type CanvasEdge, type CanvasNode } from '../domain/canvasNodes';
import { planTimelineFromScript } from './spawnRemixDownstream';
import type { RemixScriptDraft } from './remixScriptModel';

type ArtifactRef = { artifactId?: string; revision?: number };
export function remixScriptEditorLabel(data: CanvasNode['data'], hasBlueprint: boolean): '编写改编脚本' | '编辑改编脚本' | null {
  const isRemix = hasBlueprint || Boolean(data.remixScriptRef || data.remixScriptLastSavedRef || data.remixScriptDraft);
  if (!isRemix) return null;
  const dialogue = (data.remixScriptDraft as RemixScriptDraft | undefined)?.dialogue;
  return (typeof dialogue === 'string' && dialogue.trim()) || data.remixScriptRef || data.remixScriptLastSavedRef
    ? '编辑改编脚本' : '编写改编脚本';
}

function sameRef(left: unknown, right: unknown): boolean {
  const a = left as ArtifactRef | undefined;
  const b = right as ArtifactRef | undefined;
  return Boolean(a?.artifactId && b?.artifactId && a.artifactId === b.artifactId && a.revision === b.revision);
}

export function scriptCanvasState(nodes: CanvasNode[], edges: CanvasEdge[], scriptId: string): {
  dialogue: string;
  eventCount: number;
  status: 'unsaved' | 'saved';
  nextAction: 'edit_script' | 'create_narration' | 'align_narration' | 'view_alignment' | 'create_timeline';
  narrationId?: string;
  transcriptId?: string;
} {
  const script = nodes.find((node) => node.id === scriptId && node.type === CANVAS_NODE_TYPES.script);
  const draft = script?.data.remixScriptDraft as RemixScriptDraft | undefined;
  const saved = Boolean(script?.data.remixScriptRef);
  let aligned = false;
  if (saved) {
    try { planTimelineFromScript(nodes, edges, scriptId); aligned = true; }
    catch { aligned = false; }
  }
  const scriptRef = script?.data.remixScriptRef;
  const narration = saved && !aligned ? nodes.find((node) => node.type === CANVAS_NODE_TYPES.audio
    && node.data.remixRole === 'narration'
    && typeof node.data.audioUrl === 'string' && Boolean(node.data.audioUrl)
    && edges.some((edge) => edge.source === scriptId && edge.target === node.id)
    && sameRef(node.data.remixScriptRef, scriptRef)) : undefined;
  const media = narration?.data.remixMedia as { url?: string; media_path?: string; media_hash?: string } | undefined;
  const pending = narration && media && (!media.url || media.url === narration.data.audioUrl) ? nodes.find((node) => node.type === CANVAS_NODE_TYPES.transcript
    && (Boolean(node.data.operationId) || node.data.autoStartMode === 'align')
    && sameRef(node.data.pendingScriptRef, scriptRef)
    && node.data.sourceMediaPath === media.media_path
    && node.data.sourceMediaHash === media.media_hash
    && edges.some((edge) => edge.source === scriptId && edge.target === node.id)
    && edges.some((edge) => edge.source === narration.id && edge.target === node.id)) : undefined;
  return {
    dialogue: typeof draft?.dialogue === 'string' ? draft.dialogue : typeof script?.data.prompt === 'string' ? script.data.prompt : '',
    eventCount: Array.isArray(draft?.selections) ? draft.selections.length : 0,
    status: saved ? 'saved' : 'unsaved',
    nextAction: !saved ? 'edit_script' : aligned ? 'create_timeline' : pending ? 'view_alignment' : narration ? 'align_narration' : 'create_narration',
    ...(narration ? { narrationId: narration.id } : {}),
    ...(pending ? { transcriptId: pending.id } : {}),
  };
}
