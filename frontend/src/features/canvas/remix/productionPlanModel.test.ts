import { describe, expect, it } from 'vitest';
import { CANVAS_NODE_TYPES, type CanvasNode } from '@/features/canvas/domain/canvasNodes';
import { storyAnalysisContext } from './productionPlanModel';

describe('production plan reference context', () => {
  it('passes the analyzed narrative structure without event timing or dialogue', () => {
    const story = {
      type: CANVAS_NODE_TYPES.videoStory,
      data: {
        rawResult: { video_story: {
          summary: '从日常困境走向解决',
          adaptation_structure: {
            narrative_role: '先展示困境，再示范解决办法',
            reusable_beats: ['困境', '示范', '改变'],
          },
        } },
        referenceEvents: [{ start_us: 0, end_us: 2_000_000, spoken_text: '原片台词', visual_observation: '逐镜头画面' }],
      },
    } as unknown as CanvasNode;

    expect(storyAnalysisContext(story)).toEqual({
      summary: '从日常困境走向解决',
      adaptation_structure: {
        narrative_role: '先展示困境，再示范解决办法',
        reusable_beats: ['困境', '示范', '改变'],
      },
    });
  });
});
