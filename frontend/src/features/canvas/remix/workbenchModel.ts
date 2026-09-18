export interface UploadedRemixMedia {
  media_path: string;
  media_hash: string;
  url: string;
  size: number;
  media_kind: 'video' | 'audio';
  duration_us: number;
  has_audio?: boolean;
}

export interface RemixCaptionCue {
  id: string;
  label: string;
  text: string;
  startSeconds: number;
  endSeconds: number;
  visual?: UploadedRemixMedia;
}

export interface RemixWorkbenchDraft {
  video?: UploadedRemixMedia;
  audio?: UploadedRemixMedia;
  cues: RemixCaptionCue[];
  manifestRef?: { artifactId: string; revision: number };
  nodeRevision?: number;
  operationId?: string;
  resultPath?: string;
  status?: string;
  transcription?: { operationId: string; mediaPath: string; nodeRevision: number };
}

export function buildWorkbenchManifest(video: UploadedRemixMedia, audio: UploadedRemixMedia, cues: RemixCaptionCue[]): Record<string, unknown> {
  if (video.media_kind !== 'video' || (audio.media_kind !== 'audio' && !(audio.media_kind === 'video' && audio.has_audio))) throw new Error('请分别上传视频和旁白音频');
  if (!/^[a-f0-9]{64}$/.test(video.media_hash) || !/^[a-f0-9]{64}$/.test(audio.media_hash)) throw new Error('媒体文件缺少校验值');
  const fps = 30;
  const durationFrames = Math.min(Math.floor(video.duration_us * fps / 1_000_000), Math.floor(audio.duration_us * fps / 1_000_000), 60 * fps);
  if (durationFrames < 1) throw new Error('视频或音频时长无效');
  const cueFrames = cues.map((cue) => {
    const start = Math.round(cue.startSeconds * fps);
    const end = Math.round(cue.endSeconds * fps);
    if (!cue.text.trim()) throw new Error('字幕内容不能为空');
    if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || start >= end || end > durationFrames) throw new Error('字幕时间超出视频时长');
    if (cue.visual && (cue.visual.media_kind !== 'video' || cue.visual.duration_us * fps / 1_000_000 < end - start)) throw new Error('替换视频时长不足');
    return { cue, text: cue.text.trim(), start_frame: start, end_frame: end };
  }).sort((a, b) => a.start_frame - b.start_frame);
  for (let index = 1; index < cueFrames.length; index += 1) {
    if (cueFrames[index].start_frame < cueFrames[index - 1].end_frame) throw new Error('字幕时间不能重叠');
  }
  const visualBindings = cueFrames.filter((row) => row.cue.visual).map(({ cue }) => ({ media_id: `cue-${cue.id}`, kind: 'video', project_path: cue.visual!.media_path, media_hash: `sha256:${cue.visual!.media_hash}` }));
  const visualLayers = cueFrames.filter((row) => row.cue.visual).map(({ cue, start_frame, end_frame }) => ({ media_id: `cue-${cue.id}`, start_frame, end_frame, z_order: 1, fit: 'cover' }));
  return {
    output_spec: { width: 720, height: 1280, fps, duration_frames: durationFrames },
    media_bindings: [
      { media_id: 'reference', kind: 'video', project_path: video.media_path, media_hash: `sha256:${video.media_hash}` },
      { media_id: 'narration', kind: 'audio', project_path: audio.media_path, media_hash: `sha256:${audio.media_hash}` },
      ...visualBindings,
    ],
    layers: [{ media_id: 'reference', start_frame: 0, end_frame: durationFrames, z_order: 0, fit: 'cover' }, ...visualLayers],
    subtitle_events: cueFrames.map(({ text, start_frame, end_frame }) => ({ text, start_frame, end_frame })),
    audio_events: [{ media_id: 'narration', role: 'narration', start_frame: 0, end_frame: durationFrames, volume: 1 }],
    manual_overrides: [],
    source_versions: [],
  };
}
