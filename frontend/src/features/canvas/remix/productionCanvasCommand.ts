import { preflightProductionCommand, applyProductionCommand, attachRemixMedia, fetchRemixArtifact, type ProductionCommandResult, type RemixArtifactRef, type RemixUploadedMedia } from '@/api/remix';
import type { CanvasNode } from '../domain/canvasNodes';
import { isUpstreamConnectionAllowed, nodeHasSourceHandle, nodeHasTargetHandle } from '../domain/nodeRegistry';
import { useCanvasStore } from '@/stores/canvasStore';
import { flushProductionPlanV2AutoSave } from './productionPlanV2AutoSave';
import { planMediaCandidates } from './productionPlanModel';
import type { PlanResourceV2, ProductionPlanV2 } from './productionPlanV2Model';
import { captureFreezoneCanvasScope } from '@/features/freezone/canvasSyncRuntime';

type CommandKind = 'set_goal' | 'insert_beat' | 'insert_turn' | 'reorder_beats' | 'reorder_turns' | 'batch' | 'edit_beat' | 'edit_speech_take' | 'edit_turn' | 'bind_media' | 'bind_speech_source' | 'reassign_speech_source' | 'upsert_placement' | 'upsert_selection' | 'remove_entity' | 'set_output';
export interface ProductionCanvasCommand {
  type: 'production_plan_command';
  node_id: string;
  command: { command_id: string; plan_ref: { artifact_id: string; revision: number }; kind: CommandKind; target_id: string; payload: Record<string, unknown> };
}
const fields: Record<CommandKind, string[]> = {
  set_goal: ['goal'],
  insert_beat: ['beat', 'index'], insert_turn: ['beat_id', 'turn', 'index'],
  reorder_beats: ['ids'], reorder_turns: ['ids'], batch: ['commands'],
  edit_beat: ['speech_pace', 'extra_pause_seconds', 'purpose', 'duration_frames', 'timing_mode', 'duration_source_placement_id'], edit_turn: ['speaker', 'text', 'speech_placement_id', 'invalidate_speech_timing'],
  edit_speech_take: ['source_in_us', 'source_out_us', 'speed_num', 'speed_den', 'lead_in_frames', 'volume'],
  bind_media: ['resource_id', 'resource', 'invalidate_speech_timing'], bind_speech_source: ['placement_id', 'turn_ids'],
  reassign_speech_source: ['placement_id', 'turn_ids'],
  upsert_placement: ['placement'], upsert_selection: ['selection'], remove_entity: ['entity_kind'], set_output: ['output'],
};
const record = (value: unknown): value is Record<string, unknown> => Boolean(value && typeof value === 'object' && !Array.isArray(value));
const hasOwn = (value: object, key: string): boolean => Object.prototype.hasOwnProperty.call(value, key);
const resourceFields = ['id', 'node_id', 'node_revision', 'media_kind', 'media_path', 'media_hash', 'duration_us', 'has_audio'];
function validNewResource(value: unknown, resourceId: unknown): value is PlanResourceV2 {
  if (!record(value) || Object.keys(value).some((key) => !resourceFields.includes(key))
    || !['id', 'node_id', 'media_kind', 'media_path', 'media_hash', 'duration_us', 'has_audio'].every((key) => hasOwn(value, key))) return false;
  const path = value.media_path;
  return typeof resourceId === 'string' && value.id === resourceId
    && typeof value.id === 'string' && Boolean(value.id.trim())
    && typeof value.node_id === 'string' && Boolean(value.node_id.trim())
    && typeof value.media_kind === 'string' && ['image', 'video', 'audio'].includes(value.media_kind)
    && typeof path === 'string' && Boolean(path) && !path.startsWith('/') && !path.includes('\\')
    && path.split('/').every((part) => Boolean(part) && part !== '.' && part !== '..')
    && typeof value.media_hash === 'string' && /^[a-f0-9]{64}$/.test(value.media_hash)
    && Number.isSafeInteger(value.duration_us) && Number(value.duration_us) >= 0
    && typeof value.has_audio === 'boolean'
    && (value.node_revision === undefined || value.node_revision === null
      || Number.isSafeInteger(value.node_revision) && Number(value.node_revision) >= 1);
}
const sameRef = (ref: unknown, expected: { artifact_id: string; revision: number }) => record(ref)
  && ref.artifactId === expected.artifact_id && ref.revision === expected.revision;

