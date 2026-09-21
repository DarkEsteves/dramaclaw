import type { TranscriptWord } from '@/features/canvas/domain/canvasNodes';
import type { RemixArtifactRef } from '@/api/remix';

export interface TranscriptSource {
  path: string;
  url?: string;
  kind: 'audio' | 'video';
}

type SourceNode = { id: string; data: Record<string, unknown> };
type SourceEdge = { source: string; target: string };

/** Only project-local remix media can be sent to the transcription worker. */
export function sourceMediaForTranscript(nodeId: string, nodes: SourceNode[] = [], edges: SourceEdge[] = []): TranscriptSource | null {
  const sources = new Set(edges.filter((edge) => edge.target === nodeId).map((edge) => edge.source));
  const connected = nodes.filter((node) => sources.has(node.id)).map((node) => node.data.remixMedia).filter((value): value is Record<string, unknown> => Boolean(value && typeof value === 'object'));
  const media = connected.find((item) => item.media_kind === 'audio' && typeof item.media_path === 'string')
    ?? connected.find((item) => item.media_kind === 'video' && item.has_audio === true && typeof item.media_path === 'string');
  if (!media) return null;
  return { path: media.media_path as string, url: typeof media.url === 'string' ? media.url : undefined, kind: media.media_kind as 'audio' | 'video' };
}

export function sourceScriptForTranscript(nodeId: string, nodes: SourceNode[] = [], edges: SourceEdge[] = []): RemixArtifactRef | null {
  const sources = new Set(edges.filter((edge) => edge.target === nodeId).map((edge) => edge.source));
  for (const node of nodes) {
    if (!sources.has(node.id)) continue;
    const ref = node.data.remixScriptRef;
    if (ref && typeof ref === 'object' && 'artifactId' in ref && 'revision' in ref
      && typeof ref.artifactId === 'string' && Number.isInteger(ref.revision) && (ref.revision as number) > 0) {
      return { artifactId: ref.artifactId, revision: ref.revision as number };
    }
  }
  return null;
}

interface TimelineWord extends Record<string, unknown> {
  text: string;
  start_us: number;
  end_us: number;
}

export interface TranscriptTimeline extends Record<string, unknown> {
  status: string;
  words: TimelineWord[];
  transcript?: string | null;
  mismatch?: SpeechMismatch | null;
  match_report?: SpeechMatchReport | null;
}

interface SpeechTextRange { text?: unknown; start_char?: unknown; end_char?: unknown }
interface SpeechDifference { script_range?: SpeechTextRange; asr_range?: SpeechTextRange }
export interface SpeechMatchReport {
  substitutions?: unknown;
  insertions?: unknown;
  omissions?: unknown;
  character_coverage?: unknown;
}
export interface SpeechMismatch {
  reason?: unknown;
  report?: SpeechMatchReport | null;
  untimed_script_ranges?: unknown;
}

function differences(value: unknown): SpeechDifference[] {
  return Array.isArray(value) ? value.filter((item): item is SpeechDifference => Boolean(item && typeof item === 'object')) : [];
}

function rangeText(value: SpeechTextRange | undefined): string {
  return typeof value?.text === 'string' ? value.text : '';
}

/** Turn backend comparison evidence into compact review rows without trusting ASR as truth. */
export function speechReviewItems(mismatch: SpeechMismatch | null | undefined, successfulReport?: SpeechMatchReport | null): string[] {
  const report = mismatch?.report ?? successfulReport;
  const untimed = Array.isArray(mismatch?.untimed_script_ranges)
    ? mismatch.untimed_script_ranges.filter((item): item is SpeechTextRange => Boolean(item && typeof item === 'object')) : [];
  return [
    ...differences(report?.substitutions).map((item) => `可能替换：${rangeText(item.script_range)} → ${rangeText(item.asr_range)}（ASR 低置信度）`),
    ...differences(report?.omissions).map((item) => `缺少：${rangeText(item.script_range)}`),
    ...differences(report?.insertions).map((item) => `可能多读：${rangeText(item.asr_range)}（ASR 低置信度）`),
    ...untimed.map((item) => `未对齐：${rangeText(item)}（脚本字符 ${String(item.start_char)}–${String(item.end_char)}）`),
  ];
}

export class RealignmentRequiredError extends Error {
  constructor() { super('修改可能改变发音或词边界，需要重新对齐'); this.name = 'RealignmentRequiredError'; }
}

export function timelineWords(payload: TranscriptTimeline): TranscriptWord[] {
  if (payload.status !== 'completed' || !Array.isArray(payload.words)) return [];
  return payload.words.filter((word) => typeof word.text === 'string' && Number.isFinite(word.start_us) && Number.isFinite(word.end_us))
    .map((word) => ({ text: word.text, startUs: word.start_us, endUs: word.end_us,
      startChar: Number.isInteger(word.start_char) ? Number(word.start_char) : undefined,
      endChar: Number.isInteger(word.end_char) ? Number(word.end_char) : undefined,
      confidence: typeof word.confidence === 'number' ? word.confidence : null }));
}

