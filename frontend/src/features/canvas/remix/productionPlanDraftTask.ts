import {
  fetchRemixArtifact, fetchRemixOperation, fetchRemixTaskStatus, submitRemixProcessAction,
  type RemixArtifactRef, type RemixTaskRef,
} from '@/api/remix';
import { readUrl } from '@/lib/url-params';
import { useCanvasStore } from '@/stores/canvasStore';
import type { ProductionPlan } from './productionPlanModel';

const submissions = new Map<string, Promise<RemixTaskRef>>();
const observers = new Map<string, Promise<void>>();
const POLL_INTERVAL_MS = 1_000;
const POLL_TIMEOUT_MS = 30 * 60_000;

function nodeData(nodeId: string): Record<string, unknown> | null {
  return useCanvasStore.getState().nodes.find((node) => node.id === nodeId)?.data ?? null;
}

function patch(nodeId: string, data: Record<string, unknown>): void {
  useCanvasStore.getState().updateNodeData(nodeId, data);
}

function exactRef(value: unknown): RemixArtifactRef | null {
  if (!value || typeof value !== 'object') return null;
  const ref = value as { artifactId?: unknown; revision?: unknown };
  return typeof ref.artifactId === 'string' && Number.isInteger(ref.revision) && Number(ref.revision) > 0
    ? { artifactId: ref.artifactId, revision: Number(ref.revision) } : null;
}

function sameRef(left: RemixArtifactRef | null, right: RemixArtifactRef | null): boolean {
  return left === null && right === null || Boolean(left && right && left.artifactId === right.artifactId && left.revision === right.revision);
}

function fingerprint(value: unknown): string {
  return JSON.stringify(value);
}

function stillCurrent(nodeId: string, operationId: string, topic: string, sourceRef: RemixArtifactRef | null, planSnapshot: string): boolean {
  const data = nodeData(nodeId);
  return data?.productionPlanGenerationOperationId === operationId
    && data.productionPlanGenerationTopic === topic
    && sameRef(exactRef(data.productionPlanGenerationBlueprintRef), sourceRef)
    && sameRef(exactRef(data.productionPlanBlueprintRef), sourceRef)
    && fingerprint(data.productionPlanDraft) === planSnapshot;
}

function fail(nodeId: string, operationId: string, message: string): void {
  if (nodeData(nodeId)?.productionPlanGenerationOperationId !== operationId) return;
  patch(nodeId, { productionPlanGenerationOperationId: null, productionPlanGenerationStatus: 'failed', productionPlanGenerationError: message });
}

function validPlan(value: Record<string, unknown>): ProductionPlan {
  if (value.schema_version !== 1 || typeof value.goal !== 'string' || !value.output || !Array.isArray(value.beats)) {
    throw new Error('AI 起草任务返回的作品方案格式无效。');
  }
  return value as unknown as ProductionPlan;
}

async function observe(nodeId: string, project: string, operationId: string, topic: string, sourceRef: RemixArtifactRef | null, planSnapshot: string): Promise<void> {
  const key = `${nodeId}:${operationId}`;
  if (observers.has(key)) return observers.get(key);
  const pending = (async () => {
    const deadline = Date.now() + POLL_TIMEOUT_MS;
    try {
      while (Date.now() < deadline) {
        if (nodeData(nodeId)?.productionPlanGenerationOperationId !== operationId) return;
        if (!stillCurrent(nodeId, operationId, topic, sourceRef, planSnapshot)) {
          fail(nodeId, operationId, '作品方案或来源已修改，请重新起草。');
          return;
        }
        const operation = await fetchRemixOperation(project, operationId);
        if (sourceRef && !operation.input_refs?.some((ref) => ref.artifact_id === sourceRef.artifactId && ref.revision === sourceRef.revision)) {
          throw new Error('AI 起草任务对应的来源版本不一致。');
        }
        if (['failed', 'cancelled', 'stale'].includes(operation.status)) throw new Error('作品方案起草失败，请在任务中心查看详情。');
        if (operation.status === 'needs_confirmation' || operation.status === 'completed') {
          const output = operation.output_refs.find((ref) => ref.artifact_id && Number.isInteger(ref.revision));
          if (!output?.artifact_id || !output.revision) throw new Error('作品方案起草已完成，但没有返回结果。');
          const artifact = await fetchRemixArtifact(project, { artifactId: output.artifact_id, revision: output.revision });
          if (artifact.schema !== 'production_plan.v1') throw new Error('AI 起草任务返回的作品方案类型无效。');
          if (sourceRef && !artifact.upstream_refs?.some((ref) => ref.artifact_id === sourceRef.artifactId && ref.revision === sourceRef.revision)) {
            throw new Error('AI 起草结果对应的来源版本不一致。');
          }
          if (!stillCurrent(nodeId, operationId, topic, sourceRef, planSnapshot)) {
            fail(nodeId, operationId, '作品方案或来源已修改，请重新起草。');
            return;
          }
          patch(nodeId, {
            productionPlanDraft: validPlan(artifact.payload), productionPlanDirty: false,
            productionPlanRef: { artifactId: artifact.id, revision: artifact.revision },
            productionPlanGenerationOperationId: null, productionPlanGenerationStatus: 'completed', productionPlanGenerationError: null,
          });
          return;
        }
        if (operation.task_type && operation.task_scope) {
          const task = await fetchRemixTaskStatus(project, operation.task_type, operation.task_scope);
          if (task?.status === 'failed') throw new Error(task.error || '作品方案起草失败，请重试。');
        }
        await new Promise((resolve) => window.setTimeout(resolve, POLL_INTERVAL_MS));
      }
      throw new Error('作品方案起草超时，请在任务中心查看状态。');
    } catch (cause) {
      fail(nodeId, operationId, cause instanceof Error ? cause.message : String(cause));
    } finally {
      observers.delete(key);
    }
  })();
  observers.set(key, pending);
  return pending;
}