export function parseProductionCanvasCommand(value: unknown): ProductionCanvasCommand | null {
  if (!record(value) || value.type !== 'production_plan_command' || typeof value.node_id !== 'string' || !value.node_id.trim() || !record(value.command)) return null;
  const command = value.command;
  if (Object.keys(command).sort().join(',') !== 'command_id,kind,payload,plan_ref,target_id'
    || typeof command.command_id !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(command.command_id)
    || typeof command.kind !== 'string' || !hasOwn(fields, command.kind)
    || typeof command.target_id !== 'string' || !command.target_id.trim()
    || !record(command.plan_ref) || Object.keys(command.plan_ref).sort().join(',') !== 'artifact_id,revision'
    || typeof command.plan_ref.artifact_id !== 'string' || !/^[A-Za-z0-9_-]{1,64}$/.test(command.plan_ref.artifact_id)
    || !Number.isSafeInteger(command.plan_ref.revision) || Number(command.plan_ref.revision) < 1
    || !record(command.payload) || !Object.keys(command.payload).length
    || Object.keys(command.payload).some((key) => !fields[command.kind as CommandKind].includes(key))) return null;
  if (command.kind === 'edit_beat') {
    const pace = command.payload.speech_pace;
    const pause = command.payload.extra_pause_seconds;
    if (hasOwn(command.payload, 'speech_pace') && pace !== null && (typeof pace !== 'string' || !['slow', 'normal', 'fast'].includes(pace))) return null;
    if (hasOwn(command.payload, 'extra_pause_seconds') && pause !== null
      && (typeof pause !== 'number' || !Number.isFinite(pause) || pause < 0)) return null;
  }
  if (command.kind === 'batch') {
    const children = command.payload.commands;
    if (!Array.isArray(children) || children.length < 1 || children.length > 100) return null;
    for (const child of children) {
      if (!record(child) || child.kind === 'batch' || Object.keys(child).sort().join(',') !== 'kind,payload,target_id'
        || !parseProductionCanvasCommand({ type: value.type, node_id: value.node_id, command: {
          ...child, command_id: command.command_id, plan_ref: command.plan_ref,
        } })) return null;
    }
  }
  if (command.kind === 'insert_beat' || command.kind === 'insert_turn') {
    const key = command.kind === 'insert_beat' ? 'beat' : 'turn';
    if (Object.keys(command.payload).length !== fields[command.kind].length
      || !Number.isSafeInteger(command.payload.index) || Number(command.payload.index) < 0
      || !record(command.payload[key]) || command.payload[key].id !== command.target_id
      || command.kind === 'insert_turn' && (typeof command.payload.beat_id !== 'string' || !command.payload.beat_id.trim())) return null;
  }
  if (command.kind === 'reorder_beats' || command.kind === 'reorder_turns') {
    const ids = command.payload.ids;
    if (!Array.isArray(ids) || ids.some((id) => typeof id !== 'string' || !id.trim()) || new Set(ids).size !== ids.length) return null;
  }
  if (hasOwn(command.payload, 'invalidate_speech_timing') && typeof command.payload.invalidate_speech_timing !== 'boolean') return null;
  if (command.kind === 'edit_turn' && hasOwn(command.payload, 'speech_placement_id')
    && command.payload.speech_placement_id !== null
    && (typeof command.payload.speech_placement_id !== 'string' || !command.payload.speech_placement_id.trim())) return null;
  if (command.kind === 'bind_speech_source' && (typeof command.payload.placement_id !== 'string' || !command.payload.placement_id.trim()
    || !Array.isArray(command.payload.turn_ids) || !command.payload.turn_ids.length
    || command.payload.turn_ids.some((id) => typeof id !== 'string' || !id.trim())
    || new Set(command.payload.turn_ids).size !== command.payload.turn_ids.length)) return null;
  if (command.kind === 'reassign_speech_source' && (Object.keys(command.payload).length !== 2
    || command.payload.placement_id !== null && (typeof command.payload.placement_id !== 'string' || !command.payload.placement_id.trim())
    || !Array.isArray(command.payload.turn_ids) || command.payload.turn_ids.length < 2
    || command.payload.turn_ids.some((id) => typeof id !== 'string' || !id.trim())
    || new Set(command.payload.turn_ids).size !== command.payload.turn_ids.length)) return null;
  if (command.kind === 'bind_media' && (typeof command.payload.resource_id !== 'string' || !command.payload.resource_id
    || hasOwn(command.payload, 'resource') && !validNewResource(command.payload.resource, command.payload.resource_id))) return null;
  return { type: 'production_plan_command', node_id: value.node_id, command: command as unknown as ProductionCanvasCommand['command'] };
}

