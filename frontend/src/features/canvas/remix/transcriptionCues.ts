import type { RemixCaptionCue } from './workbenchModel';

interface TimedWord {
  text: string;
  start_char?: number;
  end_char?: number;
  start_us: number;
  end_us: number;
}

export interface WordTimelinePayload {
  status: string;
  transcript?: string | null;
  words: TimedWord[];
}

/** Turn word-level timing into short, editable subtitle lines. */
export function cuesFromWordTimeline(timeline: WordTimelinePayload, durationUs: number): RemixCaptionCue[] {
  if (timeline.status !== 'completed' || !Array.isArray(timeline.words)) return [];
  const renderDurationUs = Math.min(durationUs, 60_000_000);
  const result: RemixCaptionCue[] = [];
  let group: TimedWord[] = [];
  const finish = (finalGroup = false) => {
    if (!group.length) return;
    const source = timeline.transcript;
    const first = group[0];
    const last = group[group.length - 1];
    const validSpans = typeof source === 'string' && Number.isInteger(first.start_char) && Number.isInteger(last.end_char)
      && first.start_char! >= 0 && last.end_char! <= source.length
      && group.every((word) => Number.isInteger(word.start_char) && Number.isInteger(word.end_char)
        && source.slice(word.start_char!, word.end_char!) === word.text);
    let text = validSpans ? source.slice(first.start_char!, last.end_char!) : group.map((word) => word.text).join('');
    if (validSpans && finalGroup) {
      const trailing = source.slice(last.end_char!);
      if (/^[\p{P}\s]+$/u.test(trailing)) text += trailing;
    }
    text = text.trim();
    const startSeconds = Math.round(group[0].start_us / 1_000) / 1_000;
    const lastRenderableFrame = Math.floor(renderDurationUs * 30 / 1_000_000);
    const endSeconds = Math.round(Math.min(last.end_us / 1_000_000, lastRenderableFrame / 30) * 1_000) / 1_000;
    if (text && endSeconds > startSeconds) result.push({ id: crypto.randomUUID(), label: '', text, startSeconds, endSeconds });
    group = [];
  };
  for (const word of timeline.words) {
    if (typeof word.text !== 'string' || !Number.isFinite(word.start_us) || !Number.isFinite(word.end_us)
      || word.start_us < 0 || word.end_us <= word.start_us || word.start_us >= renderDurationUs) continue;
    const previous = group[group.length - 1];
    if (previous && (word.start_us - previous.end_us > 700_000
      || word.end_us - group[0].start_us > 4_500_000
      || group.map((item) => item.text).join('').length + word.text.length > 16)) finish();
    group.push(word);
  }
  finish(true);
  return result;
}
