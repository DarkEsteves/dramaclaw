export interface TimelineEventOverride {
  offsetMs?: number;
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

type AlignedWord = { start_us?: unknown; end_us?: unknown };

export function buildTimelineEditMap(words: AlignedWord[]): Record<string, number>[] {
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
    if (Number.isInteger(value.startFrame) && Number.isInteger(value.endFrame)) {
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
