import type { PlanBeat } from './productionPlanModel';

export interface VoiceTimingHint {
  sourceSeconds: number;
  plannedSeconds: number;
  differs: boolean;
}

function singleFullBeatVoice(beat: PlanBeat) {
  const voices = beat.audio.filter((cue) => cue.role === 'speech' || cue.role === 'narration');
  if (voices.length !== 1) return null;
  const cue = voices[0];
  if (!cue.media?.duration_us || cue.start_frame != null && cue.start_frame !== 0
    || cue.end_frame != null && cue.end_frame !== beat.duration_frames) return null;
  return cue;
}

export function voiceTimingHint(beat: PlanBeat, fps: number): VoiceTimingHint | null {
  const voice = singleFullBeatVoice(beat);
  if (!voice || fps <= 0) return null;
  const sourceSeconds = voice.media!.duration_us / 1_000_000;
  const plannedSeconds = beat.duration_frames / fps;
  return { sourceSeconds, plannedSeconds, differs: Math.abs(sourceSeconds - plannedSeconds) > 1 / fps };
}

/** A full-beat speech source can set the beat clock; other layouts need human timing. */
export function fitBeatToSingleVoice(beat: PlanBeat, fps: number): PlanBeat | null {
  const hint = voiceTimingHint(beat, fps);
  if (!hint) return null;
  const nextFrames = Math.max(1, Math.round(hint.sourceSeconds * fps));
  return retimeBeat(beat, nextFrames);
}

export function retimeBeat(beat: PlanBeat, nextFrames: number): PlanBeat {
  if (!Number.isInteger(nextFrames) || nextFrames <= 0) throw new Error('段落时长必须大于零');
  const oldFrames = beat.duration_frames;
  const scale = (frame: number) => Math.min(nextFrames, Math.max(0, Math.round(frame * nextFrames / oldFrames)));
  const next = structuredClone(beat);
  next.duration_frames = nextFrames;
  for (const item of [...next.turns, ...next.visuals, ...next.audio]) {
    if (item.start_frame != null) item.start_frame = scale(item.start_frame);
    if (item.end_frame != null) item.end_frame = scale(item.end_frame);
  }
  return next;
}
