import { describe, expect, it } from 'vitest';
import { buildRemixScriptPayload, editRemixDialogue, referenceBindingsForSegment, seedPerformanceSegments, type RemixScriptDraft } from './remixScriptModel';

const draft: RemixScriptDraft = { dialogue: '喝下咖啡', selections: [{ id: 'product', label: '产品登场', start: 2, end: 4, visualNodeId: 'video-1', visualMediaRevision: 'a'.repeat(64) }] };

describe('remix script editor', () => {
  it('keeps arbitrary ordered media purposes without requiring product or scene slots', () => {
    const segment = { id: 'one', start: 0, end: 2, speaker: '甲', source: 'speaking_video' as const,
      referenceBindings: [{ nodeId: 'shot', purpose: '保持手持镜头运动' }, { nodeId: 'voice', purpose: '参考说话音色' }] };
    expect(referenceBindingsForSegment(segment)).toEqual(segment.referenceBindings);
    expect(buildRemixScriptPayload({ dialogue: '你好', selections: [], segments: [segment] }, 1).segments).toEqual([
      expect.objectContaining({ reference_bindings: [
        { node_id: 'shot', purpose: '保持手持镜头运动' }, { node_id: 'voice', purpose: '参考说话音色' },
      ] }),
    ]);
    expect(() => buildRemixScriptPayload({ dialogue: '你好', selections: [], segments: [{ ...segment,
      referenceBindings: [{ nodeId: 'shot', purpose: '   ' }] }] }, 1)).toThrow('参考用途');
    expect(() => buildRemixScriptPayload({ dialogue: '你好', selections: [], segments: [{ ...segment,
      referenceBindings: [{ nodeId: 'shot', purpose: '镜头' }, { nodeId: 'shot', purpose: '另一用途' }] }] }, 1)).toThrow('重复');
  });

  it('adapts legacy fields unless an explicit empty reference list clears them', () => {
    const legacy = { id: 'one', start: 0, end: 2, speaker: '甲', source: 'speaking_video' as const,
      personNodeId: 'person', productNodeId: 'prop', settingNodeId: 'room' };
    expect(referenceBindingsForSegment(legacy)).toEqual([
      { nodeId: 'person', purpose: '保持人物形象' }, { nodeId: 'prop', purpose: '保持物件外观' },
      { nodeId: 'room', purpose: '保持场景环境' },
    ]);
    expect(referenceBindingsForSegment({ ...legacy, referenceBindings: [] })).toEqual([]);
  });
  it('seeds speaking takes from labelled dialogue independently of B-roll events', () => {
    const dialogue = '男：你喝什么？\n女：新咖啡。';
    expect(seedPerformanceSegments(dialogue)).toEqual([
      { id: 'segment-1', start: 2, end: 7, speaker: '男', source: 'speaking_video' },
      { id: 'segment-2', start: 10, end: 14, speaker: '女', source: 'speaking_video' },
    ]);
  });

  it('keeps a take choice on an unchanged line and clears selected media after text changes', () => {
    const dialogue = '男：你喝什么？\n女：新咖啡。';
    const planned: RemixScriptDraft = { dialogue, selections: [], segments: seedPerformanceSegments(dialogue).map((item, index) => ({
      ...item, personNodeId: `person-${index}`, takeNodeId: `take-${index}`, takeMediaRevision: 'a'.repeat(64),
    })) };
    const revised = editRemixDialogue(planned, '男：你喝什么？\n女：新饮料。');
    expect(revised.segments?.[0]).toEqual(planned.segments?.[0]);
    expect(revised.segments?.[1]).toEqual(expect.objectContaining({ id: 'segment-2', personNodeId: 'person-1', source: 'speaking_video' }));
    expect(revised.segments?.[1].takeNodeId).toBeUndefined();
    expect(revised.segments?.[1].takeMediaRevision).toBeUndefined();
  });

  it('serializes accepted take choices and rejects mismatched media references', () => {
    const withTake: RemixScriptDraft = { dialogue: '男：你好。', selections: [], segments: [{
      id: 'segment-1', start: 2, end: 5, speaker: '男', source: 'speaking_video',
      takeNodeId: 'video-1', takeMediaRevision: 'a'.repeat(64), productNodeId: 'product-1',
    }] };
    expect(buildRemixScriptPayload(withTake, 3).segments).toEqual([{
      segment_id: 'segment-1', start_char: 2, end_char: 5, speaker: '男', source: 'speaking_video',
      take_node_id: 'video-1', take_media_revision: 'a'.repeat(64), product_node_id: 'product-1',
    }]);
    expect(() => buildRemixScriptPayload({ ...withTake, segments: [{ ...withTake.segments![0], takeMediaRevision: 'bad' }] }, 3)).toThrow('素材版本');
  });
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

  it('saves a pending B-roll node before its media revision is available', () => {
    const payload = buildRemixScriptPayload({ ...draft, selections: [{ ...draft.selections[0], visualMediaRevision: undefined }] }, 1);
    expect(payload.events).toEqual([expect.objectContaining({ visual_node_id: 'video-1' })]);
    expect(payload.events).not.toEqual([expect.objectContaining({ visual_media_revision: expect.anything() })]);
    expect(payload.media_requirements).toEqual([{ event_id: 'product', visual_node_id: 'video-1' }]);
  });

  it('serializes an SFX directive for a timed selection', () => {
    const payload = buildRemixScriptPayload({ ...draft, selections: [{ ...draft.selections[0], sfxNodeId: 'audio-1' }] }, 1);
    expect(payload.events).toEqual([expect.objectContaining({ sfx_node_id: 'audio-1' })]);
  });
});
