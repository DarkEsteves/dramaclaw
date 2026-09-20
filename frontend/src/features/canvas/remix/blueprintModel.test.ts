import { describe, expect, it } from 'vitest';
import { buildReferenceBlueprint, connectedBlueprintSources } from './blueprintModel';

describe('reference blueprint model', () => {
  it('finds connected reference video and transcript artifact', () => {
    const nodes = [
      { id: 'video', data: { videoUrl: '/video.mp4', remixMedia: { media_path: 'reference.mp4', media_hash: 'hash', duration_us: 2_000_000, media_kind: 'video', url: '/video.mp4' } } },
      { id: 'words', data: { sourceMediaHash: 'hash', artifactRef: { artifactId: 'transcript-1', revision: 2 } } },
    ];
    const edges = [{ source: 'video', target: 'story' }, { source: 'words', target: 'story' }];
    expect(connectedBlueprintSources('story', nodes, edges)).toEqual({ mediaPath: 'reference.mp4', mediaHash: 'hash', durationUs: 2_000_000, videoUrl: '/video.mp4', transcriptRef: { artifactId: 'transcript-1', revision: 2 } });
  });

  it('ignores a connected transcript from a replaced video', () => {
    const nodes = [
      { id: 'video', data: { videoUrl: '/new.mp4', remixMedia: { media_path: 'new.mp4', media_hash: 'new', duration_us: 1_000_000, media_kind: 'video' } } },
      { id: 'words', data: { sourceMediaHash: 'old', artifactRef: { artifactId: 'old-words', revision: 1 } } },
    ];
    expect(connectedBlueprintSources('story', nodes, [{ source: 'video', target: 'story' }, { source: 'words', target: 'story' }])?.transcriptRef).toBeNull();
  });

  it('keeps observations, model interpretations, and user rules separate', () => {
    const payload = buildReferenceBlueprint({
      source: { mediaPath: 'reference.mp4', mediaHash: 'hash', durationUs: 2_000_000, videoUrl: '/video.mp4', transcriptRef: { artifactId: 'transcript-1', revision: 2 } },
      rows: [{ startTime: '0', endTime: '1', visualDescription: '杯子特写', narrative: '引出产品' }],
      narrativeRole: '先提出困扰，再展示产品', preserveRule: '保留节奏', replaceRule: '替换品牌画面',
    });
    expect(payload.narrative_role).toBe('先提出困扰，再展示产品');
    expect(payload.observations).toEqual([expect.objectContaining({ description: '杯子特写', source: 'video_story_analysis' })]);
    expect(payload.inferences).toEqual([expect.objectContaining({ interpretation: '引出产品' })]);
    expect(payload.user_choices).toEqual(expect.arrayContaining([expect.objectContaining({ type: 'preserve', text: '保留节奏' }), expect.objectContaining({ type: 'replace', text: '替换品牌画面' })]));
    expect(payload.transcript_ref).toEqual({ artifact_id: 'transcript-1', revision: 2 });
  });

  it('uses analyzed reference events when the detailed story rows are empty', () => {
    const payload = buildReferenceBlueprint({
      source: { mediaPath: 'reference.mp4', mediaHash: 'hash', durationUs: 2_000_000, videoUrl: '/video.mp4', transcriptRef: null },
      rows: [],
      referenceEvents: [{ start_us: 500_000, end_us: 1_200_000, visual_observation: '人物举杯', spoken_text: '试试这个', narrative_inference: '产品登场', note_status: 'needs_review' }],
      narrativeRole: '访谈展示产品', preserveRule: '', replaceRule: '',
    });
    expect(payload.events).toEqual([expect.objectContaining({ start_time: '0.500', end_time: '1.200', visual_description: '人物举杯', spoken_text: '试试这个', narrative: '产品登场' })]);
    expect(payload.observations).toEqual([expect.objectContaining({ description: '人物举杯', source: 'reference_event', verified: false })]);
    expect(payload.inferences).toEqual([expect.objectContaining({ interpretation: '产品登场', source: 'reference_event' })]);
  });
});
