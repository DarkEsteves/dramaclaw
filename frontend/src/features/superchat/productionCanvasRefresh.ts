import { getFreezoneCanvas } from '@/api/canvas';
import { captureFreezoneCanvasScope, getFreezoneCanvasBaseline, applyRemoteFreezoneCanvas } from '@/features/freezone/canvasSyncRuntime';
import { mergeReferenceAnalysisCanvas, mergeReferenceAnalysisMetadata } from '@/features/freezone/referenceAnalysisEvents';
import type { ReferenceAnalysisRun } from '@/api/referenceAnalysisRuns';
import { normalizeCanvasData, useCanvasStore, type CanvasNode } from '@/stores/canvasStore';

export async function refreshProductionCanvas(projectId: string, canvasId: string, isCurrent: () => boolean = () => true): Promise<void> {
  const current = captureFreezoneCanvasScope(projectId, canvasId);
  if (!current() || !isCurrent()) return;
  const remote = await getFreezoneCanvas(projectId, canvasId);
  if (!current() || !isCurrent()) return;
  const baseline = getFreezoneCanvasBaseline(projectId, canvasId);
  if (!baseline) throw new Error('画布尚未完成载入，请重新打开画布查看制作结果。');
  if (typeof remote.revision === 'number' && baseline.revision !== null && baseline.revision > remote.revision) {
    throw new Error('画布刚完成新的保存，请刷新制作状态查看结果。');
  }
  const local = useCanvasStore.getState();
  const nodes = (remote.nodes ?? []) as CanvasNode[];
  const edges = (remote.edges ?? []) as typeof local.edges;
  const merged = mergeReferenceAnalysisCanvas(nodes, edges, local.nodes, local.edges, {} as ReferenceAnalysisRun, baseline);
  const metadata = baseline.localMetadata !== undefined
    ? mergeReferenceAnalysisMetadata(baseline.metadata ?? null, baseline.localMetadata, remote.metadata ?? null)
    : remote.metadata;
  if (!current() || !isCurrent()) return;
  const normalized = { ...remote, ...normalizeCanvasData(nodes, edges) };
  if (!applyRemoteFreezoneCanvas(projectId, canvasId, normalized,
    () => baseline.localMetadata !== undefined ? { ...merged, metadata } : merged)) {
    throw new Error('画布同步暂不可用，请重新打开画布查看制作结果。');
  }
}
