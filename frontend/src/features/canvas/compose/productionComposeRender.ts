import {
  compileProductionPlan, createRemixArtifact, projectProductionPlan, renderRemixManifest,
  type ProductionPlanProjection, type RemixArtifactRef, type RemixRenderTaskRef,
} from '@/api/remix';
import { useCanvasStore } from '@/stores/canvasStore';
import { flushProductionPlanV2AutoSave } from '@/features/canvas/remix/productionPlanV2AutoSave';
import { planMediaCandidates } from '@/features/canvas/remix/productionPlanModel';
import type { ProductionPlanV2 } from '@/features/canvas/remix/productionPlanV2Model';
import { remixGraphFingerprint } from '@/features/canvas/remix/prepareNodeRemix';
import { captureFreezoneCanvasScope, flushFreezoneCanvasRuntime } from '@/features/freezone/canvasSyncRuntime';
import { canvasBindingIssues } from './productionComposeModel';

interface RenderInput { project: string; canvasId: string; planNodeId: string; composeNodeId: string }
interface RenderResult { saved: RemixArtifactRef; projection: ProductionPlanProjection; task: RemixRenderTaskRef }
const refKey = (ref: RemixArtifactRef | null | undefined) => ref ? `${ref.artifactId}:${ref.revision}` : '';

/** Shared manual and Agent export path. All async boundaries retain an exact canvas/source snapshot. */
export async function submitProductionComposeRender({ project, canvasId, planNodeId, composeNodeId }: RenderInput): Promise<RenderResult> {
  const isCurrentScope = captureFreezoneCanvasScope(project, canvasId);
  const assertScope = () => { if (!isCurrentScope()) throw new Error('所属画布已切换，未修改当前画布，请从原项目任务记录恢复'); };
  assertScope();
  const saved = await flushProductionPlanV2AutoSave(project, planNodeId, isCurrentScope);
  assertScope();
  if (!saved) throw new Error('作品方案尚未保存');
  const initial = useCanvasStore.getState();
  const planNode = initial.nodes.find((node) => node.id === planNodeId);
  const composeNode = initial.nodes.find((node) => node.id === composeNodeId);
  const draft = planNode?.data.productionPlanV2Draft as ProductionPlanV2 | undefined;
  if (!draft || planNode?.data.productionPlanV2Dirty || planNode?.data.productionPlanV2Conflict
    || refKey(planNode?.data.productionPlanV2Ref as RemixArtifactRef | undefined) !== refKey(saved)) {
    throw new Error('作品方案在保存期间发生变化，请重新导出');
  }
  if (!composeNode || composeNode.data.productionPlanNodeId !== planNodeId
    || !initial.edges.some((edge) => edge.source === planNodeId && edge.target === composeNodeId)) {
    throw new Error('作品方案与视频合成节点没有正确连接');
  }
  const draftSnapshot = JSON.stringify(draft);
  const fingerprint = remixGraphFingerprint(initial.nodes, initial.edges, composeNodeId);
  const originalComposeRevision = Number(composeNode.data.remixNodeRevision ?? 0);
  const assertCurrent = (expectedComposeRevision = originalComposeRevision) => {
    assertScope();
    const state = useCanvasStore.getState();
    const currentPlan = state.nodes.find((node) => node.id === planNodeId)?.data;
    const currentCompose = state.nodes.find((node) => node.id === composeNodeId)?.data;
    if (!currentPlan || !currentCompose || currentPlan.productionPlanV2Dirty || currentPlan.productionPlanV2Conflict
      || refKey(currentPlan.productionPlanV2Ref as RemixArtifactRef | undefined) !== refKey(saved)
      || JSON.stringify(currentPlan.productionPlanV2Draft) !== draftSnapshot
      || remixGraphFingerprint(state.nodes, state.edges, composeNodeId) !== fingerprint
      || currentCompose.productionPlanNodeId !== planNodeId
      || Number(currentCompose.remixNodeRevision ?? 0) !== expectedComposeRevision
      || !state.edges.some((edge) => edge.source === planNodeId && edge.target === composeNodeId)) {
      throw new Error('作品方案或关联素材在导出期间发生变化，请重新导出');
    }
    return state;
  };

  const projection = await projectProductionPlan(project, saved);
  const state = assertCurrent();
  const graphIssues = canvasBindingIssues(draft, state.edges, planNodeId, planMediaCandidates(project, state.nodes, planNodeId));
  if (!projection.ready || graphIssues.length) {
    throw new Error(`有 ${projection.issues.length + graphIssues.length} 项需要处理，点击时间线中的缺项可定位`);
  }
  const manifest = await compileProductionPlan(project, saved);
  assertCurrent();
  const artifact = await createRemixArtifact(project, 'composition_manifest.v2', manifest, [saved]);
  const beforeBind = assertCurrent();
  const manifestRef = { artifactId: artifact.id, revision: artifact.revision };
  const revision = originalComposeRevision + 1;
  const oldVideo = beforeBind.nodes.find((node) => node.id === composeNodeId)?.data.resultVideoUrl;
  beforeBind.updateNodeData(composeNodeId, {
    productionPlanV2Ref: saved, remixManifestRef: manifestRef, remixManifestLoadedRef: null, remixManifestLoadState: 'loading',
    remixInputFingerprint: fingerprint, remixInputsCurrent: true, remixNodeRevision: revision,
    productionPlanCompiledRef: saved, productionPlanRenderingRef: saved, remixOperationId: null,
    ...(oldVideo ? { remixPreviousVideoUrl: oldVideo, resultVideoUrl: null } : {}),
  });
  if (await flushFreezoneCanvasRuntime(project, canvasId) !== true) throw new Error('画布尚未保存，请稍后重试');
  assertCurrent(revision);
  const currentCompose = useCanvasStore.getState().nodes.find((node) => node.id === composeNodeId)?.data;
  if (refKey(currentCompose?.remixManifestRef as RemixArtifactRef | undefined) !== refKey(manifestRef)) {
    throw new Error('视频合成节点在导出期间发生变化，请重新导出');
  }
  const task = await renderRemixManifest(project, {
    manifestRef, idempotencyKey: globalThis.crypto?.randomUUID?.() ?? `production-${Date.now()}`,
    nodeRevision: revision, canvasId, nodeId: composeNodeId, inputFingerprint: fingerprint,
  });
  assertCurrent(revision);
  const latest = useCanvasStore.getState().nodes.find((node) => node.id === composeNodeId)?.data;
  if (refKey(latest?.remixManifestRef as RemixArtifactRef | undefined) !== refKey(manifestRef)) {
    throw new Error('任务已提交，但视频合成节点已变化，请从任务记录恢复');
  }
  useCanvasStore.getState().updateNodeData(composeNodeId, { remixOperationId: task.operation_id, productionPlanRenderingRef: saved });
  if (await flushFreezoneCanvasRuntime(project, canvasId) !== true) throw new Error('任务已提交；画布保存失败，请从任务记录恢复');
  assertCurrent(revision);
  return { saved, projection, task };
}