export function resumeProductionPlanDraft(nodeId: string): void {
  const project = readUrl().project;
  const data = nodeData(nodeId);
  const operationId = data?.productionPlanGenerationOperationId;
  const topic = data?.productionPlanGenerationTopic;
  const snapshot = data?.productionPlanGenerationSnapshot;
  if (!project || typeof operationId !== 'string' || typeof topic !== 'string' || typeof snapshot !== 'string') return;
  void observe(nodeId, project, operationId, topic, exactRef(data?.productionPlanGenerationBlueprintRef), snapshot);
}

export function startProductionPlanDraft(
  nodeId: string, topic: string, blueprintRef: RemixArtifactRef | null, analysisContext: Record<string, unknown> | null,
): Promise<RemixTaskRef> {
  const active = submissions.get(nodeId);
  if (active) return active;
  const project = readUrl().project;
  const data = nodeData(nodeId);
  const cleanTopic = topic.trim();
  if (!project) return Promise.reject(new Error('请先打开项目画布'));
  if (!cleanTopic) return Promise.reject(new Error('请先填写作品目标'));
  if (typeof data?.productionPlanGenerationOperationId === 'string' && data.productionPlanGenerationOperationId) {
    resumeProductionPlanDraft(nodeId);
    const taskKey = data.productionPlanGenerationTaskKey;
    if (typeof taskKey === 'string') return Promise.resolve({
      task_key: taskKey, task_type: 'remix_generate_production_plan_draft', task_id: null,
      operation_id: data.productionPlanGenerationOperationId,
    });
  }
  const revision = Number(data?.productionPlanGenerationRevision ?? 0) + 1;
  const planSnapshot = fingerprint(data?.productionPlanDraft);
  patch(nodeId, {
    productionPlanGenerationRevision: revision, productionPlanGenerationTopic: cleanTopic,
    productionPlanGenerationBlueprintRef: blueprintRef, productionPlanGenerationSnapshot: planSnapshot,
    productionPlanGenerationStatus: 'submitting', productionPlanGenerationError: null,
  });
  const pending = (async () => {
    try {
      const task = await submitRemixProcessAction(project, {
        action: 'generate_production_plan_draft', topic: cleanTopic, blueprintRef, analysisContext,
        idempotencyKey: `production-plan:${nodeId}:${revision}:${crypto.randomUUID()}`, nodeRevision: revision,
      });
      if (nodeData(nodeId)?.productionPlanGenerationRevision !== revision) return task;
      patch(nodeId, {
        productionPlanGenerationOperationId: task.operation_id, productionPlanGenerationTaskKey: task.task_key,
        productionPlanGenerationStatus: 'running',
      });
      void observe(nodeId, project, task.operation_id, cleanTopic, blueprintRef, planSnapshot);
      return task;
    } catch (cause) {
      if (nodeData(nodeId)?.productionPlanGenerationRevision === revision) patch(nodeId, {
        productionPlanGenerationStatus: 'failed', productionPlanGenerationError: cause instanceof Error ? cause.message : String(cause),
      });
      throw cause;
    } finally {
      submissions.delete(nodeId);
    }
  })();
  submissions.set(nodeId, pending);
  return pending;
}
