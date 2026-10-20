import { describe, expect, it } from 'vitest';
import type { CanvasEdge, CanvasNode } from '../domain/canvasNodes';
import { remixScriptEditorLabel, scriptCanvasState } from './scriptCanvasState';

const node = (id: string, type: string, data: Record<string, unknown> = {}) => ({ id, type, data, position: { x: 0, y: 0 } }) as CanvasNode;
const edge = (source: string, target: string) => ({ id: `${source}-${target}`, source, target }) as CanvasEdge;
const saved = { artifactId: 'script', revision: 2 };

describe('script canvas state', () => {
  it('shows the remix editor only for a linked or existing remix script', () => {
    expect(remixScriptEditorLabel({}, false)).toBeNull();
    expect(remixScriptEditorLabel({ prompt: '普通分镜提示词' }, false)).toBeNull();
    expect(remixScriptEditorLabel({}, true)).toBe('编辑作品脚本');
    expect(remixScriptEditorLabel({ remixScriptDraft: { dialogue: '新台词', selections: [] } }, false)).toBe('编辑作品脚本');
    expect(remixScriptEditorLabel({ remixScriptRef: saved }, false)).toBe('编辑作品脚本');
    expect(remixScriptEditorLabel({ remixRole: 'remix_script', sourceVideoStoryId: 'story-1' }, false)).toBe('编辑作品脚本');
  });

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

  it('offers alignment when the current script has a connected, ready narration', () => {
    const script = node('s', 'scriptNode', { remixScriptRef: saved, remixScriptDraft: { dialogue: '新台词', selections: [] } });
    const narration = node('a', 'audioNode', { remixRole: 'narration', remixScriptRef: saved, audioUrl: '/voice.wav' });
    expect(scriptCanvasState([script, narration], [edge('s', 'a')], 's')).toMatchObject({ nextAction: 'align_narration', narrationId: 'a' });
    expect(scriptCanvasState([script, { ...narration, data: { ...narration.data, audioUrl: null } }], [edge('s', 'a')], 's').nextAction).toBe('create_narration');
    expect(scriptCanvasState([script, { ...narration, data: { ...narration.data, remixScriptRef: { ...saved, revision: 1 } } }], [edge('s', 'a')], 's').nextAction).toBe('create_narration');
  });

  it('points to the existing transcript while the exact narration is aligning', () => {
    const script = node('s', 'scriptNode', { remixScriptRef: saved });
    const narration = node('a', 'audioNode', { remixRole: 'narration', remixScriptRef: saved, audioUrl: '/voice.wav', remixMedia: { url: '/voice.wav', media_path: 'voice.wav', media_hash: 'voice' } });
    const words = node('w', 'transcriptNode', { operationId: 'running', pendingScriptRef: saved, sourceMediaPath: 'voice.wav', sourceMediaHash: 'voice', status: 'needs_review' });
    expect(scriptCanvasState([script, narration, words], [edge('s', 'a'), edge('s', 'w'), edge('a', 'w')], 's')).toMatchObject({ nextAction: 'view_alignment', transcriptId: 'w' });
    const replaced = { ...narration, data: { ...narration.data, audioUrl: '/new.wav' } };
    expect(scriptCanvasState([script, replaced, words], [edge('s', 'a'), edge('s', 'w'), edge('a', 'w')], 's').nextAction).toBe('align_narration');
  });
});
