import { describe, expect, it } from 'vitest';
import { CANVAS_NODE_TYPES, type CanvasNode } from '@/features/canvas/domain/canvasNodes';
import { planMediaCandidates, storyAnalysisContext } from './productionPlanModel';

describe('production plan reference context', () => {
  it('does not reuse cached audio media after its output was invalidated', () => {
    const audio = { id: 'audio-1', type: CANVAS_NODE_TYPES.audio, data: {
      audioUrl: null, workflowResultStale: true,
      remixMedia: { media_path: 'audio/old.mp3', media_hash: 'a'.repeat(64), media_kind: 'audio', duration_us: 1000, has_audio: true },
    } } as unknown as CanvasNode;
    expect(planMediaCandidates('demo', [audio], 'plan-1')).toEqual([]);
  });

  it('does not reuse cached media when the current unparsed output URL changes', () => {
    const image = { id: 'image-1', type: CANVAS_NODE_TYPES.imageGen, data: {
      imageUrl: '/new-image.png',
      remixMedia: { media_path: 'images/old.png', media_hash: 'a'.repeat(64), media_kind: 'image',
        duration_us: 0, has_audio: false, url: '/old-image.png' },
    } } as unknown as CanvasNode;
    expect(planMediaCandidates('demo', [image], 'plan-1')[0]?.media).toBeNull();
  });
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
