import { fetchRemixArtifact, type RemixArtifact, type RemixArtifactRef } from '@/api/remix';
import { getFreezoneCanvas, type FreezoneWorkflowRunAction } from '@/api/canvas';
import type { CanvasEdge, CanvasNode } from '@/features/canvas/domain/canvasNodes';
import { acceptPlanSpeechTimings } from './productionPlanV2Edits';
import { flushProductionPlanV2AutoSave } from './productionPlanV2AutoSave';
import { emptyProductionPlanV2, type PlanSpeechTimingV2, type ProductionPlanV2 } from './productionPlanV2Model';
import { identifyRecognizedWords } from './productionSpeechWordIdentity';
import { inspectProductionSpeechReadiness } from './productionSpeechReadiness';
import { storyAnalysisContext } from './productionPlanModel';
import { normalizeCanvasData, useCanvasStore } from '@/stores/canvasStore';
import { applyRemoteFreezoneCanvas, getFreezoneCanvasBaseline } from '@/features/freezone/canvasSyncRuntime';
import { mergeReferenceAnalysisCanvas } from '@/features/freezone/referenceAnalysisEvents';

export type ProductionProcessingAction = 'generate_production_plan_draft' | 'generate_production_plan_arrangement' | 'align_production_speech' | 'compile_production_plan';
const ACTIONS = new Set<unknown>(['generate_production_plan_draft', 'generate_production_plan_arrangement', 'align_production_speech', 'compile_production_plan']);
type JsonRecord = Record<string, unknown>;

export interface ProductionWorkflowDescriptor {
  action: ProductionProcessingAction;
  nodeId: string;
  nodeRevision: number;
  payload: JsonRecord;
  requestPayload: JsonRecord;
  planNodeId?: string;
  blocked?: string;
  snapshot: { nodeData: string; planData?: string; planRef?: RemixArtifactRef | null; analysisNodeId?: string; analysisData?: string };
}

function record(value: unknown): value is JsonRecord { return value !== null && typeof value === 'object' && !Array.isArray(value); }
function exactRef(value: unknown): RemixArtifactRef | null {
  if (!record(value)) return null;
  const artifactId = value.artifactId ?? value.artifact_id;
  return typeof artifactId === 'string' && artifactId.length > 0 && Number.isInteger(value.revision) && Number(value.revision) > 0
    ? { artifactId, revision: Number(value.revision) } : null;
}
function wireRef(ref: RemixArtifactRef): { artifact_id: string; revision: number } { return { artifact_id: ref.artifactId, revision: ref.revision }; }
function sameRef(a: RemixArtifactRef | null, b: RemixArtifactRef | null): boolean {
  return a === null && b === null || Boolean(a && b && a.artifactId === b.artifactId && a.revision === b.revision);
}
function sourcePlan(node: CanvasNode, nodes: CanvasNode[], edges: CanvasEdge[]): CanvasNode | null {
  const explicit = typeof node.data.productionPlanNodeId === 'string' ? node.data.productionPlanNodeId : '';
  const candidates = nodes.filter((item) => item.type === 'scriptNode' && item.data.remixRole === 'production_plan'
    && (item.id === explicit || edges.some((edge) => edge.source === item.id && edge.target === node.id)));
  return candidates.length === 1 ? candidates[0] : null;
}
function sourceAnalysis(node: CanvasNode, nodes: CanvasNode[], edges: CanvasEdge[]): CanvasNode[] {
  return nodes.filter((item) => item.type === 'videoStoryNode'
    && edges.some((edge) => edge.source === item.id && edge.target === node.id));
}
function analysisSignature(node: CanvasNode): string {
  return JSON.stringify({ rawResult: node.data.rawResult ?? null,
    blueprintRef: node.data.referenceBlueprintRef ?? null, blueprintDirty: node.data.blueprintDirty ?? false,
    sourceVideoUrl: node.data.sourceVideoUrl ?? null });
}
function groupedTurnIds(plan: ProductionPlanV2, leaderId: string, placementId: string, takeId: string): string[] {
  const take = takeId ? plan.speech_takes?.find((item) => item.id === takeId) : null;
  if (take) return take.turn_ids;
  const group = plan.beats.flatMap((beat) => beat.turns)
    .filter((turn) => turn.speech_placement_id === placementId
      || !turn.speech_placement_id && turn.speech_timing?.placement_id === placementId)
    .map((turn) => turn.id);
  return group.includes(leaderId) ? group : [leaderId];
}
function reviewBlocker(planNode: CanvasNode): boolean {
  const reviews = planNode.data.productionSpeechReviews;
  const plan = planNode.data.productionPlanV2Draft as ProductionPlanV2 | undefined;
  if (!record(reviews) || !plan || !Array.isArray(plan.beats)) return false;
  const unresolved = new Set(inspectProductionSpeechReadiness(plan, {}, {}).map((issue) => issue.turnId));
  return Object.entries(reviews).some(([turnId, review]) => {
    if (!record(review) || review.status === 'no_speech' || !unresolved.has(turnId)) return false;
    const beat = plan.beats.find((item) => item.turns.some((turn) => turn.id === turnId));
    const turn = beat?.turns.find((item) => item.id === turnId);
    if (!beat || !turn || review.text !== turn.text) return false;
    const take = plan.speech_takes?.find((item) => item.beat_id === beat.id && item.turn_ids.includes(turnId));
    const placementId = take?.origin_placement_id ?? turn.speech_placement_id ?? turn.speech_timing?.placement_id;
    if (!placementId || review.placementId !== placementId || review.takeId && review.takeId !== take?.id) return false;
    const placement = take ? undefined : plan.placements.find((item) => item.id === placementId);
    const resource = plan.resources.find((item) => item.id === (take?.resource_id ?? placement?.resource_id));
    return Boolean(resource && review.mediaHash === resource.media_hash
      && review.sourceInUs === 0 && review.sourceOutUs === resource.duration_us);
  });
}
function inputSignature(node: CanvasNode): string {
  return JSON.stringify({ action: node.data.processingAction, payload: node.data.processingPayload ?? null,
    revision: node.data.remixNodeRevision ?? 1, remixRole: node.data.remixRole ?? null,
    productionPlanNodeId: node.data.productionPlanNodeId ?? null });
}

