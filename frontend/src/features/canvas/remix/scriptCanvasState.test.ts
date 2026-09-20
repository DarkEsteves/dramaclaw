import { describe, expect, it } from 'vitest';
import type { CanvasEdge, CanvasNode } from '../domain/canvasNodes';
import { scriptCanvasState } from './scriptCanvasState';

const node = (id: string, type: string, data: Record<string, unknown> = {}) => ({ id, type, data, position: { x: 0, y: 0 } }) as CanvasNode;
const edge = (source: string, target: string) => ({ id: `${source}-${target}`, source, target }) as CanvasEdge;
const saved = { artifactId: 'script', revision: 2 };

describe('script canvas state', () => {
  it('shows the draft and asks to save when there is no saved script', () => {
    expect(scriptCanvasState([node('s', 'scriptNode', { remixScriptDraft: { dialogue: '新台词', selections: [] } })], [], 's')).toEqual({
      dialogue: '新台词', eventCount: 0, status: 'unsaved', nextAction: 'edit_script',
    });
  });

  it('offers narration after saving, then a timeline only after exact alignment', () => {
    const script = node('s', 'scriptNode', { remixScriptRef: saved, remixScriptDraft: { dialogue: '新台词', selections: [{ id: 'a', label: '产品', start: 0, end: 2 }] } });
    expect(scriptCanvasState([script], [], 's').nextAction).toBe('create_narration');
    const words = node('w', 'transcriptNode', { artifactRef: { artifactId: 'words', revision: 1 }, alignedScriptRef: saved, status: 'completed' });
    expect(scriptCanvasState([script, words], [edge('s', 'w')], 's').nextAction).toBe('create_timeline');
    expect(scriptCanvasState([script, { ...words, data: { ...words.data, alignedScriptRef: { ...saved, revision: 1 } } }], [edge('s', 'w')], 's').nextAction).toBe('create_narration');
  });
});
