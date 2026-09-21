import type { RemixArtifactRef } from '@/api/remix';

type ManifestCollection = 'layers' | 'subtitle_events' | 'audio_events';
type ManifestRow = Record<string, unknown>;
export interface ManifestPayload { output_spec: { width: number; height: number; fps: number; duration_frames: number }; media_bindings: ManifestRow[]; layers: ManifestRow[]; subtitle_events: ManifestRow[]; audio_events: ManifestRow[]; manual_overrides: ManifestOverride[]; source_versions: ManifestRow[]; [key: string]: unknown }
export interface ManifestOverride { target: ManifestCollection; identity: Record<string, unknown>; field: string; base_value: unknown; value: unknown }
export interface ManifestEditorState { revision: number; previewRevision: number | null; unresolvedRequiredEvents?: string[]; conflicts?: string[] }
export function markPreviewStale(state: Pick<ManifestEditorState, 'revision' | 'previewRevision'>): Pick<ManifestEditorState, 'revision' | 'previewRevision'> { return { revision: state.revision, previewRevision: null }; }
export function canExportManifest(state: Pick<ManifestEditorState, 'unresolvedRequiredEvents' | 'conflicts'>): boolean { return !(state.unresolvedRequiredEvents?.length || state.conflicts?.length); }

function identityFor(collection: ManifestCollection, row: ManifestRow): Record<string, unknown> {
  if (collection === 'layers') return { media_id: row.media_id, z_order: row.z_order };
  if (collection === 'audio_events') return { media_id: row.media_id, role: row.role };
  return { start_frame: row.start_frame, end_frame: row.end_frame };
}
function clone<T>(value: T): T { return structuredClone(value); }
export function applyManifestEdit(payload: ManifestPayload, edit: { collection: ManifestCollection; index: number; field: string; value: unknown }): ManifestPayload {
  const next = clone(payload); const row = next[edit.collection][edit.index];
  if (!row) throw new Error('要编辑的清单项目不存在');
  const override: ManifestOverride = { target: edit.collection, identity: identityFor(edit.collection, row), field: edit.field, base_value: row[edit.field], value: edit.value };
  row[edit.field] = edit.value;
  const existing = next.manual_overrides.findIndex((item) => item.target === override.target && item.field === override.field && JSON.stringify(item.identity) === JSON.stringify(override.identity));
  if (existing >= 0) { override.base_value = next.manual_overrides[existing].base_value; next.manual_overrides[existing] = override; } else next.manual_overrides.push(override);
  return next;
}
export function reapplyManifestOverrides(compiled: ManifestPayload, overrides: ManifestOverride[]): { payload: ManifestPayload; conflicts: string[] } {
  const payload = clone(compiled); payload.manual_overrides = clone(overrides); const conflicts: string[] = [];
  for (const override of overrides) {
    const row = payload[override.target].find((item) => Object.entries(override.identity).every(([key, value]) => item[key] === value));
    if (!row || row[override.field] !== override.base_value) { conflicts.push(`${override.target}.${override.field} 的上游值已变化`); continue; }
    row[override.field] = override.value;
  }
  return { payload, conflicts };
}
export function projectManifestTimeline(payload: ManifestPayload) {
  const number = (value: unknown) => typeof value === 'number' ? value : 0;
  return { durationFrames: payload.output_spec.duration_frames, fps: payload.output_spec.fps,
    layers: payload.layers.map((row) => ({ mediaId: String(row.media_id ?? ''), startFrame: number(row.start_frame), endFrame: number(row.end_frame), zOrder: number(row.z_order) })),
    subtitles: payload.subtitle_events.map((row) => ({ text: String(row.text ?? ''), startFrame: number(row.start_frame), endFrame: number(row.end_frame) })),
    audio: payload.audio_events.map((row) => ({ mediaId: String(row.media_id ?? ''), role: String(row.role ?? ''), startFrame: number(row.start_frame), endFrame: number(row.end_frame), volume: typeof row.volume === 'number' ? row.volume : 1 })) };
}
export function previewStatus(current: RemixArtifactRef | null | undefined, preview: RemixArtifactRef | null | undefined, currentInputFingerprint: string | undefined, previewInputFingerprint: string | undefined): 'missing' | 'stale' | 'current' {
  if (!preview) return 'missing';
  return current?.artifactId === preview.artifactId && current.revision === preview.revision && currentInputFingerprint === previewInputFingerprint ? 'current' : 'stale';
}
function srtTime(frame: number, fps: number): string { const ms = Math.round(frame * 1000 / fps); return `${String(Math.floor(ms / 3_600_000)).padStart(2, '0')}:${String(Math.floor(ms / 60_000) % 60).padStart(2, '0')}:${String(Math.floor(ms / 1000) % 60).padStart(2, '0')},${String(ms % 1000).padStart(3, '0')}`; }
export function manifestToSrt(payload: ManifestPayload): string { const fps = payload.output_spec.fps; return payload.subtitle_events.map((row, index) => `${index + 1}\n${srtTime(Number(row.start_frame), fps)} --> ${srtTime(Number(row.end_frame), fps)}\n${String(row.text)}\n`).join('\n'); }
export function textDownloadUrl(text: string, mime: string): string { return `data:${mime};charset=utf-8,${encodeURIComponent(text)}`; }