function isEmptyPlanDraft(value: unknown): boolean {
  if (!record(value)) return false;
  return value.schema_version === 2
    && Array.isArray(value.beats) && value.beats.length === 0
    && Array.isArray(value.resources) && value.resources.length === 0
    && Array.isArray(value.placements) && value.placements.length === 0;
}

function pristineLatePlanInitialization(descriptor: ProductionWorkflowDescriptor, value: unknown): boolean {
  return descriptor.action === 'generate_production_plan_draft' && descriptor.snapshot.planData === undefined
    && !descriptor.snapshot.planRef && typeof descriptor.payload.topic === 'string'
    && JSON.stringify(value) === JSON.stringify(emptyProductionPlanV2(descriptor.payload.topic));
}

const INITIALIZATION_CONFLICT = '处理期间作品方案或素材已变化；结果已保留，请重新核对版本';

export function isProductionProcessingAction(value: unknown): value is ProductionProcessingAction { return ACTIONS.has(value); }

/** Resolve only saved current refs. A later stage must start in a new workflow run. */
export function buildProductionWorkflowAction(node: CanvasNode, nodes: CanvasNode[], edges: CanvasEdge[], override?: { action: ProductionProcessingAction; parameters?: JsonRecord }): ProductionWorkflowDescriptor | null {
  const action = override?.action ?? node.data.processingAction;
  if (!isProductionProcessingAction(action)) return null;
  const savedPayload = record(node.data.processingPayload) ? node.data.processingPayload : {};
  const parameters = structuredClone(override ? { ...(override.parameters ?? {}) } : savedPayload);
  if (action === 'generate_production_plan_draft' && override) {
    for (const field of ['requirements', 'locked_turns', 'requirements_fingerprint', 'requirements_provenance']) {
      if (parameters[field] === undefined && savedPayload[field] !== undefined) parameters[field] = structuredClone(savedPayload[field]);
    }
  }
  const nodeRevision = Number.isInteger(node.data.remixNodeRevision) && Number(node.data.remixNodeRevision) > 0 ? Number(node.data.remixNodeRevision) : 1;
  const planNode = action === 'generate_production_plan_draft' || action === 'generate_production_plan_arrangement' ? node : action === 'align_production_speech' ? node : sourcePlan(node, nodes, edges);
  const planRef = planNode ? exactRef(planNode.data.productionPlanV2Ref) : null;
  const descriptor: ProductionWorkflowDescriptor = { action, nodeId: node.id, nodeRevision, payload: parameters,
    requestPayload: structuredClone(parameters),
    ...(planNode ? { planNodeId: planNode.id } : {}), snapshot: { nodeData: inputSignature(node),
      ...(planNode ? { planData: JSON.stringify(planNode.data.productionPlanV2Draft), planRef } : {}) } };
  if (action === 'generate_production_plan_draft') {
    if (node.type !== 'scriptNode' || node.data.remixRole !== 'production_plan') descriptor.blocked = '作品方案处理动作必须绑定原生作品方案节点';
    else if (typeof parameters.topic !== 'string' || !parameters.topic.trim()) descriptor.blocked = '作品方案缺少创作目标';
    const analyses = sourceAnalysis(node, nodes, edges);
    if (analyses.length > 1) descriptor.blocked = '作品方案连接了多个原片分析，请明确选择来源';
    if (analyses.length === 1) {
      const analysis = analyses[0];
      descriptor.snapshot.analysisNodeId = analysis.id;
      descriptor.snapshot.analysisData = analysisSignature(analysis);
      if (!parameters.blueprint_ref && !parameters.analysis_context) {
        const blueprintRef = !analysis.data.blueprintDirty ? exactRef(analysis.data.referenceBlueprintRef) : null;
        if (blueprintRef) descriptor.payload.blueprint_ref = wireRef(blueprintRef);
        else {
          const context = storyAnalysisContext(analysis);
          if (context && (context.summary || context.adaptation_structure)) descriptor.payload.analysis_context = context;
        }
      }
    }
    if (node.data.productionPlanV2Dirty || node.data.productionPlanV2Conflict) descriptor.blocked = '作品方案有尚未保存的本地修改';
    else if (exactRef(node.data.productionPlanV2Ref) && !isEmptyPlanDraft(node.data.productionPlanV2Draft)
      && !hasCurrentProductionProcessingResult(node, action, nodes, edges)) descriptor.blocked = '作品方案已存在内容，请新建方案节点';
    else if (!descriptor.payload.blueprint_ref && !descriptor.payload.analysis_context) descriptor.blocked = '原片拆解尚未完成，请先运行原片分析再续跑作品方案';
    return descriptor;
  }
  if (!planNode || !planRef || planNode.data.productionPlanV2Dirty || planNode.data.productionPlanV2Conflict) {
    descriptor.blocked = '请先保存当前作品方案版本，再继续语音校准或合成';
    return descriptor;
  }
  const supplied = exactRef(parameters.plan_ref);
  if (parameters.plan_ref !== undefined && !sameRef(supplied, planRef)) {
    descriptor.blocked = '作品方案引用已变化，请读取当前版本后续跑';
    return descriptor;
  }
  descriptor.payload.plan_ref = wireRef(planRef);
  if (action === 'generate_production_plan_arrangement') {
    if (node.type !== 'scriptNode' || node.data.remixRole !== 'production_plan') descriptor.blocked = '画面与声音编排必须在作品方案节点执行';
    if (Object.keys(parameters).some((key) => key !== 'plan_ref')) descriptor.blocked = '画面与声音编排仅接收当前方案版本';
    return descriptor;
  }
  if (action === 'compile_production_plan') {
    if (node.type !== 'videoComposeNode') descriptor.blocked = '方案编译必须在视频合成节点执行';
    else if (reviewBlocker(planNode)) descriptor.blocked = '台词识别仍需人工复核，暂不能编译';
    return descriptor;
  }
  if (node.type !== 'scriptNode' || node.data.remixRole !== 'production_plan') {
    descriptor.blocked = '语音校准必须在作品方案节点执行';
    return descriptor;
  }
  const plan = planNode.data.productionPlanV2Draft as ProductionPlanV2 | undefined;
  const takeId = typeof parameters.take_id === 'string' ? parameters.take_id : '';
  const placementId = typeof parameters.placement_id === 'string' ? parameters.placement_id : '';
  const source = takeId ? plan?.speech_takes?.find((item) => item.id === takeId) : plan?.placements.find((item) => item.id === placementId);
  const resourceId = source?.resource_id;
  const resource = plan?.resources.find((item) => item.id === resourceId);
  if (!resource || typeof parameters.turn_id !== 'string' || !plan?.beats.some((beat) => beat.turns.some((turn) => turn.id === parameters.turn_id))) {
    descriptor.blocked = '请先绑定这句台词的完整发声素材';
    return descriptor;
  }
  if (parameters.media_path && parameters.media_path !== resource.media_path) {
    descriptor.blocked = '发声素材已变化，请重新校准';
    return descriptor;
  }
  descriptor.payload.media_path = resource.media_path;
  return descriptor;
}

