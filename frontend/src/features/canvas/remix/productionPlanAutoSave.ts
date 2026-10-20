import { createRemixArtifact, reviseRemixArtifact, type RemixArtifactRef } from '@/api/remix';
import { useCanvasStore } from '@/stores/canvasStore';
import type { ProductionPlan } from './productionPlanModel';
import { inspectPlanSchema } from './productionPlanReadiness';

const AUTO_SAVE_DELAY_MS = 450;

interface SaveQueue {
  timer: number | null;
  pending: boolean;
  running: Promise<void> | null;
}

const queues = new Map<string, SaveQueue>();

function queueFor(project: string, nodeId: string): SaveQueue {
  const key = `${project}:${nodeId}`;
  let queue = queues.get(key);
  if (!queue) {
    queue = { timer: null, pending: false, running: null };
    queues.set(key, queue);
  }
  return queue;
}

function dataFor(nodeId: string) {
  return useCanvasStore.getState().nodes.find((node) => node.id === nodeId)?.data;
}

function sameRef(left: RemixArtifactRef | null, right: RemixArtifactRef | null): boolean {
  return left === null && right === null || Boolean(left && right
    && left.artifactId === right.artifactId && left.revision === right.revision);
}

async function saveLatest(project: string, nodeId: string): Promise<void> {
  const data = dataFor(nodeId);
  if (!data?.productionPlanDirty) return;
  const draft = data.productionPlanDraft as ProductionPlan | undefined;
  if (!draft || inspectPlanSchema(draft).length) return;
  const payload = draft;
  const snapshot = JSON.stringify(draft);
  const previous = (data.productionPlanRef as RemixArtifactRef | null | undefined) ?? null;
  const blueprint = (data.productionPlanBlueprintRef as RemixArtifactRef | null | undefined) ?? null;
  const upstream = blueprint?.artifactId ? [blueprint] : [];
  try {
    const artifact = previous
      ? await reviseRemixArtifact(project, previous, payload as unknown as Record<string, unknown>, upstream)
      : await createRemixArtifact(project, 'production_plan.v1', payload as unknown as Record<string, unknown>, upstream);
    const current = dataFor(nodeId);
    if (!current || !sameRef((current.productionPlanRef as RemixArtifactRef | null | undefined) ?? null, previous)) return;
    const unchanged = JSON.stringify(current.productionPlanDraft) === snapshot;
    useCanvasStore.getState().updateNodeData(nodeId, {
      productionPlanRef: { artifactId: artifact.id, revision: artifact.revision },
      productionPlanDirty: !unchanged,
      productionPlanSaveError: null,
    });
  } catch (cause) {
    const current = dataFor(nodeId);
    if (!current || !sameRef((current.productionPlanRef as RemixArtifactRef | null | undefined) ?? null, previous)) return;
    useCanvasStore.getState().updateNodeData(nodeId, {
      productionPlanDirty: true,
      productionPlanSaveError: cause instanceof Error ? cause.message : String(cause),
    });
    throw cause;
  }
}

/** Debounce edits, but serialize artifact revisions for one plan node. */
export function scheduleProductionPlanAutoSave(project: string, nodeId: string): void {
  const queue = queueFor(project, nodeId);
  queue.pending = true;
  if (queue.timer !== null) window.clearTimeout(queue.timer);
  queue.timer = window.setTimeout(() => {
    queue.timer = null;
    void flushProductionPlanAutoSave(project, nodeId).catch(() => undefined);
  }, AUTO_SAVE_DELAY_MS);
}

/** Flush pending edits before a dependent action, or when leaving the editor. */
export async function flushProductionPlanAutoSave(project: string, nodeId: string): Promise<RemixArtifactRef | null> {
  const queue = queueFor(project, nodeId);
  if (queue.timer !== null) window.clearTimeout(queue.timer);
  queue.timer = null;
  if (dataFor(nodeId)?.productionPlanDirty) queue.pending = true;
  if (!queue.running) {
    queue.running = (async () => {
      while (queue.pending) {
        queue.pending = false;
        await saveLatest(project, nodeId);
      }
    })().finally(() => { queue.running = null; });
  }
  await queue.running;
  return (dataFor(nodeId)?.productionPlanRef as RemixArtifactRef | null | undefined) ?? null;
}
