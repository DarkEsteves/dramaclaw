import type { UploadedRemixMedia } from './workbenchModel';

export interface CompiledTiming {
  fps: number;
  events: Array<{ event_id: string; start_frame: number; end_frame: number }>;
  unresolved_required_events: Array<{ event_id: string; reason?: string }>;
  override_conflicts: Array<Record<string, unknown>>;
}

interface ScriptEvent { event_id: string; caption?: string; selection_id?: string; visual_node_id?: string; sfx_node_id?: string }
interface ScriptPayload { dialogue: string; events: ScriptEvent[]; selections?: Array<{ selection_id: string; start_char: number; end_char: number }> }

interface Inputs {
  reference: UploadedRemixMedia;
  narration: UploadedRemixMedia;
  timing: CompiledTiming;
  script: ScriptPayload;
  visuals?: Record<string, UploadedRemixMedia>;
  audioTracks?: Array<{ id: string; role: 'bgm' | 'sfx'; media: UploadedRemixMedia }>;
}

export function buildNodeRemixManifest({ reference, narration, timing, script, visuals = {}, audioTracks = [] }: Inputs) {
  if (reference.media_kind !== 'video') throw new Error('参考素材必须是视频');
  if (narration.media_kind !== 'audio' && !(narration.media_kind === 'video' && narration.has_audio)) throw new Error('需要实际旁白音轨');
  if (timing.unresolved_required_events.length) throw new Error('存在未解析的必需事件');
  if (timing.override_conflicts.length) throw new Error('时间覆盖有冲突');
  const fps = timing.fps;
  if (!Number.isInteger(fps) || fps <= 0) throw new Error('帧率无效');
  const durationFrames = Math.floor(narration.duration_us * fps / 1_000_000);
  if (durationFrames < 1) throw new Error('素材时长无效');
  const bindings: Array<Record<string, unknown>> = [
    { media_id: 'reference', kind: 'video', project_path: reference.media_path, media_hash: `sha256:${reference.media_hash}`, ...(reference.duration_us < narration.duration_us ? { loop: true } : {}) },
    { media_id: 'narration', kind: 'audio', project_path: narration.media_path, media_hash: `sha256:${narration.media_hash}` },
  ];
  const layers: Array<Record<string, unknown>> = [{ media_id: 'reference', start_frame: 0, end_frame: durationFrames, z_order: 0, fit: 'cover' }];
  const subtitles: Array<{ text: string; start_frame: number; end_frame: number }> = [];
  const audioEvents: Array<Record<string, unknown>> = [{ media_id: 'narration', role: 'narration', start_frame: 0, end_frame: durationFrames, volume: 1 }];
  for (const track of audioTracks) {
    if (track.media.media_kind !== 'audio') throw new Error(`${track.role} 必须是音频文件`);
    const mediaId = `${track.role}-${track.id}`;
    bindings.push({ media_id: mediaId, kind: 'audio', project_path: track.media.media_path, media_hash: `sha256:${track.media.media_hash}`, ...(track.role === 'bgm' && track.media.duration_us < narration.duration_us ? { loop: true } : {}) });
    if (track.role === 'bgm') audioEvents.push({ media_id: mediaId, role: 'bgm', start_frame: 0, end_frame: durationFrames, volume: 0.3 });
  }
  for (const event of timing.events) {
    if (!Number.isInteger(event.start_frame) || !Number.isInteger(event.end_frame) || event.start_frame < 0 || event.end_frame <= event.start_frame || event.end_frame > durationFrames) throw new Error(`事件 ${event.event_id} 超出素材时长`);
    const rule = script.events.find((item) => item.event_id === event.event_id);
    if (!rule) throw new Error(`事件 ${event.event_id} 缺少脚本规则`);
    const selection = script.selections?.find((item) => item.selection_id === rule.selection_id);
    const caption = rule.caption || (selection ? Array.from(script.dialogue).slice(selection.start_char, selection.end_char).join('') : '');
    if (caption.trim()) subtitles.push({ text: caption.trim(), start_frame: event.start_frame, end_frame: event.end_frame });
    if (rule.visual_node_id) {
      const media = visuals[rule.visual_node_id];
      if (!media || media.media_kind !== 'video') throw new Error(`事件 ${event.event_id} 缺少替换画面`);
      const mediaId = `visual-${rule.visual_node_id}`;
      if (!bindings.some((binding) => binding.media_id === mediaId)) bindings.push({ media_id: mediaId, kind: 'video', project_path: media.media_path, media_hash: `sha256:${media.media_hash}` });
      layers.push({ media_id: mediaId, start_frame: event.start_frame, end_frame: event.end_frame, z_order: 1, fit: 'cover' });
    }
    if (!caption.trim() && !rule.visual_node_id) throw new Error(`事件 ${event.event_id} 没有字幕或画面输出`);
    if (rule.sfx_node_id) {
      const sfx = audioTracks.find((track) => track.id === rule.sfx_node_id && track.role === 'sfx');
      if (!sfx) throw new Error(`事件 ${event.event_id} 缺少音效素材`);
      const length = Math.floor(sfx.media.duration_us * fps / 1_000_000);
      if (length < 1) throw new Error(`事件 ${event.event_id} 的音效时长不足一帧`);
      audioEvents.push({ media_id: `sfx-${sfx.id}`, role: 'sfx', start_frame: event.start_frame, end_frame: Math.min(event.end_frame, event.start_frame + length), volume: 1 });
    }
  }
  return {
    output_spec: { width: 720, height: 1280, fps, duration_frames: durationFrames },
    media_bindings: bindings,
    layers,
    subtitle_events: subtitles,
    audio_events: audioEvents,
    manual_overrides: [],
    source_versions: [],
  };
}