export function hasCurrentProductionProcessingResult(node: CanvasNode, action: ProductionProcessingAction, nodes: CanvasNode[], edges: CanvasEdge[], requestPayload?: JsonRecord): boolean {
  if (node.data.productionWorkflowAction !== action || node.data.productionWorkflowInputs !== inputSignature(node)) return false;
  const requested = requestPayload ?? (record(node.data.processingPayload) ? node.data.processingPayload : {});
  if (JSON.stringify(node.data.productionWorkflowRequestPayload ?? {}) !== JSON.stringify(requested)) return false;
  if (action === 'generate_production_plan_draft') {
    const analyses = sourceAnalysis(node, nodes, edges);
    const sourceCurrent = analyses.length === 0 || analyses.length === 1
      && node.data.productionWorkflowAnalysisNodeId === analyses[0].id
      && node.data.productionWorkflowAnalysisInputs === analysisSignature(analyses[0]);
    return Boolean(sourceCurrent && exactRef(node.data.productionPlanV2Ref) && node.data.productionPlanV2Draft
      && !node.data.productionPlanV2Dirty && !node.data.productionPlanV2Conflict);
  }
  if (action === 'generate_production_plan_arrangement') return Boolean(exactRef(node.data.productionPlanV2Ref) && node.data.productionPlanV2Draft
    && !node.data.productionPlanV2Dirty && !node.data.productionPlanV2Conflict
    && sameRef(exactRef(node.data.productionWorkflowResultRef), exactRef(node.data.productionPlanV2Ref)));
  if (action === 'compile_production_plan') {
    const planNode = sourcePlan(node, nodes, edges);
    return Boolean(exactRef(node.data.remixManifestRef) && planNode && sameRef(exactRef(node.data.productionPlanV2Ref), exactRef(planNode.data.productionPlanV2Ref)) && node.data.remixInputsCurrent === true && !reviewBlocker(planNode));
  }
  const turnId = requested.turn_id;
  const plan = node.data.productionPlanV2Draft as ProductionPlanV2 | undefined;
  const turn = plan?.beats.flatMap((beat) => beat.turns).find((item) => item.id === turnId);
  const takeId = requested.take_id;
  const take = plan?.speech_takes?.find((item) => item.id === takeId);
  const placementId = typeof requested.placement_id === 'string' ? requested.placement_id : '';
  const ids = turn ? groupedTurnIds(plan!, turn.id, take?.origin_placement_id ?? placementId, typeof takeId === 'string' ? takeId : '') : [];
  return Boolean(ids.length && !node.data.productionPlanV2Dirty && !reviewBlocker(node) && ids.every((id) => {
    const currentTurn = plan?.beats.flatMap((beat) => beat.turns).find((item) => item.id === id);
    const timing = currentTurn?.speech_timing;
    const placement = plan?.placements.find((item) => item.id === timing?.placement_id);
    const resource = plan?.resources.find((item) => item.id === (take?.resource_id ?? placement?.resource_id));
    const cue = plan?.speech_cues?.find((item) => item.turn_id === id);
    return Boolean(timing?.words.length && timing.text === currentTurn?.text && timing.media_hash === resource?.media_hash
      && timing.evidence_ref?.artifact_id && cue?.timing_status === 'ready');
  }));
}

