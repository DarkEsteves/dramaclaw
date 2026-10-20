import { describe, expect, it } from 'vitest';
import type { CanvasNode } from '@/features/canvas/domain/canvasNodes';
import { emptyProductionPlan, planMediaRef, type ProductionPlan } from './productionPlanModel';
import { distributePrimaryVisuals, inspectPlanSchema, inspectProductionPlan, planCueKey } from './productionPlanReadiness';

const image = { media_path: 'images/first.png', media_hash: 'a'.repeat(64), media_kind: 'image' as const,
  duration_us: 0, has_audio: false, url: '/first.png', size: 1 };
const candidate = { node: { id: 'image-1', type: 'imageGenNode', data: { imageUrl: '/first.png' } } as CanvasNode,
  kind: 'image', label: '主画面', currentPath: 'images/first.png', media: image } as const;

function plan(): ProductionPlan {
  const value = emptyProductionPlan();
  value.goal = '黑咖啡短片';
  value.beats[0].purpose = '提出问题';
  return value;
}

describe('production plan readiness', () => {
  it('locates each missing cue and counts only existing audio rows', () => {
    const draft = plan();
    draft.beats[0].audio.push({ id: 'voice', role: 'narration', description: '旁白', media: null });
    const result = inspectProductionPlan(draft, [], {}, [], 'plan-1', []);
    expect(result.visualTotal).toBe(1);
    expect(result.audioTotal).toBe(1);
    expect(result.visualReady).toBe(0);
    expect(result.audioReady).toBe(0);
    expect(result.issues.map((issue) => issue.kind)).toEqual(['visuals', 'audio']);
    expect(result.issues[0].key).toBe(planCueKey(draft.beats[0].id, 'visuals', draft.beats[0].visuals[0].id));
  });

  it('accepts a current visual and an empty optional audio list', () => {
    const draft = plan();
    draft.beats[0].visuals[0].media = planMediaRef('image-1', image);
    const result = inspectProductionPlan(draft, [candidate], {}, [candidate.node], 'plan-1', [{ id: 'edge-1', source: 'image-1', target: 'plan-1' }]);
    expect(result.issues).toEqual([]);
    expect(result.visualReady).toBe(1);
    expect(result.audioTotal).toBe(0);
  });

  it('accepts a graphic-only beat without requiring a media binding', () => {
    const draft = plan();
    draft.beats[0].visuals = [{ id: 'card', role: 'graphic', description: '问题', media: null,
      graphic: { template: 'question_card', text: '为什么？' } }];
    const result = inspectProductionPlan(draft, [], {}, [], 'plan-1', []);
    expect(result.issues).toEqual([]);
    expect(result.visualTotal).toBe(0);
  });

  it('does not count a bound material as ready after its canvas edge is removed', () => {
    const draft = plan();
    draft.beats[0].visuals[0].media = planMediaRef('image-1', image);
    const result = inspectProductionPlan(draft, [candidate], {}, [candidate.node], 'plan-1', []);
    expect(result.visualReady).toBe(0);
    expect(result.issues).toMatchObject([{ kind: 'visuals', status: 'stale' }]);
    expect(result.issues[0].reason).toContain('连线');
  });

  it('finds cue bounds beyond a shortened beat and a plan beyond 60 seconds', () => {
    const draft = plan();
    draft.beats[0].visuals.push({ id: 'overlay', role: 'overlay', description: '', media: null, start_frame: 70, end_frame: 90 });
    draft.beats[0].duration_frames = 75;
    expect(inspectPlanSchema(draft).map((issue) => issue.kind)).toContain('timing');
    draft.beats[0].duration_frames = 1300;
    draft.beats.push({ ...structuredClone(draft.beats[0]), id: 'beat-2', duration_frames: 1300, visuals: [] });
    expect(inspectPlanSchema(draft).map((issue) => issue.kind)).toContain('length');
  });

  it('reports stale media and a gap in primary coverage before composition', () => {
    const draft = plan();
    draft.beats[0].visuals[0].media = { ...planMediaRef('image-1', image), media_hash: 'b'.repeat(64) };
    draft.beats[0].visuals[0].start_frame = 10;
    draft.beats[0].visuals[0].end_frame = 90;
    const result = inspectProductionPlan(draft, [candidate], {}, [candidate.node], 'plan-1', [{ id: 'edge-1', source: 'image-1', target: 'plan-1' }]);
    expect(result.issues.map((issue) => issue.kind)).toEqual(['coverage', 'visuals']);
    expect(result.issues[1].reason).toContain('素材已变化');
  });

  it('tracks an unfinished canvas node against its cue', () => {
    const draft = plan();
    const key = planCueKey(draft.beats[0].id, 'visuals', draft.beats[0].visuals[0].id);
    const node = { id: 'new-image', type: 'imageGenNode', data: {} } as CanvasNode;
    const result = inspectProductionPlan(draft, [], { [key]: 'new-image' }, [node], 'plan-1', []);
    expect(result.issues[0]).toMatchObject({ kind: 'visuals', status: 'creating', nodeId: 'new-image' });
  });

  it('asks the author to complete or remove an empty dialogue row', () => {
    const draft = plan();
    draft.beats[0].visuals[0].media = planMediaRef('image-1', image);
    draft.beats[0].turns.push({ speaker: '采访者', text: '' });
    const result = inspectProductionPlan(draft, [candidate], {}, [candidate.node], 'plan-1', [{ id: 'edge-1', source: 'image-1', target: 'plan-1' }]);
    expect(result.issues).toMatchObject([{ kind: 'turn', beatIndex: 0, reason: '第 1 段有空台词，请填写或删除' }]);
  });

  it('evenly divides multiple primary visuals without changing overlay timing', () => {
    const draft = plan();
    const beat = draft.beats[0];
    beat.duration_frames = 100;
    beat.visuals.push({ id: 'second', role: 'primary', description: '第二镜头', media: null });
    beat.visuals.push({ id: 'third', role: 'primary', description: '第三镜头', media: null });
    beat.visuals.push({ id: 'overlay', role: 'overlay', description: '花字', media: null });
    expect(inspectProductionPlan(draft, [], {}, [], 'plan-1', []).issues.some((issue) => issue.kind === 'coverage')).toBe(true);
    distributePrimaryVisuals(beat);
    expect(beat.visuals.map((cue) => [cue.start_frame, cue.end_frame])).toEqual([
      [0, 33], [33, 66], [66, 100], [undefined, undefined],
    ]);
    expect(inspectProductionPlan(draft, [], {}, [], 'plan-1', []).issues.some((issue) => issue.kind === 'coverage')).toBe(false);
  });

  it('flags video speech that spans more than its matching primary shot', () => {
    const draft = plan();
    const clip = { media_path: 'videos/source.mp4', media_hash: 'b'.repeat(64), media_kind: 'video' as const,
      duration_us: 10_000_000, has_audio: true, url: '/source.mp4', size: 1 };
    const videoCandidate = { node: { id: 'video-1', type: 'videoNode', data: { videoUrl: '/source.mp4' } } as CanvasNode,
      kind: 'video', label: '视频1', currentPath: 'videos/source.mp4', media: clip } as const;
    const beat = draft.beats[0];
    beat.visuals[0].media = planMediaRef('video-1', clip);
    beat.visuals.push({ id: 'second', role: 'primary', description: '第二镜头', media: planMediaRef('video-1', clip) });
    distributePrimaryVisuals(beat);
    beat.audio.push({ id: 'speech', role: 'speech', description: '同期人声', media: planMediaRef('video-1', clip) });

    const result = inspectProductionPlan(draft, [videoCandidate], {}, [videoCandidate.node], 'plan-1',
      [{ id: 'edge-1', source: 'video-1', target: 'plan-1' }]);

    expect(result.issues).toMatchObject([{ kind: 'timing', beatIndex: 0 }]);
    expect(result.issues[0].reason).toContain('同期人声');
    beat.audio[0].role = 'narration';
    expect(inspectProductionPlan(draft, [videoCandidate], {}, [videoCandidate.node], 'plan-1',
      [{ id: 'edge-1', source: 'video-1', target: 'plan-1' }]).issues).toEqual([]);
  });
});
