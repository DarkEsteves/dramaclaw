import type { UploadedRemixMedia } from './workbenchModel';

export interface CompiledTiming {
  fps: number;
  events: Array<{ event_id: string; start_frame: number; end_frame: number }>;
  unresolved_required_events: Array<{ event_id: string; reason?: string }>;
  override_conflicts: Array<Record<string, unknown>>;
}

interface ScriptEvent { event_id: string; caption?: string; selection_id?: string; visual_node_id?: string; visual_media_revision?: string; sfx_node_id?: string }
interface ScriptPayload { dialogue: string; events: ScriptEvent[]; selections?: Array<{ selection_id: string; start_char: number; end_char: number }> }
interface WordTimeline { transcript: string; words: Array<{ text: string; start_char: number; end_char: number; start_us: number; end_us: number }> }
interface WordCue { start_char: number; end_char: number; start_frame: number; end_frame: number }

interface Inputs {
  reference: UploadedRemixMedia;
  background?: UploadedRemixMedia;
  narration: UploadedRemixMedia;
  timing: CompiledTiming;
  script: ScriptPayload;
  wordTimeline?: WordTimeline;
  visuals?: Record<string, UploadedRemixMedia>;
  audioTracks?: Array<{ id: string; role: 'bgm' | 'sfx'; media: UploadedRemixMedia }>;
}

function subtitleWordCues(
  selection: NonNullable<ScriptPayload['selections']>[number] | undefined,
  caption: string, script: ScriptPayload, timeline: WordTimeline | undefined,
  startFrame: number, endFrame: number, fps: number,
): WordCue[] {
  if (!selection || !timeline || timeline.transcript !== script.dialogue) return [];
  const selectedText = Array.from(script.dialogue).slice(selection.start_char, selection.end_char).join('');
  if (selectedText !== caption) return [];
  const candidates = timeline.words.filter((word) => word.start_char >= selection.start_char && word.end_char <= selection.end_char);
  const spokenUnits: WordTimeline['words'] = [];
  for (const word of candidates) {
    const previous = spokenUnits[spokenUnits.length - 1];
    if (previous && previous.end_char === word.start_char
      && /^[A-Za-z0-9'’]+$/u.test(previous.text) && /^[A-Za-z0-9'’]+$/u.test(word.text)) {
      previous.text += word.text;
      previous.end_char = word.end_char;
      previous.end_us = word.end_us;
    } else spokenUnits.push({ ...word });
  }
  const cues: WordCue[] = [];
  for (const word of spokenUnits) {
    const start = Math.max(startFrame, Math.floor(word.start_us * fps / 1_000_000));
    const end = Math.min(endFrame, Math.ceil(word.end_us * fps / 1_000_000));
    const startAfterPrevious = Math.max(start, cues.length ? cues[cues.length - 1].end_frame : startFrame);
    if (startAfterPrevious >= end) return [];
    cues.push({ start_char: word.start_char - selection.start_char, end_char: word.end_char - selection.start_char, start_frame: startAfterPrevious, end_frame: end });
  }
  return cues;
}