type BindSource = { resource: PlanResourceV2; outputUrl: string; currentPath: string | null; nodeRevision: unknown; media: RemixUploadedMedia | null };
const activeCandidateBinds = new Map<string, symbol>();
function newBindResources(command: ProductionCanvasCommand['command']): PlanResourceV2[] {
  if (command.kind === 'batch') return (command.payload.commands as Array<ProductionCanvasCommand['command']>).flatMap(newBindResources);
  return command.kind === 'bind_media' && hasOwn(command.payload, 'resource')
    ? [command.payload.resource as PlanResourceV2] : [];
}
function candidateOutputUrl(candidate: ReturnType<typeof planMediaCandidates>[number]): string | null {
  const data = candidate.node.data;
  const value = candidate.kind === 'audio' ? data.audioUrl : candidate.kind === 'video' ? data.videoUrl : data.imageUrl ?? data.previewImageUrl;
  return typeof value === 'string' ? value : null;
}
function sourceForNewBind(project: string, command: ProductionCanvasCommand, resource: PlanResourceV2, prior?: BindSource): BindSource {
  const state = useCanvasStore.getState();
  const candidate = planMediaCandidates(project, state.nodes, command.node_id).find((item) => item.node.id === resource.node_id);
  if (!candidate || candidate.node.data.workflowResultStale === true || candidate.node.data.remixVisualOutputCurrent === false
    || candidate.kind !== resource.media_kind) throw new Error('画布素材不存在、已失效或类型已变化');
  const outputUrl = candidateOutputUrl(candidate);
  if (!outputUrl || (candidate.currentPath
    ? candidate.currentPath !== resource.media_path
    : !candidate.media || candidate.media.media_path !== resource.media_path)) {
    throw new Error('画布素材输出路径已变化，请重新读取素材');
  }
  if (resource.node_revision != null && candidate.node.data.remixNodeRevision !== resource.node_revision) {
    throw new Error('画布素材版本已变化，请重新读取素材');
  }
  if (candidate.media && !sameMedia(candidate.media, resource)) {
    throw new Error('画布素材实际信息与绑定命令不一致，请重新读取素材');
  }
  if (prior && (prior.outputUrl !== outputUrl || prior.currentPath !== candidate.currentPath
    || prior.nodeRevision !== candidate.node.data.remixNodeRevision)) {
    throw new Error('画布素材在绑定期间发生变化，请重新读取素材');
  }
  const planNode = state.nodes.find((node) => node.id === command.node_id);
  if (!planNode || !nodeHasSourceHandle(candidate.node.type) || !nodeHasTargetHandle(planNode.type)
    || !isUpstreamConnectionAllowed(candidate.node.type, planNode.type)) throw new Error('无法连接素材节点到作品方案');
  return { resource, outputUrl, currentPath: candidate.currentPath,
    nodeRevision: candidate.node.data.remixNodeRevision, media: candidate.media };
}
function sameMedia(media: RemixUploadedMedia, resource: PlanResourceV2): boolean {
  return media.media_path === resource.media_path && media.media_hash === resource.media_hash
    && media.media_kind === resource.media_kind && media.duration_us === resource.duration_us
    && (media.has_audio === true) === resource.has_audio;
}

function replayResult(command: ProductionCanvasCommand, node: CanvasNode): ProductionCommandResult | null {
  const data = node.data;
  const saved = data.productionLastCommandResult as ProductionCommandResult | undefined;
  return data.productionLastCommandRequest === JSON.stringify(command.command) && saved && sameRef(data.productionPlanV2Ref, saved.plan_ref) ? saved : null;
}

