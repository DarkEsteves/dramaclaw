import { describe, expect, it } from 'vitest';
import { buildRemixScriptPayload, editRemixDialogue, type RemixScriptDraft } from './remixScriptModel';

const draft: RemixScriptDraft = { dialogue: '喝下咖啡', selections: [{ id: 'product', label: '产品登场', start: 2, end: 4, visualNodeId: 'video-1', visualMediaRevision: 'a'.repeat(64) }] };

describe('remix script editor', () => {
  it('serializes code-point selections and B-roll rules for exact artifact revision', () => {
    expect(buildRemixScriptPayload(draft, 2)).toEqual({
      dialogue: '喝下咖啡', segments: [],
      selections: [{ selection_id: 'product', start_char: 2, end_char: 4, script_revision: 2 }],
      events: [{ event_id: 'product', selection_id: 'product', required: true, caption: '咖啡', visual_node_id: 'video-1', visual_media_revision: 'a'.repeat(64) }],
      blueprint_mappings: [], media_requirements: [{ event_id: 'product', visual_node_id: 'video-1', visual_media_revision: 'a'.repeat(64) }],
    });
  });

  it('invalidates touched selections when dialogue changes', () => {
    expect(editRemixDialogue(draft, '喝下可啡').selections[0].invalidReason).toBeTruthy();
  });

  it('blocks saving invalid or out-of-range selections', () => {
    expect(() => buildRemixScriptPayload({ ...draft, selections: [{ ...draft.selections[0], end: 99 }] }, 1)).toThrow('语义区间');
  });

  it('blocks saving a B-roll selection until its exact media revision is attached', () => {
    expect(() => buildRemixScriptPayload({ ...draft, selections: [{ ...draft.selections[0], visualMediaRevision: undefined }] }, 1)).toThrow(/素材/);
  });

  it('serializes an SFX directive for a timed selection', () => {
    const payload = buildRemixScriptPayload({ ...draft, selections: [{ ...draft.selections[0], sfxNodeId: 'audio-1' }] }, 1);
    expect(payload.events).toEqual([expect.objectContaining({ sfx_node_id: 'audio-1' })]);
  });
});
