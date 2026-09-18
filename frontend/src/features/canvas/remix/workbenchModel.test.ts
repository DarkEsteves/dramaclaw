import { describe, expect, it } from 'vitest';
import { buildWorkbenchManifest, type UploadedRemixMedia } from './workbenchModel';

const video: UploadedRemixMedia = { media_path: 'remix/uploads/video.mp4', media_hash: 'a'.repeat(64), url: '/video.mp4', size: 100, media_kind: 'video', duration_us: 3_000_000 };
const audio: UploadedRemixMedia = { media_path: 'remix/uploads/voice.wav', media_hash: 'b'.repeat(64), url: '/voice.wav', size: 50, media_kind: 'audio', duration_us: 3_000_000 };

describe('remix workbench manifest', () => {
  it('binds uploaded media and manually timed captions into a 9:16 render', () => {
    const result = buildWorkbenchManifest(video, audio, [{ id: 'hook', label: '开场', text: '你好', startSeconds: 0.25, endSeconds: 1.5 }]);
    expect(result).toMatchObject({
      output_spec: { width: 720, height: 1280, fps: 30, duration_frames: 90 },
      media_bindings: [
        { media_id: 'reference', kind: 'video', project_path: video.media_path, media_hash: `sha256:${video.media_hash}` },
        { media_id: 'narration', kind: 'audio', project_path: audio.media_path, media_hash: `sha256:${audio.media_hash}` },
      ],
      subtitle_events: [{ text: '你好', start_frame: 8, end_frame: 45 }],
    });
  });

  it('rejects captions outside the video or overlapping', () => {
    expect(() => buildWorkbenchManifest(video, audio, [{ id: 'a', label: 'A', text: 'late', startSeconds: 2, endSeconds: 4 }])).toThrow(/视频时长/);
    expect(() => buildWorkbenchManifest(video, audio, [
      { id: 'a', label: 'A', text: 'first', startSeconds: 0, endSeconds: 2 },
      { id: 'b', label: 'B', text: 'second', startSeconds: 1, endSeconds: 3 },
    ])).toThrow(/重叠/);
  });

  it('uses the reference video audio when no separate narration is uploaded', () => {
    const result = buildWorkbenchManifest({ ...video, has_audio: true }, { ...video, has_audio: true }, []);
    expect(result.media_bindings).toEqual(expect.arrayContaining([expect.objectContaining({ media_id: 'narration', kind: 'audio', project_path: video.media_path })]));
  });

  it('places a replacement clip at its semantic cue time', () => {
    const replacement = { ...video, media_path: 'media/product.mp4', media_hash: 'c'.repeat(64), duration_us: 2_000_000 };
    const result = buildWorkbenchManifest(video, audio, [{ id: 'product', label: '产品登场', text: '看这里', startSeconds: 1, endSeconds: 2, visual: replacement }]);
    expect(result.media_bindings).toEqual(expect.arrayContaining([expect.objectContaining({ media_id: 'cue-product', project_path: replacement.media_path })]));
    expect(result.layers).toEqual(expect.arrayContaining([expect.objectContaining({ media_id: 'cue-product', start_frame: 30, end_frame: 60, z_order: 1 })]));
  });
});
