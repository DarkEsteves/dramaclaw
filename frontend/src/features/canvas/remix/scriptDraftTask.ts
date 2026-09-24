import {
  fetchRemixArtifact,
  fetchRemixOperation,
  fetchRemixTaskStatus,
  submitRemixProcessAction,
  type RemixArtifactRef,
} from '@/api/remix';
import { readUrl } from '@/lib/url-params';
import { useCanvasStore } from '@/stores/canvasStore';
import { applyRemixInvalidation } from './spawnRemixDownstream';
import { type RemixScriptDraft, type RemixPerformanceSegment } from './remixScriptModel';

const observers = new Map<string, Promise<RemixScriptDraft | null>>();
const POLL_INTERVAL_MS = 1_000;
const POLL_TIMEOUT_MS = 30 * 60_000;

function exactRef(value: unknown): RemixArtifactRef | null {
  if (!value || typeof value !== 'object') return null;
  const ref = value as { artifactId?: unknown; revision?: unknown };
  return typeof ref.artifactId === 'string' && Number.isInteger(ref.revision) && Number(ref.revision) > 0
    ? { artifactId: ref.artifactId, revision: Number(ref.revision) }
    : null;
}

function sameRef(left: RemixArtifactRef | null, right: RemixArtifactRef | null): boolean {
  return Boolean(left && right && left.artifactId === right.artifactId && left.revision === right.revision);
}

function nodeData(nodeId: string): Record<string, unknown> | null {
  return useCanvasStore.getState().nodes.find((node) => node.id === nodeId)?.data ?? null;
}

function patch(nodeId: string, value: Record<string, unknown>): void {
  useCanvasStore.getState().updateNodeData(nodeId, value);
}

function currentBlueprint(data: Record<string, unknown> | null): RemixArtifactRef | null {
  return exactRef(data?.remixBlueprintRef) ?? exactRef(data?.referenceBlueprintRef);
}

function matchesRequest(data: Record<string, unknown> | null, blueprintRef: RemixArtifactRef, topic: string): boolean {
  const liveBlueprint = currentBlueprint(data);
  return data?.remixTopic === topic
    && sameRef(exactRef(data?.remixScriptGenerationBlueprintRef), blueprintRef)
    && (!liveBlueprint || sameRef(liveBlueprint, blueprintRef));
}

function fail(nodeId: string, operationId: string, message: string): void {
  const current = nodeData(nodeId);
  if (current?.remixScriptGenerationOperationId !== operationId) return;
  patch(nodeId, {
    remixScriptGenerationOperationId: null,
    remixScriptGenerationBlueprintRef: null,
    remixScriptGenerationTopic: null,
    remixScriptGenerationStatus: 'failed',
    remixScriptGenerationError: message,
  });
}

