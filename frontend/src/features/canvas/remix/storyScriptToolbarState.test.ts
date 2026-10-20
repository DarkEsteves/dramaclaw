import { describe, expect, it } from 'vitest';
import { storyScriptToolbarState } from './storyScriptToolbarState';

const story = (data: Record<string, unknown>) => ({ id: 'story', type: 'videoStoryNode', position: { x: 0, y: 0 }, data: { sourceVideoUrl: '/reference.mp4', ...data } });
describe('storyScriptToolbarState', () => {
  it('allows nine analyzed events to create a plan without a review acknowledgement', () => {
    expect(storyScriptToolbarState([story({ referenceEvents: [
      ...Array.from({ length: 9 }, (_, index) => ({ event_id: `event-${index}`, review_state: 'needs_review' })),
    ] })] as never, [], 'story')).toEqual({
      label: '新建作品方案', disabled: false, title: undefined,
    });
  });

  it('offers a new plan after review even when a plan is already connected', () => {
    expect(storyScriptToolbarState([story({ referenceEvents: [{ review_state: 'reviewed' }] })] as never, [], 'story')).toEqual({
      label: '新建作品方案', disabled: false, title: undefined,
    });
    expect(storyScriptToolbarState([story({ referenceEvents: [{ review_state: 'reviewed' }] }), { id: 'script', type: 'scriptNode', data: {} }] as never, [{ source: 'story', target: 'script' }] as never, 'story')).toEqual({
      label: '新建作品方案', disabled: false, title: undefined,
    });
  });

  it('allows a new plan after analysis content was edited without changing historical review fields', () => {
    expect(storyScriptToolbarState([story({ blueprintDirty: true, referenceEvents: [{ review_state: 'needs_review' }] }), { id: 'script', type: 'scriptNode', data: {} }] as never, [{ source: 'story', target: 'script' }] as never, 'story')).toEqual({
      label: '新建作品方案', disabled: false, title: undefined,
    });
  });
  it('still blocks incomplete analysis, failures, empty results and missing source media', () => {
    const events = [{ event_id: 'event-1', review_state: 'reviewed' }];
    for (const data of [{ isAnalyzing: true, referenceEvents: events }, { remixAnalysisStage: 'extracting', referenceEvents: events }, { analysisError: '分析失败', referenceEvents: events }, { referenceEvents: [] }, { sourceVideoUrl: null, referenceEvents: events }]) {
      expect(storyScriptToolbarState([story(data)] as never, [], 'story').disabled).toBe(true);
    }
  });
});
