import { describe, expect, it } from 'vitest';
import { buildTakeRemixManifest, type TakeTiming } from './takeRemixManifest';

const first = { media_path: 'media/first.mp4', media_hash: 'a'.repeat(64), url: '/first.mp4', size: 1, media_kind: 'video' as const, duration_us: 1_000_000, has_audio: true };
const second = { ...first, media_path: 'media/second.mp4', media_hash: 'b'.repeat(64), url: '/second.mp4' };
const broll = { media_path: 'media/product.png', media_hash: 'c'.repeat(64), url: '/product.png', size: 1, media_kind: 'image' as const, duration_us: 0 };

describe('take-based remix manifest', () => {
  const timing: TakeTiming = { fps: 30, duration_frames: 60, takes: [
    { segment_id: 'one', source: 'speaking_video', media_hash: `sha256:${first.media_hash}`, start_frame: 0, end_frame: 30, caption: '你好', word_cues: [{ start_char: 0, end_char: 2, start_frame: 2, end_frame: 20 }] },
    { segment_id: 'two', source: 'speaking_video', media_hash: `sha256:${second.media_hash}`, start_frame: 30, end_frame: 60, caption: '咖啡', word_cues: [{ start_char: 0, end_char: 2, start_frame: 32, end_frame: 50 }] },
  ], events: [{ event_id: 'product', start_frame: 35, end_frame: 50 }], unresolved_required_events: [], override_conflicts: [] };
  const script = { dialogue: '男：你好\n女：咖啡', events: [{ event_id: 'product', visual_node_id: 'product', visual_media_revision: broll.media_hash }] };

  it('uses each speaking video for both picture and sound while overlaying silent B-roll', () => {
    const manifest = buildTakeRemixManifest({ timing, script, takes: { one: first, two: second }, visuals: { product: broll } });
    expect(manifest.manifest_version).toBe(3);
    expect(manifest.audio_events).toEqual([
      expect.objectContaining({ media_id: 'take-one', role: 'speech', start_frame: 0, end_frame: 30 }),
      expect.objectContaining({ media_id: 'take-two', role: 'speech', start_frame: 30, end_frame: 60 }),
    ]);
    expect(manifest.layers).toContainEqual(expect.objectContaining({ media_id: 'visual-product', z_order: 1, start_frame: 35, end_frame: 50 }));
    expect(manifest.subtitle_events).toContainEqual(expect.objectContaining({ text: '咖啡', start_frame: 30, end_frame: 60 }));
  });

  it('rejects a changed or silent speaking video', () => {
    expect(() => buildTakeRemixManifest({ timing, script, takes: { one: { ...first, has_audio: false }, two: second }, visuals: { product: broll } })).toThrow(/声音/);
    expect(() => buildTakeRemixManifest({ timing, script, takes: { one: { ...first, media_hash: 'd'.repeat(64) }, two: second }, visuals: { product: broll } })).toThrow(/变化/);
  });

  it('uses independent voiceover audio over a looping main visual', () => {
    const voice = { media_path: 'media/voice.wav', media_hash: 'd'.repeat(64), url: '/voice.wav', size: 1, media_kind: 'audio' as const, duration_us: 2_000_000 };
    const silentVisual = { ...first, media_path: 'media/scene.mp4', media_hash: 'e'.repeat(64), has_audio: false, duration_us: 1_000_000 };
    const voiceTiming = { ...timing, duration_frames: 60, takes: [{ ...timing.takes[0], source: 'voiceover' as const, media_hash: `sha256:${voice.media_hash}`, end_frame: 60 }] , events: [] };
    const manifest = buildTakeRemixManifest({ timing: voiceTiming, script: { dialogue: '你好', events: [] }, takes: { one: voice }, primaryVisuals: { one: silentVisual } });
    expect(manifest.media_bindings).toContainEqual(expect.objectContaining({ media_id: 'primary-one', loop: true }));
    expect(manifest.audio_events).toContainEqual(expect.objectContaining({ media_id: 'take-one', role: 'speech' }));
  });
});
