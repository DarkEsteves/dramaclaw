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
}

export function timelineWords(payload: TranscriptTimeline): TranscriptWord[] {
  if (payload.status !== 'completed' || !Array.isArray(payload.words)) return [];
  return payload.words.filter((word) => typeof word.text === 'string' && Number.isFinite(word.start_us) && Number.isFinite(word.end_us))
    .map((word) => ({ text: word.text, startUs: word.start_us, endUs: word.end_us, confidence: typeof word.confidence === 'number' ? word.confidence : null }));
}

/** Keep word timing but regenerate character offsets for a corrected transcript. */
export function correctedTimeline(payload: TranscriptTimeline, correctedWords: string[]): TranscriptTimeline {
  if (payload.status !== 'completed' || correctedWords.length !== payload.words.length) throw new Error('转写内容已变化，请重新加载');
  let offset = 0;
  const words = payload.words.map((word, index) => {
    const text = correctedWords[index]?.trim();
    if (!text) throw new Error('字词不能为空');
    const start = offset;
    offset += text.length;
    return { ...word, text, start_char: start, end_char: offset };
  });
  return { ...payload, transcript: words.map((word) => word.text).join(''), words };
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
