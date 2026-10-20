import { describe, expect, it } from 'vitest';
import { blueprintReadinessErrors, buildReferenceBlueprint, connectedBlueprintSources, referenceEventsFromBlueprint } from './blueprintModel';

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
  it('keeps analyzed row conversions usable without inventing a human review', () => {
    const payload = buildReferenceBlueprint({
      source: { mediaPath: 'reference.mp4', mediaHash: 'hash', durationUs: 2_000_000, videoUrl: '/video.mp4', transcriptRef: null },
      rows: [{ startTime: '0', endTime: '1', visualDescription: '杯子特写', narrative: '引出产品' }],
      narrativeRole: '产品演示', preserveRule: '', replaceRule: '', evidenceRef: { artifactId: 'evidence-1', revision: 1 },
    });
    expect(payload.events).toEqual([expect.objectContaining({ user_override: expect.objectContaining({ review_state: 'needs_review' }) })]);
    expect(blueprintReadinessErrors(payload)).toEqual([]);
  });

  it('links every event to an exact evidence frame and transcript range', () => {
    const payload = buildReferenceBlueprint({
      source: { mediaPath: 'reference.mp4', mediaHash: 'hash', durationUs: 3_000_000, videoUrl: '/video.mp4', transcriptRef: { artifactId: 'transcript-1', revision: 2 } },
      rows: [{ startTime: '0', endTime: '3', visualDescription: 'legacy row', narrative: 'legacy' }],
      referenceEvents: [{
        event_id: 'event-1', start_us: 0, end_us: 3_000_000,
        observed_frame: { pts: 90_000, time_base: '1/90000', time_us: 1_000_000, url: '/frame.png' },
        spoken_range: { text: 'try this', start_us: 500_000, end_us: 2_000_000 },
        visual_observation: '人物举杯', visible_text: 'OLD', narrative_inference: '模型判断产品登场',
        user_visual_observation: '人物举着空杯', user_visible_text: 'NEW',
        user_narrative_role: '证实产品价值', review_state: 'reviewed_uncertain',
      }],
      narrativeRole: '产品演示', preserveRule: '', replaceRule: '',
      evidenceRef: { artifactId: 'evidence-1', revision: 4 },
    });

    expect(payload.events).toEqual([{
      event_id: 'event-1', start_us: 0, end_us: 3_000_000,
      observed_frame: {
        evidence_ref: { artifact_id: 'evidence-1', revision: 4 },
        pts: 90_000, time_base: '1/90000', time_us: 1_000_000, url: '/frame.png',
      },
      spoken_range: {
        transcript_ref: { artifact_id: 'transcript-1', revision: 2 },
        text: 'try this', start_us: 500_000, end_us: 2_000_000,
      },
      model: { visual_observation: '人物举杯', visible_text: 'OLD', narrative_inference: '模型判断产品登场' },
      user_override: { visual_observation: '人物举着空杯', visible_text: 'NEW', narrative_role: '证实产品价值', review_state: 'reviewed_uncertain' },
    }]);
    expect(payload.observations).toEqual([expect.objectContaining({ description: '人物举着空杯', source: 'user_override', verified: true })]);
    expect(blueprintReadinessErrors(payload)).toEqual([]);
  });

  it('preserves analyzed speech as unresolved when no transcript revision is connected', () => {
    const payload = buildReferenceBlueprint({
      source: { mediaPath: 'reference.mp4', mediaHash: 'hash', durationUs: 2_000_000, videoUrl: '/video.mp4', transcriptRef: null },
      rows: [],
      referenceEvents: [{
        event_id: 'event-1', start_us: 0, end_us: 2_000_000,
        observed_frame: { pts: 0, time_base: '1/1000', time_us: 0 },
        spoken_range: { text: 'detected speech', start_us: 200_000, end_us: 1_500_000 },
        narrative_inference: 'hook', user_narrative_role: 'hook', review_state: 'reviewed',
      }],
      narrativeRole: 'hook', preserveRule: '', replaceRule: '',
      evidenceRef: { artifactId: 'evidence-1', revision: 1 },
    });

    expect(payload.events).toEqual([expect.objectContaining({
      spoken_range: expect.objectContaining({
        text: 'detected speech', start_us: 200_000, end_us: 1_500_000,
        unresolved_reason: 'missing_transcript_reference',
      }),
    })]);
    expect(blueprintReadinessErrors(payload)).toEqual(expect.arrayContaining([expect.stringMatching(/台词证据/)]));
  });

  it('blocks broken evidence structure while allowing reviewed uncertainty', () => {
    const payload = {
      narrative_role: '产品演示',
      events: [{
        event_id: 'event-1', start_us: 0, end_us: 1_000_000,
        observed_frame: { evidence_ref: { artifact_id: '', revision: 0 }, time_us: 500_000 },
        spoken_range: null,
        model: { visual_observation: '', narrative_inference: '可能是转折' },
        user_override: { narrative_role: '保留不确定转折', review_state: 'reviewed_uncertain' },
      }],
    };
    expect(blueprintReadinessErrors(payload)).toEqual(expect.arrayContaining([expect.stringMatching(/画面证据/)]));
    (payload.events[0].observed_frame.evidence_ref as { artifact_id: string; revision: number }) = { artifact_id: 'evidence-1', revision: 1 };
    expect(blueprintReadinessErrors(payload)).toEqual([]);
    payload.events[0].user_override.review_state = 'needs_review';
    expect(blueprintReadinessErrors(payload)).toEqual([]);
  });

  it('restores user overrides and evidence links from an exact blueprint revision', () => {
    const events = referenceEventsFromBlueprint({ events: [{
      event_id: 'event-7', start_us: 1_000_000, end_us: 2_000_000,
      observed_frame: { evidence_ref: { artifact_id: 'evidence-1', revision: 1 }, pts: 45, time_base: '1/45', time_us: 1_000_000, url: '/saved.png' },
      spoken_range: { transcript_ref: { artifact_id: 'words-1', revision: 3 }, text: 'saved words', start_us: 1_100_000, end_us: 1_900_000 },
      model: { visual_observation: '已保存画面', visible_text: '旧字幕', narrative_inference: '模型结论' },
      user_override: { visual_observation: '修正画面', visible_text: '修正字幕', narrative_role: '用户修改后角色', review_state: 'reviewed' },
    }] });
    expect(events).toEqual([expect.objectContaining({
      event_id: 'event-7', frame_url: '/saved.png', spoken_text: 'saved words',
      visual_observation: '已保存画面', user_visual_observation: '修正画面', visible_text: '旧字幕', user_visible_text: '修正字幕',
      user_narrative_role: '用户修改后角色', review_state: 'reviewed',
    })]);
  });
});
