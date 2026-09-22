import { describe, expect, it } from 'vitest';
import { storyScriptToolbarState } from './storyScriptToolbarState';

const story = (data: Record<string, unknown>) => ({ id: 'story', type: 'videoStoryNode', position: { x: 0, y: 0 }, data });
describe('storyScriptToolbarState', () => {
  it('blocks script generation until every reference event is reviewed', () => {
    expect(storyScriptToolbarState([story({ referenceEvents: [
      { review_state: 'reviewed' }, { review_state: 'needs_review' }, {},
    ] })] as never, [], 'story')).toEqual({
      label: '生成改编脚本', disabled: true, title: '还有 2 项待核对',
    });
  });

  it('keeps generation available after review even when an older script exists', () => {
    expect(storyScriptToolbarState([story({ referenceEvents: [{ review_state: 'reviewed' }] })] as never, [], 'story')).toEqual({
      label: '生成改编脚本', disabled: false, title: undefined,
    });
    expect(storyScriptToolbarState([story({ referenceEvents: [{ review_state: 'reviewed' }] }), { id: 'script', type: 'scriptNode', data: {} }] as never, [{ source: 'story', target: 'script' }] as never, 'story')).toEqual({
      label: '生成改编脚本', disabled: false, title: undefined,
    });
  });

  it('requires review before generating another script', () => {
    expect(storyScriptToolbarState([story({ blueprintDirty: true, referenceEvents: [{ review_state: 'needs_review' }] }), { id: 'script', type: 'scriptNode', data: {} }] as never, [{ source: 'story', target: 'script' }] as never, 'story')).toEqual({
      label: '生成改编脚本', disabled: true, title: '还有 1 项待核对',
    });
  });
});
