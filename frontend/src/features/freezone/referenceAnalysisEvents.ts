import type { ReferenceAnalysisRun } from '@/api/referenceAnalysisRuns';
import type { CanvasEdge, CanvasNode } from '@/stores/canvasStore';
import { normalizeCanvasData } from '@/stores/canvasStore';
import type { FreezoneCanvasBaseline } from './canvasSyncRuntime';

export const REFERENCE_ANALYSIS_STATUS_EVENT = 'freezone-reference-analysis-status';

export function referenceAnalysisRunMatchesScope(run: ReferenceAnalysisRun, projectId: string | null, canvasId: string | null, agentId: string | null): boolean {
  return run.project_id === projectId && run.canvas_id === canvasId && run.agent_id === (agentId?.trim() || 'main');
}
export function shouldRefreshReferenceAnalysis(run: ReferenceAnalysisRun): boolean { return Boolean(run.analysis_node_id); }
const VIEW_ONLY = new Set(['selected', 'dragging', 'resizing', 'measured']);
function same(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) return true;
  if (!left || !right || typeof left !== 'object' || typeof right !== 'object') return false;
  if (Array.isArray(left) || Array.isArray(right)) return Array.isArray(left) && Array.isArray(right) && left.length === right.length && left.every((value, index) => same(value, right[index]));
  const a = left as Record<string, unknown>, b = right as Record<string, unknown>;
  const keys = Object.keys(a);
  return keys.length === Object.keys(b).length && keys.every((key) => Object.prototype.hasOwnProperty.call(b, key) && same(a[key], b[key]));
}
function sameItem(left: Record<string, unknown>, right: Record<string, unknown>): boolean {
  const clean = (item: Record<string, unknown>) => Object.fromEntries(Object.entries(item).filter(([key]) => !VIEW_ONLY.has(key)));
  return same(clean(left), clean(right));
}
function conflict(): never { throw new Error('画布的同一内容被同时修改，分析结果已保留。请先处理画布保存冲突，再刷新状态。'); }
function mergeFields(base: Record<string, unknown>, local: Record<string, unknown>, remote: Record<string, unknown>, data = false): Record<string, unknown> {
  const merged: Record<string, unknown> = {};
  for (const key of new Set([...Object.keys(base), ...Object.keys(local), ...Object.keys(remote)])) {
    if (!data && VIEW_ONLY.has(key)) {
      if (Object.prototype.hasOwnProperty.call(local, key)) merged[key] = local[key];
      continue;
    }
    if (key === 'data' && !data) {
      merged.data = mergeFields((base.data ?? {}) as Record<string, unknown>, (local.data ?? {}) as Record<string, unknown>, (remote.data ?? {}) as Record<string, unknown>, true);
      continue;
    }
    const localChanged = !same(local[key], base[key]);
    const remoteChanged = !same(remote[key], base[key]);
    if (localChanged && remoteChanged && !same(local[key], remote[key])) conflict();
    const chosen = localChanged ? local : remote;
    if (Object.prototype.hasOwnProperty.call(chosen, key)) merged[key] = chosen[key];
  }
  return merged;
}
function mergeItems<T extends { id: string }>(base: T[], local: T[], remote: T[]): T[] {
  const baseById = new Map(base.map((item) => [item.id, item]));
  const localById = new Map(local.map((item) => [item.id, item]));
  const remoteById = new Map(remote.map((item) => [item.id, item]));
  const merged: T[] = [];
  for (const id of new Set([...localById.keys(), ...remoteById.keys(), ...baseById.keys()])) {
    const before = baseById.get(id), here = localById.get(id), there = remoteById.get(id);
    if (!before) {
      if (here && there && !sameItem(here as Record<string, unknown>, there as Record<string, unknown>)) conflict();
      if (here || there) merged.push((here ?? there)!);
    } else if (!here || !there) {
      const survivor = here ?? there;
      if (survivor && !sameItem(survivor as Record<string, unknown>, before as Record<string, unknown>)) conflict();
    } else merged.push(mergeFields(before as Record<string, unknown>, here as Record<string, unknown>, there as Record<string, unknown>) as T);
  }
  return merged;
}

/** The registered save baseline makes local edits distinguishable from server
 * results. Conflicts must be checked before canvasRuntime reanchors autosave. */
export function mergeReferenceAnalysisCanvas(remoteNodes: CanvasNode[], remoteEdges: CanvasEdge[], localNodes: CanvasNode[], localEdges: CanvasEdge[], _run: ReferenceAnalysisRun, baseline: FreezoneCanvasBaseline) {
  return mergeConcurrentCanvas(remoteNodes, remoteEdges, localNodes, localEdges, baseline);
}

export function mergeConcurrentCanvas(remoteNodes: CanvasNode[], remoteEdges: CanvasEdge[], localNodes: CanvasNode[], localEdges: CanvasEdge[], baseline: FreezoneCanvasBaseline) {
  const base = normalizeCanvasData(baseline.nodes, baseline.edges);
  const local = normalizeCanvasData(localNodes, localEdges);
  const remote = normalizeCanvasData(remoteNodes, remoteEdges);
  const nodes = mergeItems(base.nodes, local.nodes, remote.nodes);
  const nodeIds = new Set(nodes.map((node) => node.id));
  const edges = mergeItems(base.edges, local.edges, remote.edges).filter((edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target));
  return { nodes, edges };
}

export function mergeReferenceAnalysisMetadata(base: Record<string, unknown> | null, local: Record<string, unknown> | null, remote: Record<string, unknown> | null): Record<string, unknown> {
  return mergeFields(base ?? {}, local ?? {}, remote ?? {}, true);
}
