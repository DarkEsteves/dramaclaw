import { CANVAS_NODE_TYPES, type CanvasEdge, type CanvasNode } from '../domain/canvasNodes';
import { planTimelineFromScript } from './spawnRemixDownstream';
import type { RemixScriptDraft } from './remixScriptModel';

export function scriptCanvasState(nodes: CanvasNode[], edges: CanvasEdge[], scriptId: string): {
  dialogue: string;
  eventCount: number;
  status: 'unsaved' | 'saved';
  nextAction: 'edit_script' | 'create_narration' | 'create_timeline';
} {
  const script = nodes.find((node) => node.id === scriptId && node.type === CANVAS_NODE_TYPES.script);
  const draft = script?.data.remixScriptDraft as RemixScriptDraft | undefined;
  const saved = Boolean(script?.data.remixScriptRef);
  let aligned = false;
  if (saved) {
    try { planTimelineFromScript(nodes, edges, scriptId); aligned = true; }
    catch { aligned = false; }
  }
  return {
    dialogue: typeof draft?.dialogue === 'string' ? draft.dialogue : typeof script?.data.prompt === 'string' ? script.data.prompt : '',
    eventCount: Array.isArray(draft?.selections) ? draft.selections.length : 0,
    status: saved ? 'saved' : 'unsaved',
    nextAction: !saved ? 'edit_script' : aligned ? 'create_timeline' : 'create_narration',
  };
}