function currentNode(nodeId: string): CanvasNode | undefined { return useCanvasStore.getState().nodes.find((node) => node.id === nodeId); }
function assertScopeCurrent(isCurrent: () => boolean): void {
  if (!isCurrent()) throw new Error('项目或画布已切换，旧任务结果未写入当前画布');
}
function guardCurrent(descriptor: ProductionWorkflowDescriptor, isCurrent: () => boolean): CanvasNode {
  assertScopeCurrent(isCurrent);
  const node = currentNode(descriptor.nodeId);
  const planNode = descriptor.planNodeId ? currentNode(descriptor.planNodeId) : undefined;
  const currentRef = planNode ? exactRef(planNode.data.productionPlanV2Ref) : null;
  const localPlanChanged = planNode && (JSON.stringify(planNode.data.productionPlanV2Draft) !== descriptor.snapshot.planData
    && !pristineLatePlanInitialization(descriptor, planNode.data.productionPlanV2Draft)
    || planNode.data.productionPlanV2Dirty || planNode.data.productionPlanV2Conflict || !sameRef(currentRef, descriptor.snapshot.planRef ?? null));
  const analysis = descriptor.snapshot.analysisNodeId ? currentNode(descriptor.snapshot.analysisNodeId) : undefined;
  const analysisChanged = descriptor.snapshot.analysisNodeId && (!analysis || analysisSignature(analysis) !== descriptor.snapshot.analysisData);
  if (!node || inputSignature(node) !== descriptor.snapshot.nodeData || localPlanChanged || analysisChanged) {
    if (planNode?.data.remixRole === 'production_plan') useCanvasStore.getState().updateNodeData(planNode.id, { productionPlanV2Conflict: true,
      productionPlanV2SaveError: '处理期间作品方案或素材已变化；结果已保留，请重新核对版本' });
    throw new Error('本地修改或方案版本已变化，未覆盖当前作品方案');
  }
  return node;
}
function artifactMatches(artifact: RemixArtifact, ref: RemixArtifactRef, schema: string): boolean {
  return artifact.id === ref.artifactId && artifact.revision === ref.revision && artifact.schema === schema;
}