export function productionCanvasCommandError(command: ProductionCanvasCommand, nodes: CanvasNode[]): string | null {
  const node = nodes.find((item) => item.id === command.node_id);
  if (!node || node.type !== 'scriptNode' || node.data.remixRole !== 'production_plan') return '找不到可编辑的作品方案节点';
  if (replayResult(command, node)) return null;
  if (node.data.productionPlanV2Conflict || !sameRef(node.data.productionPlanV2Ref, command.command.plan_ref)) return '作品方案版本已变化，请重新读取后提交修改';
  return null;
}

/** Reads saved plan state only; passing does not authorize execution. */
export async function preflightProductionCanvasCommand(command: ProductionCanvasCommand, project: string, canvasId: string) {
  const isCurrentScope = captureFreezoneCanvasScope(project, canvasId);
  const assertCurrent = () => {
    if (!isCurrentScope()) throw new Error('当前项目或画布已切换，请重新校验');
    const error = productionCanvasCommandError(command, useCanvasStore.getState().nodes);
    if (error) throw new Error(error);
    const node = useCanvasStore.getState().nodes.find((item) => item.id === command.node_id)!;
    if (node.data.productionPlanV2Dirty || node.data.productionPlanV2Saving) throw new Error('作品方案有未保存编辑，请先完成保存后重新校验');
    return node;
  };
  if (!parseProductionCanvasCommand(command)) throw new Error('作品方案命令字段无效');
  const initial = assertCurrent();
  const draft = initial.data.productionPlanV2Draft;
  const result = await preflightProductionCommand(project, command.command);
  const current = assertCurrent();
  if (current.data.productionPlanV2Draft !== draft
    || result.ok && !result.plan_ref
    || result.plan_ref && (result.plan_ref.artifact_id !== command.command.plan_ref.artifact_id
      || result.plan_ref.revision !== command.command.plan_ref.revision)) throw new Error('作品方案在校验期间发生变化，请重新读取并校验');
  return result;
}

