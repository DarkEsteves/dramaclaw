import { describe, expect, it } from 'vitest';
import { buildTimelineEditMap, buildTimelineOverrides, readTimingEvents, operationMatchesTimelineInputs } from './semanticTimelineModel';

describe('semantic timeline inputs', () => {
  it('covers the complete narration with a source-to-output edit segment', () => {
    expect(buildTimelineEditMap([{ start_us: 100_000, end_us: 1_250_000 }])).toEqual([
      { source_start_us: 0, source_end_us: 1_250_000, timeline_start_us: 0 },
    ]);
  });

  it('turns per-event offset and fixed duration hints into compiler overrides', () => {
    expect(buildTimelineOverrides({ intro: { offsetMs: 250, minimumDurationFrames: 12, startFrame: 5, endFrame: 24 } })).toEqual([
      { event_id: 'intro', relative_offset_us: 250_000 },
      { event_id: 'intro', kind: 'minimum_duration_frames', frames: 12 },
      { event_id: 'intro', kind: 'fixed_frames', start_frame: 5, end_frame: 24 },
    ]);
  });

  it('preserves an explicit crop and speed edit map for recompilation', () => {
    const manual = [{ source_start_us: 250_000, source_end_us: 1_250_000, timeline_start_us: 0, speed_num: 2, speed_den: 1 }];
    expect(buildTimelineEditMap([{ start_us: 0, end_us: 2_000_000 }], manual)).toEqual(manual);
  });

  it.each([
    [{ intro: { startFrame: 5 } }, '同时填写'],
    [{ intro: { endFrame: 24 } }, '同时填写'],
    [{ intro: { startFrame: 24, endFrame: 5 } }, '晚于'],
  ])('rejects incomplete or reversed fixed-frame overrides', (values, message) => {
    expect(() => buildTimelineOverrides(values)).toThrow(message);
  });

  it('shows unresolved and conflicting events alongside compiled events', () => {
    expect(readTimingEvents({ events: [{ event_id: 'intro', start_frame: 5, end_frame: 24 }], unresolved_required_events: [{ event_id: 'outro', reason: 'selection has no aligned words' }], override_conflicts: [{ event_id: 'intro', reason: 'fixed frame override differs from compiled timing' }] })).toEqual([
      { selectionId: 'intro', label: 'intro', startFrame: 5, endFrame: 24, invalidReason: 'fixed frame override differs from compiled timing' },
      { selectionId: 'outro', label: 'outro', invalidReason: 'selection has no aligned words' },
    ]);
  });

  it('rejects an operation from a different exact script or alignment revision', () => {
    const operation = { node_revision: 3, input_refs: [{ artifact_id: 'script', revision: 2 }, { artifact_id: 'words', revision: 1 }] };
    expect(operationMatchesTimelineInputs(operation, { artifactId: 'script', revision: 2 }, { artifactId: 'words', revision: 1 }, 3)).toBe(true);
    expect(operationMatchesTimelineInputs(operation, { artifactId: 'script', revision: 3 }, { artifactId: 'words', revision: 1 }, 3)).toBe(false);
    expect(operationMatchesTimelineInputs(operation, { artifactId: 'script', revision: 2 }, { artifactId: 'words', revision: 1 }, 4)).toBe(false);
  });
});