/** Restore a verified server result after refresh, without resubmitting a paid task. */
export async function recoverProductionWorkflowAction(project: string, action: FreezoneWorkflowRunAction, isCurrent: () => boolean,
  canvasId?: string): Promise<'ready' | 'review' | 'stale' | 'retry' | 'irrelevant'> {
  if (!isCurrent()) return 'stale';
  if (!isProductionProcessingAction(action.action) || !action.result_ref
    || !(action.status === 'completed' && action.artifact_status === 'valid'
      || action.status === 'blocked' && action.artifact_status === 'needs_review')) return 'irrelevant';
  const state = useCanvasStore.getState();
  const node = state.nodes.find((item) => item.id === action.node_id);
  if (!node) return 'stale';
  const resultRef = exactRef(action.result_ref);
  if (!resultRef) return 'stale';
  const attached = (action.action === 'generate_production_plan_draft' || action.action === 'generate_production_plan_arrangement') ? exactRef(node.data.productionPlanV2Ref)
    : action.action === 'compile_production_plan' ? exactRef(node.data.remixManifestRef)
      : exactRef(node.data.productionWorkflowResultRef);
  if (sameRef(attached, resultRef)) return 'ready';
  if (!canvasId) return 'retry';
  const baseline = getFreezoneCanvasBaseline(project, canvasId);
  if (!baseline) return 'retry';
  const remote = await getFreezoneCanvas(project, canvasId);
  if (!isCurrent()) return 'stale';
  const normalizedRemote = normalizeCanvasData(remote.nodes as CanvasNode[], remote.edges as CanvasEdge[]);
  const published = normalizedRemote.nodes.find((item) => item.id === action.node_id);
  if (!published) return 'stale';
  const publishedRef = (action.action === 'generate_production_plan_draft' || action.action === 'generate_production_plan_arrangement') ? exactRef(published.data.productionPlanV2Ref)
    : action.action === 'compile_production_plan' ? exactRef(published.data.remixManifestRef)
      : exactRef(published.data.productionWorkflowResultRef);
  if (!sameRef(publishedRef, resultRef)) return 'retry';
  let applied = false;
  try {
    applied = applyRemoteFreezoneCanvas(project, canvasId, remote,
      (remoteNodes, remoteEdges, localNodes, localEdges) => mergeReferenceAnalysisCanvas(
        remoteNodes, remoteEdges, localNodes, localEdges, null as never, baseline)) === true;
  } catch {
    if (action.action === 'generate_production_plan_draft' || action.action === 'generate_production_plan_arrangement') useCanvasStore.getState().updateNodeData(action.node_id, {
      productionPlanV2Conflict: true, productionPlanV2SaveError: INITIALIZATION_CONFLICT,
    });
    return 'stale';
  }
  if (!applied || !isCurrent()) return 'stale';
  return action.artifact_status === 'needs_review' ? 'review' : 'ready';
}

