import { describe, expect, it } from 'vitest';
import { buildNodeRemixManifest } from './nodeRemixManifest';

const video = { media_path: 'input/reference.mp4', media_hash: 'a'.repeat(64), url: '/reference.mp4', size: 10, media_kind: 'video' as const, duration_us: 4_000_000, has_audio: true };
const audio = { media_path: 'input/voice.wav', media_hash: 'b'.repeat(64), url: '/voice.wav', size: 10, media_kind: 'audio' as const, duration_us: 4_000_000 };
const image = { media_path: 'freezone/remix_media/product.png', media_hash: 'c'.repeat(64), url: '/product.png', size: 10, media_kind: 'image' as const, duration_us: 0 };
const visualVideo = { ...video, media_path: 'freezone/remix_media/product.mp4', media_hash: 'd'.repeat(64), has_audio: false };
const background = { ...image, media_path: 'freezone/remix_media/background.png', media_hash: 'f'.repeat(64) };

describe('buildNodeRemixManifest', () => {
  it('builds continuous primary shots without an uploaded full-length background', () => {
    const second = { ...image, media_path: 'freezone/remix_media/second.png', media_hash: 'e'.repeat(64) };
    const output = buildNodeRemixManifest({
      reference: video, narration: audio,
      timing: { fps: 30, events: [
        { event_id: 'opening', start_frame: 12, end_frame: 36 },
        { event_id: 'answer', start_frame: 50, end_frame: 88 },
      ], unresolved_required_events: [], override_conflicts: [] },
      script: { dialogue: '开场回答', events: [
        { event_id: 'opening', caption: '开场' },
        { event_id: 'answer', caption: '回答' },
      ] },
      primaryVisuals: { opening: image, answer: second },
    });
    expect(output.media_bindings).not.toContainEqual(expect.objectContaining({ purpose: 'background' }));
    expect(output.layers.filter((layer) => layer.z_order === 0)).toEqual([
      expect.objectContaining({ start_frame: 0, end_frame: 50, media_id: 'primary-opening' }),
      expect.objectContaining({ start_frame: 50, end_frame: 120, media_id: 'primary-answer' }),
    ]);
  });

  it('uses an already generated B-roll shot as primary coverage when that event has no separate main shot', () => {
    const output = buildNodeRemixManifest({
      reference: video, narration: audio,
      timing: { fps: 30, events: [{ event_id: 'product', start_frame: 10, end_frame: 45 }], unresolved_required_events: [], override_conflicts: [] },
      script: { dialogue: '产品', events: [{ event_id: 'product', caption: '产品', visual_node_id: 'visual', visual_media_revision: image.media_hash }] },
      visuals: { visual: image },
    });
    expect(output.layers).toEqual([expect.objectContaining({ row_id: 'primary:product', start_frame: 0, end_frame: 120, z_order: 0 })]);
  });

  it('binds compiled timing to project media and script captions', () => {
    const output = buildNodeRemixManifest({
      reference: video, background,
      narration: audio,
      timing: { fps: 30, events: [{ event_id: 'reveal', start_frame: 12, end_frame: 45 }], unresolved_required_events: [], override_conflicts: [] },
      script: { dialogue: '喝一口咖啡', events: [{ event_id: 'reveal', caption: '喝一口咖啡' }] },
    });
    expect(output.subtitle_events).toEqual([{ text: '喝一口咖啡', start_frame: 12, end_frame: 45 }]);
    expect(output.manifest_version).toBe(2);
    expect(output.output_spec).toMatchObject({ width: 1080, height: 1920, fps: 30, subtitle_safe_area: { left: 90, right: 90, top: 120, bottom: 180 } });
    expect(output.layers).toEqual([{ row_id: 'background', media_id: 'background', start_frame: 0, end_frame: 120, z_order: 0, fit: 'cover' }]);
    expect(output.media_bindings).not.toContainEqual(expect.objectContaining({ media_id: 'reference' }));
    expect(output.media_bindings).toHaveLength(2);
  });

  it('adds word cue frames when a subtitle matches its aligned script selection', () => {
    const output = buildNodeRemixManifest({
      reference: video, background, narration: audio,
      timing: { fps: 30, events: [{ event_id: 'reveal', start_frame: 12, end_frame: 45 }], unresolved_required_events: [], override_conflicts: [] },
      script: { dialogue: '喝一口咖啡', selections: [{ selection_id: 'reveal', start_char: 0, end_char: 5 }], events: [{ event_id: 'reveal', selection_id: 'reveal', caption: '喝一口咖啡' }] },
      wordTimeline: { transcript: '喝一口咖啡', words: [
        { text: '喝', start_char: 0, end_char: 1, start_us: 400_000, end_us: 700_000 },
        { text: '一口', start_char: 1, end_char: 3, start_us: 700_000, end_us: 900_000 },
        { text: '咖啡', start_char: 3, end_char: 5, start_us: 900_000, end_us: 1_400_000 },
      ] },
    });
    expect(output.subtitle_events).toEqual([{ text: '喝一口咖啡', start_frame: 12, end_frame: 45, word_cues: [
      { start_char: 0, end_char: 1, start_frame: 12, end_frame: 21 },
      { start_char: 1, end_char: 3, start_frame: 21, end_frame: 27 },
      { start_char: 3, end_char: 5, start_frame: 27, end_frame: 42 },
    ] }]);
  });

  it('groups character-aligned English letters into spoken words', () => {
    const output = buildNodeRemixManifest({
      reference: video, background, narration: audio,
      timing: { fps: 30, events: [{ event_id: 'hook', start_frame: 0, end_frame: 30 }], unresolved_required_events: [], override_conflicts: [] },
      script: { dialogue: 'go home', selections: [{ selection_id: 'hook', start_char: 0, end_char: 7 }], events: [{ event_id: 'hook', selection_id: 'hook' }] },
      wordTimeline: { transcript: 'go home', words: [
        { text: 'g', start_char: 0, end_char: 1, start_us: 0, end_us: 100_000 },
        { text: 'o', start_char: 1, end_char: 2, start_us: 100_000, end_us: 200_000 },
        { text: 'h', start_char: 3, end_char: 4, start_us: 500_000, end_us: 600_000 },
        { text: 'o', start_char: 4, end_char: 5, start_us: 600_000, end_us: 700_000 },
        { text: 'm', start_char: 5, end_char: 6, start_us: 700_000, end_us: 800_000 },
        { text: 'e', start_char: 6, end_char: 7, start_us: 800_000, end_us: 900_000 },
      ] },
    });
    expect(output.subtitle_events[0].word_cues).toEqual([
      { start_char: 0, end_char: 2, start_frame: 0, end_frame: 6 },
      { start_char: 3, end_char: 7, start_frame: 15, end_frame: 27 },
    ]);
  });

  it('blocks unresolved required semantic events', () => {
    expect(() => buildNodeRemixManifest({ reference: video, background, narration: audio, timing: { fps: 30, events: [], unresolved_required_events: [{ event_id: 'hook' }], override_conflicts: [] }, script: { dialogue: '', events: [] } })).toThrow(/未解析/);
  });

  it('uses the new narration duration and loops a shorter reference video', () => {
    const shortReference = { ...video, duration_us: 1_000_000 };
    const output = buildNodeRemixManifest({ reference: shortReference, background, narration: audio, timing: { fps: 30, events: [{ event_id: 'late', start_frame: 60, end_frame: 90 }], unresolved_required_events: [], override_conflicts: [] }, script: { dialogue: '新台词', events: [{ event_id: 'late', caption: '新台词' }] } });
    expect(output.output_spec.duration_frames).toBe(120);
    expect(output.media_bindings).not.toContainEqual(expect.objectContaining({ media_id: 'reference' }));
    expect(output.layers[0]).toMatchObject({ end_frame: 120 });
  });

  it('mixes a connected BGM track and a cue-bound SFX track', () => {
    const bgm = { ...audio, media_path: 'input/bgm.wav', media_hash: 'c'.repeat(64), duration_us: 1_000_000 };
    const sfx = { ...audio, media_path: 'input/sfx.wav', media_hash: 'd'.repeat(64), duration_us: 500_000 };
    const output = buildNodeRemixManifest({ reference: video, background, narration: audio, timing: { fps: 30, events: [{ event_id: 'reveal', start_frame: 30, end_frame: 90 }], unresolved_required_events: [], override_conflicts: [] }, script: { dialogue: '登场', events: [{ event_id: 'reveal', caption: '登场', sfx_node_id: 'sfx-node' }] }, audioTracks: [{ id: 'bgm-node', role: 'bgm', media: bgm }, { id: 'sfx-node', role: 'sfx', media: sfx }] });
    expect(output.audio_events).toEqual(expect.arrayContaining([
      expect.objectContaining({ role: 'bgm', start_frame: 0, end_frame: 120 }),
      expect.objectContaining({ role: 'sfx', start_frame: 30, end_frame: 45 }),
    ]));
    expect(output.media_bindings).toContainEqual(expect.objectContaining({ media_id: 'bgm-bgm-node', loop: true }));
  });

  it('assigns stable unique row IDs when one visual and SFX are reused by repeated events', () => {
    const sfx = { ...audio, media_path: 'input/sfx.wav', media_hash: 'd'.repeat(64), duration_us: 500_000 };
    const output = buildNodeRemixManifest({ reference: video, background, narration: audio,
      timing: { fps: 30, events: [{ event_id: 'first', start_frame: 0, end_frame: 30 }, { event_id: 'second', start_frame: 60, end_frame: 90 }], unresolved_required_events: [], override_conflicts: [] },
      script: { dialogue: '重复素材', events: [
        { event_id: 'first', caption: '第一次', visual_node_id: 'visual', visual_media_revision: image.media_hash, sfx_node_id: 'sfx' },
        { event_id: 'second', caption: '第二次', visual_node_id: 'visual', visual_media_revision: image.media_hash, sfx_node_id: 'sfx' },
      ] }, visuals: { visual: image }, audioTracks: [{ id: 'sfx', role: 'sfx', media: sfx }],
    });
    expect(output.layers.filter((row) => row.media_id === 'visual-visual').map((row) => row.row_id)).toEqual(['visual:first', 'visual:second']);
    expect(output.audio_events.filter((row) => row.role === 'sfx').map((row) => row.row_id)).toEqual(['sfx:first:sfx', 'sfx:second:sfx']);
  });

  it('binds an uploaded image by exact project path and selected media revision', () => {
    const output = buildNodeRemixManifest({ reference: video, background, narration: audio,
      timing: { fps: 30, events: [{ event_id: 'product', start_frame: 12, end_frame: 45 }], unresolved_required_events: [], override_conflicts: [] },
      script: { dialogue: '咖啡', events: [{ event_id: 'product', caption: '咖啡', visual_node_id: 'image-1', visual_media_revision: image.media_hash }] },
      visuals: { 'image-1': image },
    });
    expect(output.media_bindings).toContainEqual({ media_id: 'visual-image-1', kind: 'image', project_path: image.media_path, media_hash: `sha256:${image.media_hash}` });
    expect(output.layers).toContainEqual({ row_id: 'visual:product', media_id: 'visual-image-1', start_frame: 12, end_frame: 45, z_order: 1, fit: 'cover', fade_in_frames: 4, fade_out_frames: 4, zoom: { start: 1, end: 1.04 } });
  });

  it.each([
    { endFrame: 1, fadeFrames: 0 },
    { endFrame: 6, fadeFrames: 3 },
    { endFrame: 7, fadeFrames: 3 },
    { endFrame: 8, fadeFrames: 4 },
  ])('keeps B-roll fades within a $endFrame-frame event', ({ endFrame, fadeFrames }) => {
    const output = buildNodeRemixManifest({ reference: video, background, narration: audio,
      timing: { fps: 30, events: [{ event_id: 'flash', start_frame: 0, end_frame: endFrame }], unresolved_required_events: [], override_conflicts: [] },
      script: { dialogue: '闪', events: [{ event_id: 'flash', caption: '闪', visual_node_id: 'image-1', visual_media_revision: image.media_hash }] },
      visuals: { 'image-1': image },
    });
    expect(output.layers).toContainEqual(expect.objectContaining({
      row_id: 'visual:flash',
      fade_in_frames: fadeFrames,
      fade_out_frames: fadeFrames,
    }));
  });

  it('binds an uploaded video by exact project path and selected media revision', () => {
    const output = buildNodeRemixManifest({ reference: video, background, narration: audio,
      timing: { fps: 30, events: [{ event_id: 'product', start_frame: 12, end_frame: 45 }], unresolved_required_events: [], override_conflicts: [] },
      script: { dialogue: '咖啡', events: [{ event_id: 'product', caption: '咖啡', visual_node_id: 'video-1', visual_media_revision: visualVideo.media_hash }] },
      visuals: { 'video-1': visualVideo },
    });
    expect(output.media_bindings).toContainEqual({ media_id: 'visual-video-1', kind: 'video', project_path: visualVideo.media_path, media_hash: `sha256:${visualVideo.media_hash}` });
  });

  it('binds a generated visual node using the immutable generated media revision', () => {
    const generated = { ...image, media_path: 'freezone/image/generated/product.webp', media_hash: 'e'.repeat(64) };
    const output = buildNodeRemixManifest({ reference: video, background, narration: audio,
      timing: { fps: 30, events: [{ event_id: 'product', start_frame: 12, end_frame: 45 }], unresolved_required_events: [], override_conflicts: [] },
      script: { dialogue: '咖啡', events: [{ event_id: 'product', caption: '咖啡', visual_node_id: 'generated-1', visual_media_revision: generated.media_hash }] },
      visuals: { 'generated-1': generated },
    });
    expect(output.media_bindings).toContainEqual(expect.objectContaining({ project_path: generated.media_path, media_hash: `sha256:${generated.media_hash}` }));
  });

  it('rejects a required visual that has no attached media before submission', () => {
    expect(() => buildNodeRemixManifest({ reference: video, background, narration: audio,
      timing: { fps: 30, events: [{ event_id: 'product', start_frame: 12, end_frame: 45 }], unresolved_required_events: [], override_conflicts: [] },
      script: { dialogue: '咖啡', events: [{ event_id: 'product', caption: '咖啡', visual_node_id: 'missing', visual_media_revision: 'f'.repeat(64) }] },
    })).toThrow(/缺少替换画面/);
  });

  it('rejects a visual whose media hash changed after the script selection', () => {
    expect(() => buildNodeRemixManifest({ reference: video, background, narration: audio,
      timing: { fps: 30, events: [{ event_id: 'product', start_frame: 12, end_frame: 45 }], unresolved_required_events: [], override_conflicts: [] },
      script: { dialogue: '咖啡', events: [{ event_id: 'product', caption: '咖啡', visual_node_id: 'image-1', visual_media_revision: 'f'.repeat(64) }] },
      visuals: { 'image-1': image },
    })).toThrow(/素材已变化/);
  });

  it('requires planned primary coverage instead of rendering the analysis reference', () => {
    expect(() => buildNodeRemixManifest({ reference: video, narration: audio,
      timing: { fps: 30, events: [], unresolved_required_events: [], override_conflicts: [] },
      script: { dialogue: '', events: [] },
    })).toThrow(/主画面/);
  });
});