export function buildNodeRemixManifest({ reference, background, narration, timing, script, wordTimeline, visuals = {}, audioTracks = [] }: Inputs) {
  if (reference.media_kind !== 'video') throw new Error('参考素材必须是视频');
  if (!background || (background.media_kind !== 'image' && background.media_kind !== 'video')) throw new Error('请选择单独的成片背景或 A-roll');
  if (narration.media_kind !== 'audio' && !(narration.media_kind === 'video' && narration.has_audio)) throw new Error('需要实际旁白音轨');
  if (timing.unresolved_required_events.length) throw new Error('存在未解析的必需事件');
  if (timing.override_conflicts.length) throw new Error('时间覆盖有冲突');
  const fps = timing.fps;
  if (!Number.isInteger(fps) || fps <= 0) throw new Error('帧率无效');
  const durationFrames = Math.floor(narration.duration_us * fps / 1_000_000);
  if (durationFrames < 1) throw new Error('素材时长无效');
  const bindings: Array<Record<string, unknown>> = [
    { media_id: 'background', kind: background.media_kind, purpose: 'background', project_path: background.media_path, media_hash: `sha256:${background.media_hash}`, ...(background.media_kind === 'video' && background.duration_us < narration.duration_us ? { loop: true } : {}) },
    { media_id: 'narration', kind: 'audio', project_path: narration.media_path, media_hash: `sha256:${narration.media_hash}` },
  ];
  const layers: Array<Record<string, unknown>> = [{ row_id: 'background', media_id: 'background', start_frame: 0, end_frame: durationFrames, z_order: 0, fit: 'cover' }];
  const subtitles: Array<{ text: string; start_frame: number; end_frame: number; word_cues?: WordCue[] }> = [];
  const audioEvents: Array<Record<string, unknown>> = [{ row_id: 'narration', media_id: 'narration', role: 'narration', start_frame: 0, end_frame: durationFrames, volume: 1 }];
  for (const track of audioTracks) {
    if (track.media.media_kind !== 'audio') throw new Error(`${track.role} 必须是音频文件`);
    const mediaId = `${track.role}-${track.id}`;
    bindings.push({ media_id: mediaId, kind: 'audio', project_path: track.media.media_path, media_hash: `sha256:${track.media.media_hash}`, ...(track.role === 'bgm' && track.media.duration_us < narration.duration_us ? { loop: true } : {}) });
    if (track.role === 'bgm') audioEvents.push({ row_id: `bgm:${track.id}`, media_id: mediaId, role: 'bgm', start_frame: 0, end_frame: durationFrames, volume: 0.3, duck_under_narration: 0.35 });
  }
  for (const event of timing.events) {
    if (!Number.isInteger(event.start_frame) || !Number.isInteger(event.end_frame) || event.start_frame < 0 || event.end_frame <= event.start_frame || event.end_frame > durationFrames) throw new Error(`事件 ${event.event_id} 超出素材时长`);
    const rule = script.events.find((item) => item.event_id === event.event_id);
    if (!rule) throw new Error(`事件 ${event.event_id} 缺少脚本规则`);
    const selection = script.selections?.find((item) => item.selection_id === rule.selection_id);
    const caption = rule.caption || (selection ? Array.from(script.dialogue).slice(selection.start_char, selection.end_char).join('') : '');
    if (caption.trim()) {
      const wordCues = subtitleWordCues(selection, caption.trim(), script, wordTimeline, event.start_frame, event.end_frame, fps);
      subtitles.push({ text: caption.trim(), start_frame: event.start_frame, end_frame: event.end_frame, ...(wordCues.length ? { word_cues: wordCues } : {}) });
    }
    if (rule.visual_node_id) {
      const media = visuals[rule.visual_node_id];
      if (!media || (media.media_kind !== 'image' && media.media_kind !== 'video')) throw new Error(`事件 ${event.event_id} 缺少替换画面`);
      if (!rule.visual_media_revision || media.media_hash !== rule.visual_media_revision) throw new Error(`事件 ${event.event_id} 的替换素材已变化，请重新选择`);
      const mediaId = `visual-${rule.visual_node_id}`;
      if (!bindings.some((binding) => binding.media_id === mediaId)) bindings.push({ media_id: mediaId, kind: media.media_kind, project_path: media.media_path, media_hash: `sha256:${media.media_hash}` });
      const fadeFrames = Math.min(4, Math.floor((event.end_frame - event.start_frame) / 2));
      layers.push({ row_id: `visual:${event.event_id}`, media_id: mediaId, start_frame: event.start_frame, end_frame: event.end_frame, z_order: 1, fit: 'cover', fade_in_frames: fadeFrames, fade_out_frames: fadeFrames, zoom: { start: 1, end: 1.04 } });
    }
    if (!caption.trim() && !rule.visual_node_id) throw new Error(`事件 ${event.event_id} 没有字幕或画面输出`);
    if (rule.sfx_node_id) {
      const sfx = audioTracks.find((track) => track.id === rule.sfx_node_id && track.role === 'sfx');
      if (!sfx) throw new Error(`事件 ${event.event_id} 缺少音效素材`);
      const length = Math.floor(sfx.media.duration_us * fps / 1_000_000);
      if (length < 1) throw new Error(`事件 ${event.event_id} 的音效时长不足一帧`);
      audioEvents.push({ row_id: `sfx:${event.event_id}:${sfx.id}`, media_id: `sfx-${sfx.id}`, role: 'sfx', start_frame: event.start_frame, end_frame: Math.min(event.end_frame, event.start_frame + length), volume: 1 });
    }
  }
  return {
    manifest_version: 2,
    output_spec: { width: 1080, height: 1920, fps, duration_frames: durationFrames, subtitle_safe_area: { left: 90, right: 90, top: 120, bottom: 180 } },
    media_bindings: bindings,
    layers,
    subtitle_events: subtitles,
    audio_events: audioEvents,
    manual_overrides: [],
    source_versions: [],
  };
}
