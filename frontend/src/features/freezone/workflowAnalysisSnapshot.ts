import { CANVAS_NODE_TYPES, type CanvasNode } from "@/features/canvas/domain/canvasNodes";

function sameJsonValue(left: unknown, right: unknown): boolean {
  if (left === right) return true;
  if (Array.isArray(left) && Array.isArray(right)) return left.length === right.length && left.every((value, index) => sameJsonValue(value, right[index]));
  if (!left || !right || typeof left !== "object" || typeof right !== "object" || Array.isArray(left) || Array.isArray(right)) return false;
  const l = left as Record<string, unknown>;
  const r = right as Record<string, unknown>;
  return Object.keys(l).length === Object.keys(r).length && Object.keys(l).every((key) => Object.prototype.hasOwnProperty.call(r, key) && sameJsonValue(l[key], r[key]));
}

export function assertWorkflowAnalysisSnapshot(source: CanvasNode | undefined, expected: Record<string, unknown>, sourceId: string): void {
  const data = source?.data;
  const matches = source?.type === CANVAS_NODE_TYPES.videoStory && data && !data.isAnalyzing && !data.analysisError
    && ["rawResult", "referenceBlueprintRef", "sourceVideoUrl"].every((key) => sameJsonValue(data[key] ?? null, expected[key] ?? null))
    && Boolean(data.blueprintDirty) === Boolean(expected.blueprintDirty);
  if (!matches) throw new Error(`workflow source analysis changed: ${sourceId}`);
}

