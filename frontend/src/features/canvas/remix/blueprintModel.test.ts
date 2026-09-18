import { describe, expect, it } from 'vitest';
import { buildReferenceBlueprint, connectedBlueprintSources } from './blueprintModel';

describe('reference blueprint model', () => {
  it('finds connected reference video and transcript artifact', () => {
    const nodes = [
      { id: 'video', data: { videoUrl: '/video.mp4', remixMedia: { media_path: 'reference.mp4', media_hash: 'hash', duration_us: 2_000_000, media_kind: 'video', url: '/video.mp4' } } },
      { id: 'words', data: { artifactRef: { artifactId: 'transcript-1', revision: 2 } } },
    ];
    const edges = [{ source: 'video', target: 'story' }, { source: 'words', target: 'story' }];
    expect(connectedBlueprintSources('story', nodes, edges)).toEqual({ mediaPath: 'reference.mp4', mediaHash: 'hash', durationUs: 2_000_000, videoUrl: '/video.mp4', transcriptRef: { artifactId: 'transcript-1', revision: 2 } });
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
});
