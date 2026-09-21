export interface TimelineEventOverride {
  offsetMs?: number;
  minimumDurationFrames?: number;
  startFrame?: number;
  endFrame?: number;
}

export interface TimelineEvent {
  selectionId: string;
  label: string;
  startFrame?: number;
  endFrame?: number;
  invalidReason?: string;
}

interface ExactRef { artifactId: string; revision: number }
interface OperationInputs { node_revision?: number; input_refs?: Array<{ artifact_id: string; revision: number }> }

export function operationMatchesTimelineInputs(operation: OperationInputs, scriptRef: ExactRef | null, wordRef: ExactRef | null, nodeRevision: number): boolean {
  if (!scriptRef || !wordRef || operation.node_revision !== nodeRevision || operation.input_refs?.length !== 2) return false;
  return [scriptRef, wordRef].every((ref) => operation.input_refs?.some((item) => item.artifact_id === ref.artifactId && item.revision === ref.revision));
}

type AlignedWord = { start_us?: unknown; end_us?: unknown };

export function buildTimelineEditMap(words: AlignedWord[], manual?: Record<string, number>[]): Record<string, number>[] {
  if (manual?.length) return manual.map((segment) => ({ ...segment }));
  const ends = words.map((word) => word.end_us).filter((value): value is number => Number.isInteger(value) && Number(value) > 0);
  const end = Math.max(0, ...ends);
  return end ? [{ source_start_us: 0, source_end_us: end, timeline_start_us: 0 }] : [];
}

export function buildTimelineOverrides(values: Record<string, TimelineEventOverride>): Record<string, unknown>[] {
  return Object.entries(values).flatMap(([eventId, value]) => {
    const rows: Record<string, unknown>[] = [];
    if (Number.isFinite(value.offsetMs) && value.offsetMs !== undefined && value.offsetMs !== 0) {
      rows.push({ event_id: eventId, relative_offset_us: Math.round(value.offsetMs * 1000) });
    }
    if (Number.isInteger(value.minimumDurationFrames) && Number(value.minimumDurationFrames) > 0) {
      rows.push({ event_id: eventId, kind: 'minimum_duration_frames', frames: value.minimumDurationFrames });
    }
    const hasStart = value.startFrame !== undefined;
    const hasEnd = value.endFrame !== undefined;
    if (hasStart !== hasEnd) {
      throw new Error(`${eventId} 的固定起始帧和固定结束帧必须同时填写`);
    }
    if (hasStart && (!Number.isInteger(value.startFrame) || !Number.isInteger(value.endFrame))) {
      throw new Error(`${eventId} 的固定帧必须是整数`);
    }
    if (hasStart && (Number(value.startFrame) < 0 || Number(value.endFrame) <= Number(value.startFrame))) {
      throw new Error(`${eventId} 的固定结束帧必须晚于固定起始帧`);
    }
    if (hasStart && hasEnd) {
      rows.push({ event_id: eventId, kind: 'fixed_frames', start_frame: value.startFrame, end_frame: value.endFrame });
    }
    return rows;
  });
}

export function readTimingEvents(payload: Record<string, unknown>): TimelineEvent[] {
  const events = Array.isArray(payload.events) ? payload.events : [];
  const unresolved = Array.isArray(payload.unresolved_required_events) ? payload.unresolved_required_events : [];
  const conflicts = Array.isArray(payload.override_conflicts) ? payload.override_conflicts : [];
  const conflictById = new Map(conflicts.filter((item) => item && typeof item === 'object').map((item) => [String(item.event_id), String(item.reason)]));
  return [
    ...events.filter((item) => item && typeof item === 'object').map((item) => ({
      selectionId: String(item.event_id), label: String(item.event_id), startFrame: Number(item.start_frame), endFrame: Number(item.end_frame),
      ...(conflictById.has(String(item.event_id)) ? { invalidReason: conflictById.get(String(item.event_id)) } : {}),
    })),
    ...unresolved.filter((item) => item && typeof item === 'object').map((item) => ({ selectionId: String(item.event_id), label: String(item.event_id), invalidReason: String(item.reason) })),
  ];
}
