import { describe, expect, it } from 'vitest';
import { applyManifestEdit, canExportManifest, manifestToSrt, previewStatus, projectManifestTimeline, reapplyManifestOverrides } from './manifestEditor';

const manifest = () => ({
  output_spec: { width: 1080, height: 1920, fps: 30, duration_frames: 90 },
  media_bindings: [],
  layers: [{ media_id: 'background', start_frame: 0, end_frame: 90, z_order: 0 }],
  subtitle_events: [{ text: '咖啡登场', start_frame: 30, end_frame: 60 }],
  audio_events: [
    { media_id: 'voice', role: 'narration', start_frame: 0, end_frame: 90, volume: 1 },
    { media_id: 'music', role: 'bgm', start_frame: 0, end_frame: 90, volume: 0.3 },
  ],
  manual_overrides: [], source_versions: [],
});

describe('remix manifest editor', () => {
  it('marks an exact preview stale after an edit and blocks unresolved required events', () => {
    expect(previewStatus({ artifactId: 'm', revision: 3 }, { artifactId: 'm', revision: 3 }, 'same', 'same')).toBe('current');
    expect(previewStatus({ artifactId: 'm', revision: 3 }, { artifactId: 'm', revision: 3 }, 'changed', 'same')).toBe('stale');
    expect(canExportManifest({ unresolvedRequiredEvents: ['product reveal'], conflicts: [] })).toBe(false);
    expect(canExportManifest({ unresolvedRequiredEvents: [], conflicts: [] })).toBe(true);
  });

  it('projects picture, subtitle, and audio role/volume from one manifest', () => {
    expect(projectManifestTimeline(manifest())).toMatchObject({
      durationFrames: 90,
      layers: [{ mediaId: 'background', startFrame: 0, endFrame: 90 }],
      subtitles: [{ text: '咖啡登场', startFrame: 30, endFrame: 60 }],
      audio: [{ mediaId: 'voice', role: 'narration', volume: 1 }, { mediaId: 'music', role: 'bgm', volume: 0.3 }],
    });
  });

  it('stores edits as manifest overrides and reapplies them after compile', () => {
    const edited = applyManifestEdit(manifest(), { collection: 'audio_events', index: 1, field: 'volume', value: 0.18 });
    expect(edited.audio_events[1].volume).toBe(0.18);
    expect(edited.manual_overrides).toEqual([expect.objectContaining({ target: 'audio_events', identity: { media_id: 'music', role: 'bgm' }, field: 'volume', base_value: 0.3, value: 0.18 })]);
    const compiled = manifest();
    compiled.audio_events[1].volume = 0.3;
    expect(reapplyManifestOverrides(compiled, edited.manual_overrides)).toMatchObject({ payload: { audio_events: [{ volume: 1 }, { volume: 0.18 }] }, conflicts: [] });
  });

  it('reports a conflict instead of silently losing an incompatible manual edit', () => {
    const edited = applyManifestEdit(manifest(), { collection: 'subtitle_events', index: 0, field: 'text', value: '新品登场' });
    const compiled = manifest();
    compiled.subtitle_events[0].text = '模型重写';
    const result = reapplyManifestOverrides(compiled, edited.manual_overrides);
    expect(result.conflicts).toEqual([expect.stringContaining('subtitle_events')]);
    expect(result.payload.subtitle_events[0].text).toBe('模型重写');
  });

  it('refuses to replay an ambiguous legacy override onto repeated rows', () => {
    const compiled = manifest();
    compiled.layers = [
      { media_id: 'product', start_frame: 0, end_frame: 20, z_order: 1 },
      { media_id: 'product', start_frame: 30, end_frame: 50, z_order: 1 },
    ];
    const result = reapplyManifestOverrides(compiled, [{
      target: 'layers', identity: { media_id: 'product', z_order: 1 }, field: 'end_frame', base_value: 20, value: 25,
    }]);
    expect(result.conflicts).toEqual([expect.stringContaining('ambiguous')]);
    expect(result.payload.layers.map((row) => row.end_frame)).toEqual([20, 50]);
  });

  it('exports SRT from the exact current manifest frames', () => {
    expect(manifestToSrt(manifest())).toContain('00:00:01,000 --> 00:00:02,000\n咖啡登场');
  });
});
