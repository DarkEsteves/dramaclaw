import {
  fetchRemixArtifact,
  fetchRemixOperation,
  fetchRemixTaskStatus,
  submitRemixProcessAction,
  type RemixArtifactRef,
} from '@/api/remix';
import { readUrl } from '@/lib/url-params';
import { useCanvasStore } from '@/stores/canvasStore';

const observers = new Map<string, Promise<void>>();
const submissions = new Map<string, Promise<void>>();
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

function fail(nodeId: string, operationId: string, message: string): void {
  const current = nodeData(nodeId);
  if (current?.remixBrollOperationId !== operationId) return;
  patch(nodeId, {
    remixBrollOperationId: null,
    remixBrollOperationScriptRef: null,
    remixBrollPlanStatus: 'failed',
    remixBrollPlanError: message,
  });
}

async function delay(): Promise<void> {
  await new Promise((resolve) => window.setTimeout(resolve, POLL_INTERVAL_MS));
}

async function observe(nodeId: string, project: string, operationId: string, sourceRef: RemixArtifactRef): Promise<void> {
  const key = `${nodeId}:${operationId}`;
  const currentObserver = observers.get(key);
  if (currentObserver) return currentObserver;

  const pending = (async () => {
    const deadline = Date.now() + POLL_TIMEOUT_MS;
    try {
      while (Date.now() < deadline) {
        const before = nodeData(nodeId);
        if (before?.remixBrollOperationId !== operationId) return;
        if (!sameRef(exactRef(before.remixScriptRef), sourceRef)
          || !sameRef(exactRef(before.remixBrollOperationScriptRef), sourceRef)) {
          fail(nodeId, operationId, '脚本已修改，请按当前版本重新规划 B-roll。');
          return;
        }

        const operation = await fetchRemixOperation(project, operationId);
        const operationMatches = operation.input_refs?.some(
          (ref) => ref.artifact_id === sourceRef.artifactId && ref.revision === sourceRef.revision,
        );
        if (!operationMatches) throw new Error('B-roll 任务对应的脚本版本不一致，请重新规划。');
        if (operation.status === 'failed' || operation.status === 'cancelled' || operation.status === 'stale') {
          throw new Error('B-roll 规划失败，请重新规划。');
        }
        if (operation.status === 'needs_confirmation' || operation.status === 'completed') {
          const output = operation.output_refs.find((ref) => typeof ref.artifact_id === 'string' && Number.isInteger(ref.revision));
          if (!output?.artifact_id || !output.revision) throw new Error('B-roll 规划完成，但没有返回规划产物。');
          const artifact = await fetchRemixArtifact(project, { artifactId: output.artifact_id, revision: output.revision });
          if (artifact.schema !== 'remix_script.v1') throw new Error('B-roll 规划产物格式错误。');
          const linked = artifact.upstream_refs?.some(
            (ref) => ref.artifact_id === sourceRef.artifactId && ref.revision === sourceRef.revision,
          );
          if (!linked) throw new Error('B-roll 规划产物对应的脚本版本不一致，请重新规划。');
          const latest = nodeData(nodeId);
          if (latest?.remixBrollOperationId !== operationId
            || !sameRef(exactRef(latest.remixScriptRef), sourceRef)
            || !sameRef(exactRef(latest.remixBrollOperationScriptRef), sourceRef)) return;
          patch(nodeId, {
            remixBrollOperationId: null,
            remixBrollOperationScriptRef: null,
            remixBrollPlanRef: { artifactId: artifact.id, revision: artifact.revision },
            remixBrollScriptRef: sourceRef,
            remixBrollRequirements: Array.isArray(artifact.payload.media_requirements) ? artifact.payload.media_requirements : [],
            remixBrollPlanStatus: 'completed',
            remixBrollPlanError: null,
          });
          return;
        }
        if (operation.task_type && operation.task_scope) {
          const task = await fetchRemixTaskStatus(project, operation.task_type, operation.task_scope);
          if (task?.status === 'failed') throw new Error(task.error || 'B-roll 规划失败，请重新规划。');
        }
        await delay();
      }
      throw new Error('B-roll 规划超时，请在任务中心检查状态。');
    } catch (cause) {
      fail(nodeId, operationId, cause instanceof Error ? cause.message : String(cause));
    } finally {
      observers.delete(key);
    }
  })();
  observers.set(key, pending);
  return pending;
}

async function submitBrollPlan(nodeId: string): Promise<void> {
  const project = readUrl().project;
  const data = nodeData(nodeId);
  const scriptRef = exactRef(data?.remixScriptRef);
  if (!project) throw new Error('请先打开项目画布');
  if (!scriptRef) throw new Error('请先保存改编脚本');
  if (typeof data?.remixBrollOperationId === 'string' && data.remixBrollOperationId) {
    return resumeBrollPlan(nodeId);
  }
  const nodeRevision = Number(data?.nodeRevision ?? 0) + 1;
  patch(nodeId, { remixBrollPlanStatus: 'running', remixBrollPlanError: null, nodeRevision });
  try {
    const task = await submitRemixProcessAction(project, {
      action: 'plan_broll',
      scriptRef,
      idempotencyKey: `broll:${nodeId}:${scriptRef.artifactId}:${scriptRef.revision}:${nodeRevision}`,
      nodeRevision,
    });
    const latest = nodeData(nodeId);
    if (!sameRef(exactRef(latest?.remixScriptRef), scriptRef)) {
      patch(nodeId, { remixBrollPlanStatus: 'failed', remixBrollPlanError: '脚本已修改，请按当前版本重新规划 B-roll。' });
      return;
    }
    patch(nodeId, {
      remixBrollOperationId: task.operation_id,
      remixBrollOperationScriptRef: scriptRef,
      remixBrollPlanStatus: 'running',
      remixBrollPlanError: null,
    });
    await observe(nodeId, project, task.operation_id, scriptRef);
  } catch (cause) {
    patch(nodeId, {
      remixBrollOperationId: null,
      remixBrollOperationScriptRef: null,
      remixBrollPlanStatus: 'failed',
      remixBrollPlanError: cause instanceof Error ? cause.message : String(cause),
    });
  }
}

export function startBrollPlan(nodeId: string): Promise<void> {
  const current = submissions.get(nodeId);
  if (current) return current;
  const submission = submitBrollPlan(nodeId);
  submissions.set(nodeId, submission);
  const clear = () => { if (submissions.get(nodeId) === submission) submissions.delete(nodeId); };
  void submission.then(clear, clear);
  return submission;
}

export async function resumeBrollPlan(nodeId: string): Promise<void> {
  const project = readUrl().project;
  const data = nodeData(nodeId);
  const operationId = typeof data?.remixBrollOperationId === 'string' ? data.remixBrollOperationId : '';
  const sourceRef = exactRef(data?.remixBrollOperationScriptRef);
  if (!project || !operationId || !sourceRef) return;
  if (!sameRef(exactRef(data?.remixScriptRef), sourceRef)) {
    fail(nodeId, operationId, '脚本已修改，请按当前版本重新规划 B-roll。');
    return;
  }
  await observe(nodeId, project, operationId, sourceRef);
}