/** Keep the transcript's punctuation and spaces while making matched words seekable. */
export function transcriptPlaybackParts(text: string, words: TranscriptWord[]): Array<{ text: string; startUs?: number; endUs?: number }> {
  const parts: Array<{ text: string; startUs?: number; endUs?: number }> = [];
  const utf16Offsets = [0];
  for (const character of text) utf16Offsets.push(utf16Offsets[utf16Offsets.length - 1] + character.length);
  let cursor = 0;
  for (const word of words) {
    const spanStart = word.startChar === undefined ? undefined : utf16Offsets[word.startChar];
    const spanEnd = word.endChar === undefined ? undefined : utf16Offsets[word.endChar];
    let position = spanStart !== undefined && spanEnd !== undefined
      && text.slice(spanStart, spanEnd) === word.text ? spanStart : -1;
    if (position < cursor) position = -1;
    if (position < 0) {
      position = text.indexOf(word.text, cursor);
      while (position >= 0 && ((/^[A-Za-z0-9]/u.test(word.text) && /[A-Za-z0-9]/u.test(text[position - 1] ?? ''))
        || (/[A-Za-z0-9]$/u.test(word.text) && /[A-Za-z0-9]/u.test(text[position + word.text.length] ?? '')))) {
        position = text.indexOf(word.text, position + 1);
      }
    }
    if (position < 0) return [{ text }];
    if (position > cursor) parts.push({ text: text.slice(cursor, position) });
    parts.push({ text: word.text, startUs: word.startUs, endUs: word.endUs });
    cursor = position + word.text.length;
  }
  if (cursor < text.length) parts.push({ text: text.slice(cursor) });
  return parts;
}

/** Keep word timing but regenerate character offsets for a corrected transcript. */
export function correctedTimeline(payload: TranscriptTimeline, correctedWords: string[], confirmedSameSpeechAndBoundaries: boolean): TranscriptTimeline {
  if (payload.status !== 'completed' || correctedWords.length !== payload.words.length) throw new Error('转写内容已变化，请重新加载');
  const source = typeof payload.transcript === 'string' ? payload.transcript : payload.words.map((word) => word.text).join('');
  let sourceOffset = 0;
  let revisedText = '';
  const words = payload.words.map((word, index) => {
    const text = correctedWords[index]?.trim();
    if (!text) throw new Error('字词不能为空');
    const changed = text !== word.text;
    const sameLength = Array.from(text).length === Array.from(word.text).length;
    const singleLatinWordCorrection = /^[A-Za-z]+[.,!?]?$/u.test(text)
      && /^[A-Za-z]+[.,!?]?$/u.test(word.text)
      && (text.match(/[.,!?]$/u)?.[0] ?? '') === (word.text.match(/[.,!?]$/u)?.[0] ?? '');
    if (changed && (!confirmedSameSpeechAndBoundaries || (!sameLength && !singleLatinWordCorrection) || /\s/u.test(text))) {
      throw new RealignmentRequiredError();
    }
    const sourceStart = Number.isInteger(word.start_char) ? Number(word.start_char) : sourceOffset;
    const sourceEnd = Number.isInteger(word.end_char) ? Number(word.end_char) : sourceStart + word.text.length;
    if (sourceStart < sourceOffset || sourceEnd > source.length || source.slice(sourceStart, sourceEnd) !== word.text) throw new Error('转写内容已变化，请重新加载');
    revisedText += source.slice(sourceOffset, sourceStart);
    const start = revisedText.length;
    revisedText += text;
    sourceOffset = sourceEnd;
    return { ...word, text, start_char: start, end_char: revisedText.length, ...(changed ? { timing_status: 'manually_verified', original_text: word.text } : {}) };
  });
  revisedText += source.slice(sourceOffset);
  return { ...payload, transcript: revisedText, words, manual_timing_review: words.some((word) => word.timing_status === 'manually_verified') };
}

function srtTime(us: number): string {
  const ms = Math.max(0, Math.floor(us / 1_000));
  const hours = Math.floor(ms / 3_600_000);
  const minutes = Math.floor(ms / 60_000) % 60;
  const seconds = Math.floor(ms / 1_000) % 60;
  return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')},${String(ms % 1_000).padStart(3, '0')}`;
}

export function transcriptSrt(words: TranscriptWord[]): string {
  return words.map((word, index) => `${index + 1}\n${srtTime(word.startUs)} --> ${srtTime(word.endUs)}\n${word.text}\n`).join('\n');
}