function fingerprint(value: string): string {
  let hash = 0x811c9dc5;
  for (const char of value) {
    hash ^= char.codePointAt(0) ?? 0;
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(16).padStart(8, '0');
}

async function delay(): Promise<void> {
  await new Promise((resolve) => window.setTimeout(resolve, POLL_INTERVAL_MS));
}

function draftFromPayload(payload: Record<string, unknown>): RemixScriptDraft {
  if (typeof payload.dialogue !== 'string' || !Array.isArray(payload.selections)) {
    throw new Error('改编台词任务返回了无效结果。');
  }
  const segments = Array.isArray(payload.segments)
    ? payload.segments.map((raw) => {
      const row = raw as Record<string, unknown>;
      return {
        id: row.segment_id, start: row.start_char, end: row.end_char,
        speaker: row.speaker, source: row.source,
        ...(Array.isArray(row.reference_bindings) ? { referenceBindings: row.reference_bindings.map((binding) => ({
          nodeId: (binding as Record<string, unknown>).node_id,
          purpose: (binding as Record<string, unknown>).purpose,
        })) } : {}),
        ...(row.person_node_id ? { personNodeId: row.person_node_id } : {}),
        ...(row.product_node_id ? { productNodeId: row.product_node_id } : {}),
        ...(row.setting_node_id ? { settingNodeId: row.setting_node_id } : {}),
        ...(row.motion_node_id ? { motionNodeId: row.motion_node_id } : {}),
        ...(row.voice_node_id ? { voiceNodeId: row.voice_node_id } : {}),
        ...(row.take_node_id ? { takeNodeId: row.take_node_id } : {}),
        ...(row.take_media_revision ? { takeMediaRevision: row.take_media_revision } : {}),
        ...(row.primary_node_id ? { primaryNodeId: row.primary_node_id } : {}),
        ...(row.primary_media_revision ? { primaryMediaRevision: row.primary_media_revision } : {}),
      } as RemixPerformanceSegment;
    })
    : [];
  return { dialogue: payload.dialogue, selections: payload.selections as RemixScriptDraft['selections'], segments };
}

async function observe(
  nodeId: string,
  project: string,
  operationId: string,
  blueprintRef: RemixArtifactRef,
  topic: string,
): Promise<RemixScriptDraft | null> {
  const key = `${nodeId}:${operationId}`;
  const active = observers.get(key);
  if (active) return active;
  const pending = (async () => {
    const deadline = Date.now() + POLL_TIMEOUT_MS;
    try {
      while (Date.now() < deadline) {
        const before = nodeData(nodeId);
        if (before?.remixScriptGenerationOperationId !== operationId) return null;
        if (!matchesRequest(before, blueprintRef, topic)) {
          fail(nodeId, operationId, '改编规则或新主题已修改，请重新生成台词。');
          return null;
        }
        const operation = await fetchRemixOperation(project, operationId);
        const operationMatches = operation.input_refs?.some(
          (ref) => ref.artifact_id === blueprintRef.artifactId && ref.revision === blueprintRef.revision,
        );
        if (!operationMatches) throw new Error('改编台词任务对应的蓝图版本不一致。');
        if (operation.status === 'failed' || operation.status === 'cancelled' || operation.status === 'stale') {
          throw new Error('改编台词生成失败，请重试。');
        }
        if (operation.status === 'needs_confirmation' || operation.status === 'completed') {
          const output = operation.output_refs.find((ref) => typeof ref.artifact_id === 'string' && Number.isInteger(ref.revision));
          if (!output?.artifact_id || !output.revision) throw new Error('改编台词任务完成，但没有返回结果。');
          const artifact = await fetchRemixArtifact(project, { artifactId: output.artifact_id, revision: output.revision });
          if (artifact.schema !== 'remix_script.v1') throw new Error('改编台词任务返回的格式无效。');
          const linked = artifact.upstream_refs?.some(
            (ref) => ref.artifact_id === blueprintRef.artifactId && ref.revision === blueprintRef.revision,
          );
          if (!linked) throw new Error('改编台词任务对应的蓝图版本不一致。');
          if (!matchesRequest(nodeData(nodeId), blueprintRef, topic)) return null;
          const draft = draftFromPayload(artifact.payload);
          const scriptRef = { artifactId: artifact.id, revision: artifact.revision };
          applyRemixInvalidation(nodeId, 'script');
          patch(nodeId, {
            remixScriptGenerationOperationId: null,
            remixScriptGenerationBlueprintRef: null,
            remixScriptGenerationTopic: null,
            remixScriptGenerationStatus: 'completed',
            remixScriptGenerationError: null,
            remixScriptDraft: draft,
            remixScriptRef: scriptRef,
            remixScriptLastSavedRef: scriptRef,
            remixBrollPlanRef: null,
            remixBrollScriptRef: null,
            remixBrollRequirements: [],
            remixBrollDecisions: {},
            remixBrollPlanStatus: null,
            remixBrollOperationId: null,
            remixBrollOperationScriptRef: null,
          });
          return draft;
        }
        if (operation.task_type && operation.task_scope) {
          const task = await fetchRemixTaskStatus(project, operation.task_type, operation.task_scope);
          if (task?.status === 'failed') throw new Error(task.error || '改编台词生成失败，请重试。');
        }
        await delay();
      }
      throw new Error('改编台词生成超时，请在任务中心检查状态。');
    } catch (cause) {
      fail(nodeId, operationId, cause instanceof Error ? cause.message : String(cause));
      return null;
    } finally {
      observers.delete(key);
    }
  })();
  observers.set(key, pending);
  return pending;
}

export async function startScriptDraftGeneration(
  nodeId: string,
  blueprintRef: RemixArtifactRef,
  topic: string,
): Promise<RemixScriptDraft | null> {
  const project = readUrl().project;
  const cleanTopic = topic.trim();
  const data = nodeData(nodeId);
  if (!project) throw new Error('请先打开项目画布');
  if (!cleanTopic) throw new Error('请填写新主题');
  if (typeof data?.remixScriptGenerationOperationId === 'string' && data.remixScriptGenerationOperationId) {
    return resumeScriptDraftGeneration(nodeId);
  }
  const nodeRevision = Number(data?.nodeRevision ?? 0) + 1;
  patch(nodeId, {
    remixTopic: cleanTopic,
    remixScriptGenerationBlueprintRef: blueprintRef,
    remixScriptGenerationTopic: cleanTopic,
    remixScriptGenerationStatus: 'running',
    remixScriptGenerationError: null,
    nodeRevision,
  });
  try {
    const task = await submitRemixProcessAction(project, {
      action: 'generate_script_draft',
      blueprintRef,
      newTopic: cleanTopic,
      idempotencyKey: `script-draft:${nodeId}:${nodeRevision}:${blueprintRef.artifactId}:${blueprintRef.revision}:${fingerprint(cleanTopic)}`,
      nodeRevision,
    });
    if (!matchesRequest(nodeData(nodeId), blueprintRef, cleanTopic)) return null;
    patch(nodeId, { remixScriptGenerationOperationId: task.operation_id });
    return observe(nodeId, project, task.operation_id, blueprintRef, cleanTopic);
  } catch (cause) {
    patch(nodeId, {
      remixScriptGenerationOperationId: null,
      remixScriptGenerationStatus: 'failed',
      remixScriptGenerationError: cause instanceof Error ? cause.message : String(cause),
    });
    return null;
  }
}

export async function resumeScriptDraftGeneration(nodeId: string): Promise<RemixScriptDraft | null> {
  const project = readUrl().project;
  const data = nodeData(nodeId);
  const operationId = typeof data?.remixScriptGenerationOperationId === 'string' ? data.remixScriptGenerationOperationId : '';
  const blueprintRef = exactRef(data?.remixScriptGenerationBlueprintRef);
  const topic = typeof data?.remixScriptGenerationTopic === 'string' ? data.remixScriptGenerationTopic : '';
  if (!project || !operationId || !blueprintRef || !topic) return null;
  if (!matchesRequest(data, blueprintRef, topic)) {
    fail(nodeId, operationId, '改编规则或新主题已修改，请重新生成台词。');
    return null;
  }
  return observe(nodeId, project, operationId, blueprintRef, topic);
}