export function markProductionWorkflowSubmission(descriptor: ProductionWorkflowDescriptor): void {
  useCanvasStore.getState().updateNodeData(descriptor.nodeId, { productionWorkflowSubmission: {
    action: descriptor.action, payload: structuredClone(descriptor.payload), requestPayload: structuredClone(descriptor.requestPayload),
    nodeData: descriptor.snapshot.nodeData,
    planRef: descriptor.snapshot.planRef ?? null, analysisNodeId: descriptor.snapshot.analysisNodeId ?? null,
    analysisData: descriptor.snapshot.analysisData ?? null,
  } });
}
function linkedTo(artifact: RemixArtifact, ref: RemixArtifactRef): boolean {
  return artifact.upstream_refs?.some((upstream) => upstream.artifact_id === ref.artifactId && upstream.revision === ref.revision) === true;
}

export async function reconcileProductionWorkflowResult(project: string, descriptor: ProductionWorkflowDescriptor, resultRef: unknown, isCurrent: () => boolean): Promise<{ status: 'ready' | 'review'; artifactRef: RemixArtifactRef }> {
  if (descriptor.blocked) throw new Error(descriptor.blocked);
  guardCurrent(descriptor, isCurrent);
  const ref = exactRef(resultRef);
  if (!ref) throw new Error('处理任务未返回有效的素材产物版本');
  const artifact = await fetchRemixArtifact(project, ref);
  assertScopeCurrent(isCurrent);
  const already = currentNode(descriptor.nodeId);
  if (already?.data.productionWorkflowAction === descriptor.action
    && sameRef(exactRef(already.data.productionWorkflowResultRef), ref)) {
    return { status: descriptor.action === 'align_production_speech' && reviewBlocker(already) ? 'review' : 'ready', artifactRef: ref };
  }
  guardCurrent(descriptor, isCurrent);
  const store = useCanvasStore.getState();
  if (descriptor.action === 'generate_production_plan_draft' || descriptor.action === 'generate_production_plan_arrangement') {
    if (descriptor.action === 'generate_production_plan_arrangement' && (!descriptor.snapshot.planRef
      || ref.artifactId !== descriptor.snapshot.planRef.artifactId || ref.revision <= descriptor.snapshot.planRef.revision)) throw new Error('编排结果必须修订当前方案');
    if (!artifactMatches(artifact, ref, 'production_plan.v2')) throw new Error('作品方案产物类型或版本不匹配');
    const node = currentNode(descriptor.nodeId)!;
    if (node.data.productionPlanV2Dirty || descriptor.action === 'generate_production_plan_draft' && exactRef(node.data.productionPlanV2Ref) && !isEmptyPlanDraft(node.data.productionPlanV2Draft))
      throw new Error('本地修改或方案版本已变化，未覆盖当前作品方案');
    assertScopeCurrent(isCurrent);
    store.updateNodeData(node.id, { remixRole: 'production_plan', productionPlanV2Ref: ref, productionPlanV2Draft: artifact.payload,
      productionWorkflowAction: descriptor.action, productionWorkflowInputs: descriptor.snapshot.nodeData,
      productionWorkflowRequestPayload: descriptor.requestPayload,
      productionWorkflowResultRef: ref,
      productionWorkflowAnalysisNodeId: descriptor.snapshot.analysisNodeId ?? null,
      productionWorkflowAnalysisInputs: descriptor.snapshot.analysisData ?? null,
      productionPlanV2Dirty: false, productionPlanV2Conflict: false, productionPlanV2SaveError: null });
    for (const compose of store.nodes) if (compose.type === 'videoComposeNode'
      && (compose.data.productionPlanNodeId === node.id || store.edges.some((edge) => edge.source === node.id && edge.target === compose.id))) {
      assertScopeCurrent(isCurrent);
      store.updateNodeData(compose.id, { productionPlanNodeId: node.id, productionPlanV2Ref: ref, remixInputsCurrent: false });
    }
    return { status: 'ready', artifactRef: ref };
  }
  const planRef = descriptor.snapshot.planRef;
  if (!planRef) throw new Error('作品方案版本不可用');
  if (descriptor.action === 'compile_production_plan') {
    if (!artifactMatches(artifact, ref, 'composition_manifest.v2') || !linkedTo(artifact, planRef)) throw new Error('合成清单与当前作品方案版本不一致');
    assertScopeCurrent(isCurrent);
    store.updateNodeData(descriptor.nodeId, { productionPlanNodeId: descriptor.planNodeId, productionPlanV2Ref: planRef,
      productionWorkflowAction: descriptor.action, productionWorkflowInputs: descriptor.snapshot.nodeData,
      productionWorkflowRequestPayload: descriptor.requestPayload,
      productionWorkflowResultRef: ref,
      remixManifestRef: ref, remixManifestLoadedRef: null, remixManifestLoadState: 'loading', remixManifestDirty: false,
      remixInputsCurrent: true, remixOperationId: null, resultVideoUrl: null });
    const compiledNode = currentNode(descriptor.nodeId);
    if (compiledNode) {
      assertScopeCurrent(isCurrent);
      store.updateNodeData(compiledNode.id, { productionWorkflowInputs: inputSignature(compiledNode) });
    }
    return { status: 'ready', artifactRef: ref };
  }
  if (!artifactMatches(artifact, ref, 'word_timeline.v1') || !linkedTo(artifact, planRef)) throw new Error('台词校准产物与当前作品方案版本不一致');
  const planNode = currentNode(descriptor.planNodeId!)!;
  const plan = planNode.data.productionPlanV2Draft as ProductionPlanV2;
  const payload = artifact.payload;
  const leaderId = String(descriptor.payload.turn_id);
  const take = typeof descriptor.payload.take_id === 'string' ? plan.speech_takes?.find((item) => item.id === descriptor.payload.take_id) : undefined;
  const placementId = take?.origin_placement_id ?? String(descriptor.payload.placement_id ?? '');
  const turnIds = groupedTurnIds(plan, leaderId, placementId, typeof descriptor.payload.take_id === 'string' ? descriptor.payload.take_id : '');
  const placement = plan.placements.find((item) => item.id === placementId);
  const resource = plan.resources.find((item) => item.id === (take?.resource_id ?? placement?.resource_id));
  const mediaHash = resource?.media_hash.startsWith('sha256:') ? resource.media_hash : `sha256:${resource?.media_hash ?? ''}`;
  if (!resource || payload.turn_id !== leaderId || payload.placement_id !== placementId || payload.media_hash !== mediaHash
    || payload.source_in_us !== 0 || payload.source_out_us !== resource.duration_us
    || turnIds.length > 1 && JSON.stringify(payload.group_turn_ids) !== JSON.stringify(turnIds)) throw new Error('发声素材或同源台词与校准产物不一致');
  if (payload.status !== 'completed') {
    const reviews = { ...(record(planNode.data.productionSpeechReviews) ? planNode.data.productionSpeechReviews : {}) };
    for (const turnId of turnIds) reviews[turnId] = { status: payload.status === 'no_speech' ? 'no_speech' : 'script_mismatch',
      artifactRef: ref, placementId, mediaHash: resource.media_hash,
      text: plan.beats.flatMap((beat) => beat.turns).find((turn) => turn.id === turnId)?.text ?? '', sourceInUs: 0, sourceOutUs: resource.duration_us,
      message: payload.status === 'no_speech' ? '这段素材没有识别到语音，可按画面节奏继续' : '完整发声素材与作品方案台词不一致，请试听并校正 SRT' };
    assertScopeCurrent(isCurrent);
    store.updateNodeData(planNode.id, { productionSpeechReviews: reviews,
      productionWorkflowAction: descriptor.action, productionWorkflowResultRef: ref,
      productionWorkflowInputs: descriptor.snapshot.nodeData, productionWorkflowRequestPayload: descriptor.requestPayload });
    return { status: 'review', artifactRef: ref };
  }
  const groupRefs = Array.isArray(payload.group_refs) ? payload.group_refs : [];
  if (groupRefs.length !== turnIds.length - 1) throw new Error('同源台词产物不完整，请重新校准');
  const extras = await Promise.all(groupRefs.map((raw) => {
    const groupRef = exactRef(raw);
    if (!groupRef || !record(raw) || typeof raw.turn_id !== 'string' || !turnIds.includes(raw.turn_id)) throw new Error('同源台词产物引用无效');
    return fetchRemixArtifact(project, groupRef);
  }));
  guardCurrent(descriptor, isCurrent);
  const artifacts = [artifact, ...extras];
  const timings: Record<string, PlanSpeechTimingV2> = {};
  for (const turnId of turnIds) {
    const matches = artifacts.filter((item) => item.payload.turn_id === turnId);
    const timed = matches[0];
    const turn = plan.beats.flatMap((beat) => beat.turns).find((item) => item.id === turnId);
    const words = timed?.payload.words;
    if (matches.length !== 1 || !turn || !timed || timed.schema !== 'word_timeline.v1' || !linkedTo(timed, planRef)
      || timed.payload.status !== 'completed' || timed.payload.placement_id !== placementId || timed.payload.media_hash !== mediaHash
      || timed.payload.source_in_us !== 0 || timed.payload.source_out_us !== resource.duration_us
      || timed.payload.transcript !== undefined && timed.payload.transcript !== turn.text
      || typeof timed.payload.text_hash !== 'string' || !Array.isArray(words) || words.length === 0) throw new Error('逐词时间或台词文本与当前方案不一致');
    timings[turnId] = { placement_id: placementId, media_hash: resource.media_hash, text: turn.text,
      text_hash: timed.payload.text_hash, source_in_us: 0, source_out_us: resource.duration_us,
      evidence_ref: { artifact_id: timed.id, revision: timed.revision },
      words: identifyRecognizedWords(turnId, timed.id, timed.revision, words.map((word: unknown) => {
        if (!record(word) || typeof word.text !== 'string' || typeof word.start_us !== 'number' || typeof word.end_us !== 'number') throw new Error('逐词时间产物无效');
        return { text: word.text, start_us: word.start_us, end_us: word.end_us,
          ...(Number.isInteger(word.start_char) && Number.isInteger(word.end_char) ? { start_char: Number(word.start_char), end_char: Number(word.end_char) } : {}) };
      }), turn.speech_timing?.words ?? []) };
  }
  const draft = structuredClone(plan);
  acceptPlanSpeechTimings(draft, timings);
  const reviews = { ...(record(planNode.data.productionSpeechReviews) ? planNode.data.productionSpeechReviews : {}) };
  for (const turnId of turnIds) delete reviews[turnId];
  assertScopeCurrent(isCurrent);
  store.updateNodeData(planNode.id, { productionPlanV2Draft: draft, productionPlanV2Dirty: true, productionSpeechReviews: reviews });
  assertScopeCurrent(isCurrent);
  await flushProductionPlanV2AutoSave(project, planNode.id, isCurrent);
  assertScopeCurrent(isCurrent);
  useCanvasStore.getState().updateNodeData(planNode.id, { productionWorkflowAction: descriptor.action,
    productionWorkflowInputs: descriptor.snapshot.nodeData, productionWorkflowRequestPayload: descriptor.requestPayload });
  assertScopeCurrent(isCurrent);
  useCanvasStore.getState().updateNodeData(planNode.id, { productionWorkflowResultRef: ref });
  return { status: 'ready', artifactRef: ref };
}