/** Binds a canvas output using the same revisioned command as Agent plan edits. */
export async function bindProductionCanvasCandidate(project: string, canvasId: string, planNodeId: string,
  placementId: string, candidateNodeId: string, isStillRequested?: () => boolean): Promise<{ nodeId: string; output: ProductionCommandResult }> {
  const targetKey = JSON.stringify([project, canvasId, planNodeId, placementId]);
  const claim = Symbol('candidate bind');
  activeCandidateBinds.set(targetKey, claim);
  const claimed = () => activeCandidateBinds.get(targetKey) === claim && (!isStillRequested || isStillRequested());
  const isCurrentScope = captureFreezoneCanvasScope(project, canvasId);
  const assertCurrent = () => {
    if (!isCurrentScope()) throw new Error('当前画布已切换，请重新选择素材');
    if (!claimed()) throw new Error('绑定目标已变化，请重新选择素材');
  };
  try {
    assertCurrent();
    const node = useCanvasStore.getState().nodes.find((item) => item.id === planNodeId);
    const placement = (node?.data.productionPlanV2Draft as ProductionPlanV2 | undefined)?.placements?.find((item) => item.id === placementId);
    if (!placement || (placement.kind !== 'visual' && placement.kind !== 'audio')) throw new Error('找不到可绑定的画面或声音位置');
    const candidate = planMediaCandidates(project, useCanvasStore.getState().nodes, planNodeId).find((item) => item.node.id === candidateNodeId);
    if (!candidate || candidate.node.data.workflowResultStale === true || candidate.node.data.remixVisualOutputCurrent === false) throw new Error('素材不存在或已失效，请重新生成');
    const outputUrl = candidateOutputUrl(candidate);
    const media = candidate.media ?? (candidate.currentPath ? await attachRemixMedia(project, candidate.currentPath) : null);
    assertCurrent();
    if (!media) throw new Error('请先完成素材生成或上传');
    if (candidate.kind !== media.media_kind) throw new Error('素材类型已变化，请重新读取素材');
    if (placement.kind === 'visual' && media.media_kind === 'audio') throw new Error('画面需要图片或视频');
    if (placement.kind === 'audio' && (media.media_kind === 'image' || media.media_kind === 'video' && !media.has_audio)) throw new Error('声音需要音频或带声音的视频');
    const ref = await flushProductionPlanV2AutoSave(project, planNodeId, isCurrentScope);
    assertCurrent();
    const current = useCanvasStore.getState().nodes.find((item) => item.id === planNodeId)?.data;
    const plan = current?.productionPlanV2Draft as ProductionPlanV2 | undefined;
    const latestPlacement = plan?.placements?.find((item) => item.id === placementId);
    if (!latestPlacement || latestPlacement.kind !== placement.kind || latestPlacement.resource_id !== placement.resource_id)
      throw new Error('绑定位置已变化，请重新选择素材');
    const latestCandidate = planMediaCandidates(project, useCanvasStore.getState().nodes, planNodeId).find((item) => item.node.id === candidateNodeId);
    if (!latestCandidate || candidateOutputUrl(latestCandidate) !== outputUrl
      || latestCandidate.currentPath !== candidate.currentPath
      || latestCandidate.node.data.remixNodeRevision !== candidate.node.data.remixNodeRevision
      || latestCandidate.node.data.workflowResultStale === true || latestCandidate.node.data.remixVisualOutputCurrent === false) {
      throw new Error('素材在绑定期间发生变化，请重新读取素材');
    }
    const exactRef = ref ?? current?.productionPlanV2Ref as RemixArtifactRef | undefined;
    if (!exactRef || current?.productionPlanV2Conflict) throw new Error('请先保存作品方案并处理版本冲突');
    const revision = latestCandidate.node.data.remixNodeRevision;
    const resourceId = `resource:${candidateNodeId}:${media.media_hash.slice(0, 12)}`;
    const resource: PlanResourceV2 = { id: resourceId, node_id: candidateNodeId,
      node_revision: typeof revision === 'number' && Number.isInteger(revision) && revision > 0 ? revision : null,
      media_kind: media.media_kind, media_path: media.media_path, media_hash: media.media_hash,
      duration_us: media.duration_us, has_audio: media.has_audio === true };
    const registered = plan?.resources?.find((item) => item.id === resourceId);
    if (registered && (registered.node_id !== candidateNodeId || !sameMedia(media, registered))) {
      throw new Error('已登记素材与当前输出不一致，请重新读取素材');
    }
    const command: ProductionCanvasCommand = { type: 'production_plan_command', node_id: planNodeId, command: {
      command_id: crypto.randomUUID(), plan_ref: { artifact_id: exactRef.artifactId, revision: exactRef.revision },
      kind: 'bind_media', target_id: placementId,
      payload: { resource_id: resourceId, ...(registered ? {} : { resource }) },
    } };
    if (!registered) sourceForNewBind(project, command, resource);
    return await executeProductionCanvasCommand(command, project, canvasId, claimed);
  } finally {
    if (activeCandidateBinds.get(targetKey) === claim) activeCandidateBinds.delete(targetKey);
  }
}

