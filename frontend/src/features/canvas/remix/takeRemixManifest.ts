import type { UploadedRemixMedia } from './workbenchModel';

export interface TimedRemixTake {
  segment_id: string;
  source: 'speaking_video' | 'voiceover' | 'original_sound';
  media_hash: string;
  start_frame: number;
  end_frame: number;
  caption: string;
  word_cues: Array<{ start_char: number; end_char: number; start_frame: number; end_frame: number }>;
}

export interface TakeTiming {
  fps: number;
  duration_frames: number;
  takes: TimedRemixTake[];
  events: Array<{ event_id: string; start_frame: number; end_frame: number }>;
  unresolved_required_events: Array<{ event_id: string }>;
  override_conflicts: Array<Record<string, unknown>>;
}

interface TakeScript {
  dialogue: string;
  events: Array<{ event_id: string; visual_node_id?: string; visual_media_revision?: string; sfx_node_id?: string }>;
}

interface Inputs {
  timing: TakeTiming;
  script: TakeScript;
  takes: Record<string, UploadedRemixMedia>;
  visuals?: Record<string, UploadedRemixMedia>;
  primaryVisuals?: Record<string, UploadedRemixMedia>;
  background?: UploadedRemixMedia;
  audioTracks?: Array<{ id: string; role: 'bgm' | 'sfx'; media: UploadedRemixMedia }>;
}

function binding(mediaId: string, media: UploadedRemixMedia, purpose?: string) {
  return { media_id: mediaId, kind: media.media_kind, ...(purpose ? { purpose } : {}),
    project_path: media.media_path, media_hash: `sha256:${media.media_hash}`,
    ...(media.media_kind === 'video' ? { has_audio: media.has_audio === true } : {}) };
}

/** Compile selected media, never a presumed global narration, into the saved render contract. */
export function buildTakeRemixManifest({ timing, script, takes, visuals = {}, primaryVisuals = {}, background, audioTracks = [] }: Inputs) {
  if (timing.unresolved_required_events.length || timing.override_conflicts.length) throw new Error('存在未解析的时间轴事件');
  const { fps, duration_frames: durationFrames } = timing;
  if (!Number.isInteger(fps) || fps <= 0 || !Number.isInteger(durationFrames) || durationFrames <= 0) throw new Error('时间轴时长无效');
  const bindings: Array<Record<string, unknown>> = [];
  const layers: Array<Record<string, unknown>> = [];
  const audioEvents: Array<Record<string, unknown>> = [];
  const subtitles: Array<Record<string, unknown>> = [];
  let cursor = 0;
  for (const take of timing.takes) {
    if (take.start_frame !== cursor || take.end_frame <= cursor) throw new Error('表演段落时间不连续');
    const media = takes[take.segment_id];
    if (!media || `sha256:${media.media_hash}` !== take.media_hash) throw new Error(`表演段落 ${take.segment_id} 素材已变化`);
    if (media.duration_us * fps / 1_000_000 + 0.5 < take.end_frame - take.start_frame) throw new Error(`表演段落 ${take.segment_id} 素材时长不足`);
    const id = `take-${take.segment_id}`;
    if (take.source === 'voiceover') {
      if (media.media_kind !== 'audio' || media.has_audio === false) throw new Error(`表演段落 ${take.segment_id} 缺少配音声音`);
      const visual = primaryVisuals[take.segment_id] ?? background;
      if (!visual || (visual.media_kind !== 'image' && visual.media_kind !== 'video')) throw new Error(`旁白段落 ${take.segment_id} 缺少主画面`);
      const visualId = `primary-${take.segment_id}`;
      bindings.push({ ...binding(visualId, visual, 'a-roll'),
        ...(visual.media_kind === 'video' && visual.duration_us * fps / 1_000_000 < take.end_frame - take.start_frame ? { loop: true } : {}) });
      layers.push({ row_id: `primary:${take.segment_id}`, media_id: visualId, start_frame: take.start_frame, end_frame: take.end_frame, z_order: 0, fit: 'cover' });
    } else {
      if (media.media_kind !== 'video' || media.has_audio !== true) throw new Error(`表演段落 ${take.segment_id} 需要带声音的视频`);
      layers.push({ row_id: `primary:${take.segment_id}`, media_id: id, start_frame: take.start_frame, end_frame: take.end_frame, z_order: 0, fit: 'cover' });
    }
    bindings.push(binding(id, media, take.source === 'voiceover' ? 'voiceover' : 'a-roll'));
    audioEvents.push({ row_id: `speech:${take.segment_id}`, media_id: id, role: 'speech', start_frame: take.start_frame, end_frame: take.end_frame, volume: 1 });
    subtitles.push({ row_id: `caption:${take.segment_id}`, text: take.caption, start_frame: take.start_frame, end_frame: take.end_frame,
      word_cues: take.word_cues.map(({ start_char, end_char, start_frame, end_frame }) => ({ start_char, end_char, start_frame, end_frame })) });
    cursor = take.end_frame;
  }
  if (cursor !== durationFrames) throw new Error('表演段落时长与时间轴不一致');
  for (const event of timing.events) {
    const rule = script.events.find((item) => item.event_id === event.event_id);
    if (!rule) throw new Error(`事件 ${event.event_id} 缺少脚本规则`);
    if (rule.visual_node_id) {
      const media = visuals[rule.visual_node_id];
      if (!media || media.media_hash !== rule.visual_media_revision) throw new Error(`事件 ${event.event_id} 的补充画面已变化`);
      if (media.media_kind !== 'image' && media.media_kind !== 'video') throw new Error(`事件 ${event.event_id} 的补充画面类型无效`);
      const id = `visual-${rule.visual_node_id}`;
      if (!bindings.some((row) => row.media_id === id)) bindings.push(binding(id, media));
      const fade = Math.min(4, Math.floor((event.end_frame - event.start_frame) / 2));
      layers.push({ row_id: `visual:${event.event_id}`, media_id: id, start_frame: event.start_frame, end_frame: event.end_frame,
        z_order: 1, fit: 'cover', fade_in_frames: fade, fade_out_frames: fade });
    }
    if (rule.sfx_node_id) {
      const sfx = audioTracks.find((track) => track.id === rule.sfx_node_id && track.role === 'sfx');
      if (!sfx) throw new Error(`事件 ${event.event_id} 缺少音效素材`);
      const id = `sfx-${sfx.id}`;
      if (!bindings.some((row) => row.media_id === id)) bindings.push(binding(id, sfx.media));
      audioEvents.push({ row_id: `sfx:${event.event_id}`, media_id: id, role: 'sfx', start_frame: event.start_frame,
        end_frame: Math.min(event.end_frame, event.start_frame + Math.floor(sfx.media.duration_us * fps / 1_000_000)), volume: 1 });
    }
  }
  for (const track of audioTracks.filter((item) => item.role === 'bgm')) {
    const id = `bgm-${track.id}`;
    bindings.push({ ...binding(id, track.media), ...(track.media.duration_us * fps / 1_000_000 < durationFrames ? { loop: true } : {}) });
    audioEvents.push({ row_id: id, media_id: id, role: 'bgm', start_frame: 0, end_frame: durationFrames, volume: 0.3, duck_under_narration: 0.35 });
  }
  return { manifest_version: 3,
    output_spec: { width: 1080, height: 1920, fps, duration_frames: durationFrames, subtitle_safe_area: { left: 90, right: 90, top: 120, bottom: 180 } },
    media_bindings: bindings, layers, subtitle_events: subtitles, audio_events: audioEvents, manual_overrides: [], source_versions: [] };
}