/** Runs only after the existing canvas approval bridge accepts this command. */
export async function executeProductionCanvasCommand(command: ProductionCanvasCommand, project: string, canvasId: string,
  isStillRequested?: () => boolean): Promise<{ nodeId: string; output: ProductionCommandResult }> {
  const isCurrentScope = captureFreezoneCanvasScope(project, canvasId);
  const assertScope = () => { if (!isCurrentScope()) throw new Error('当前画布已切换，旧请求结果已保留在原项目，未修改当前画布'); };
  const assertRequested = () => { if (isStillRequested && !isStillRequested()) throw new Error('绑定目标已变化，请重新选择素材'); };
  assertScope();
  assertRequested();
  if (!parseProductionCanvasCommand(command)) throw new Error('作品方案命令字段无效');
  const initial = useCanvasStore.getState().nodes.find((node) => node.id === command.node_id);
  if (initial) { const replay = replayResult(command, initial); if (replay) return { nodeId: command.node_id, output: replay }; }
  const before = productionCanvasCommandError(command, useCanvasStore.getState().nodes);
  if (before) throw new Error(before);
  await flushProductionPlanV2AutoSave(project, command.node_id, isCurrentScope);
  assertScope();
  const error = productionCanvasCommandError(command, useCanvasStore.getState().nodes);
  if (error) throw new Error(error);
  const resources = newBindResources(command.command);
  const bindings: Array<{ resource: PlanResourceV2; source: BindSource; media: RemixUploadedMedia }> = [];
  const plan = useCanvasStore.getState().nodes.find((node) => node.id === command.node_id)?.data.productionPlanV2Draft as ProductionPlanV2 | undefined;
  // Validate every child before registering or mutating any resource.
  const sources = resources.map((resource) => {
    if (plan?.resources?.some((row) => row.id === resource.id)) throw new Error('素材已在作品方案中登记');
    return sourceForNewBind(project, command, resource);
  });
  for (let index = 0; index < resources.length; index += 1) {
    const resource = resources[index];
    const source = sources[index];
    const media = source.media ?? (source.currentPath ? await attachRemixMedia(project, source.currentPath) : null);
    assertScope();
    sources.forEach((prior) => sourceForNewBind(project, command, prior.resource, prior));
    if (!media || !sameMedia(media, resource)) throw new Error('素材实际信息与绑定命令不一致，请重新读取素材');
    bindings.push({ resource, source, media });
  }
  assertScope();
  assertRequested();
  const data = useCanvasStore.getState().nodes.find((node) => node.id === command.node_id)!.data;
  const draft = data.productionPlanV2Draft;
  const output = await applyProductionCommand(project, command.command);
  assertScope();
  const nextRef: RemixArtifactRef = { artifactId: output.plan_ref.artifact_id, revision: output.plan_ref.revision };
  const artifact = await fetchRemixArtifact(project, nextRef);
  assertScope();
  if (artifact.schema !== 'production_plan.v2' || artifact.id !== nextRef.artifactId || artifact.revision !== nextRef.revision) throw new Error('保存的作品方案类型或版本不匹配');
  const current = useCanvasStore.getState().nodes.find((node) => node.id === command.node_id)?.data;
  let sourceChanged = false;
  for (const { resource, source } of bindings) {
    try { sourceForNewBind(project, command, resource, source); }
    catch { sourceChanged = true; }
  }
  if (!current || current.productionPlanV2Draft !== draft || current.productionPlanV2Dirty || !sameRef(current.productionPlanV2Ref, command.command.plan_ref) || sourceChanged
    || isStillRequested && !isStillRequested()) {
    if (current) useCanvasStore.getState().updateNodeData(command.node_id, { productionPlanV2Conflict: true, productionPlanV2SaveError: '虾导已保存新版本；执行期间的本地修改已保留，请处理版本冲突' });
    throw new Error('虾导已保存新版本，本地修改已保留，请处理版本冲突');
  }
  const store = useCanvasStore.getState();
  for (const { resource, media } of bindings) {
    if (!store.edges.some((edge) => edge.source === resource.node_id && edge.target === command.node_id)
      && !store.addEdge(resource.node_id, command.node_id)) {
      store.updateNodeData(command.node_id, { productionPlanV2Conflict: true, productionPlanV2SaveError: '作品方案已保存，但素材节点未能连接，请处理画布连线冲突' });
      throw new Error('作品方案已保存，但素材节点未能连接，请处理画布连线冲突');
    }
    store.updateNodeData(resource.node_id, { remixMedia: media });
  }
  store.updateNodeData(command.node_id, {
    productionPlanV2Ref: nextRef, productionPlanV2Draft: artifact.payload,
    productionPlanV2Dirty: false, productionPlanV2Conflict: false, productionPlanV2SaveError: null,
    productionLastCommandRequest: JSON.stringify(command.command), productionLastCommandResult: output,
  });
  for (const node of store.nodes) if (node.type === 'videoComposeNode' && node.data.productionPlanNodeId === command.node_id) {
    store.updateNodeData(node.id, { productionPlanV2Ref: nextRef, remixInputsCurrent: false });
  }
  return { nodeId: command.node_id, output };
}
