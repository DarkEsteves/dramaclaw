import { createRemixArtifact, fetchLatestRemixArtifact, fetchRemixArtifact, reviseRemixArtifact, type RemixArtifactRef } from '@/api/remix';
import { useCanvasStore } from '@/stores/canvasStore';
import { captureRegisteredFreezoneCanvasSession } from '@/features/freezone/canvasSyncRuntime';
import type { ProductionPlanV2 } from './productionPlanV2Model';
import { applyProductionPlanHistory, productionPlanHistoryAlreadyApplied, productionPlanHistoryEntry } from './productionPlanEditHistory';


const AUTO_SAVE_DELAY_MS = 450;

interface SaveQueue {
  key: string;
  timer: number | null;
  pending: boolean;
  running: Promise<void> | null;
}

const queues = new Map<string, SaveQueue>();
type CanvasSession = ReturnType<typeof captureRegisteredFreezoneCanvasSession>;

function queueFor(project: string, nodeId: string, session: CanvasSession): SaveQueue {
  const key = `${project}\u0000${session.id}\u0000${nodeId}`;
  let queue = queues.get(key);
  if (!queue) {
    queue = { key, timer: null, pending: false, running: null };
    queues.set(key, queue);
  }
  return queue;
}

function disposeExpiredQueue(queue: SaveQueue): void {
  if (queue.timer !== null) window.clearTimeout(queue.timer);
  queue.timer = null;
  queue.pending = false;
  if (queues.get(queue.key) === queue) queues.delete(queue.key);
}

function dataFor(nodeId: string) {
  return useCanvasStore.getState().nodes.find((node) => node.id === nodeId)?.data;
}

function sameRef(left: RemixArtifactRef | null, right: RemixArtifactRef | null): boolean {
  return left === null && right === null || Boolean(left && right
    && left.artifactId === right.artifactId && left.revision === right.revision);
}

function isRevisionConflict(cause: unknown): boolean {
  const error = cause as { response?: { status?: number }; status?: number };
  return error.response?.status === 409 || error.status === 409;
}

async function reviseWithOneSafeRebase(
  project: string, nodeId: string, session: CanvasSession, isCurrent: () => boolean,
  previous: RemixArtifactRef, payload: ProductionPlanV2, snapshot: string,
) {
  try {
    return { artifact: await reviseRemixArtifact(project, previous, payload as unknown as Record<string, unknown>, productionPlanUpstreamRefs(payload)), saved: payload };
  } catch (cause) {
    if (!isRevisionConflict(cause)) throw cause;
    const [base, latest] = await Promise.all([
      fetchRemixArtifact(project, previous), fetchLatestRemixArtifact(project, previous.artifactId),
    ]).catch(() => { throw cause; });
    assertCanvasCurrent(session, isCurrent);
    const current = dataFor(nodeId);
    if (!current || !sameRef((current.productionPlanV2Ref as RemixArtifactRef | null | undefined) ?? null, previous)
      || JSON.stringify(current.productionPlanV2Draft) !== snapshot
      || !base || !latest || base.schema !== 'production_plan.v2' || latest.schema !== 'production_plan.v2'
      || base.id !== previous.artifactId || latest.id !== previous.artifactId
      || base.revision !== previous.revision || latest.revision <= previous.revision) throw cause;
    let merged: ProductionPlanV2;
    const edits = productionPlanHistoryEntry(base.payload as unknown as ProductionPlanV2, payload);
    if (productionPlanHistoryAlreadyApplied(latest.payload as unknown as ProductionPlanV2, edits)) {
      return { artifact: latest, saved: latest.payload as unknown as ProductionPlanV2 };
    }
    try {
      merged = applyProductionPlanHistory(latest.payload as unknown as ProductionPlanV2, edits, 'redo');
    } catch {
      throw cause;
    }
    const artifact = await reviseRemixArtifact(project, { artifactId: latest.id, revision: latest.revision },
      merged as unknown as Record<string, unknown>, productionPlanUpstreamRefs(merged));
    return { artifact, saved: merged };
  }
}

/** Every accepted timing artifact must remain reachable from the saved plan revision. */
export function productionPlanUpstreamRefs(draft: ProductionPlanV2): RemixArtifactRef[] {
  const speechEvidence = draft.beats.flatMap((beat) => beat.turns.map((turn) => turn.speech_timing?.evidence_ref));
  const cueEvidence = draft.speech_cues?.map((cue) => cue.evidence_ref) ?? [];
  return [...new Map([draft.provenance.blueprint_ref, draft.provenance.source_plan_ref, draft.provenance.branch_source_ref, ...speechEvidence, ...cueEvidence]
    .filter((ref): ref is { artifact_id: string; revision: number } => Boolean(ref))
    .map((ref) => [`${ref.artifact_id}:${ref.revision}`, { artifactId: ref.artifact_id, revision: ref.revision }])).values()];
}

function assertCanvasCurrent(session: CanvasSession, isCurrent: () => boolean): void {
  if (!session.isCurrent() || !isCurrent()) throw new Error('作品方案所属画布已切换，未写入当前画布');
}

async function saveLatest(project: string, nodeId: string, session: CanvasSession, isCurrent: () => boolean): Promise<void> {
  assertCanvasCurrent(session, isCurrent);
  const data = dataFor(nodeId);
  if (!data?.productionPlanV2Dirty) return;
  const draft = data.productionPlanV2Draft as ProductionPlanV2 | undefined;
  if (!draft) return;
  if (data.productionPlanV2Conflict) throw new Error(String(data.productionPlanV2SaveError ?? '方案版本冲突，请先处理'));
  const payload = draft;
  const snapshot = JSON.stringify(draft);
  const previous = (data.productionPlanV2Ref as RemixArtifactRef | null | undefined) ?? null;
  try {
    const { artifact, saved } = previous
      ? await reviseWithOneSafeRebase(project, nodeId, session, isCurrent, previous, payload, snapshot)
      : { artifact: await createRemixArtifact(project, 'production_plan.v2', payload as unknown as Record<string, unknown>, productionPlanUpstreamRefs(payload)), saved: payload };
    assertCanvasCurrent(session, isCurrent);
    const current = dataFor(nodeId);
    if (!current || !sameRef((current.productionPlanV2Ref as RemixArtifactRef | null | undefined) ?? null, previous)) return;
    const unchanged = JSON.stringify(current.productionPlanV2Draft) === snapshot;
    const rebased = saved !== payload;
    assertCanvasCurrent(session, isCurrent);
    useCanvasStore.getState().updateNodeData(nodeId, {
      productionPlanV2Ref: { artifactId: artifact.id, revision: artifact.revision },
      ...(unchanged && rebased ? { productionPlanV2Draft: saved } : {}),
      productionPlanV2Dirty: !unchanged,
      productionPlanV2SaveError: !unchanged && rebased ? '保存期间方案又被修改，请核对后继续保存' : null,
      productionPlanV2Conflict: !unchanged && rebased,
    });
    const store = useCanvasStore.getState();
    for (const node of store.nodes) if (node.data.productionPlanNodeId === nodeId) {
      assertCanvasCurrent(session, isCurrent);
      store.updateNodeData(node.id, { productionPlanV2Ref: { artifactId: artifact.id, revision: artifact.revision } });
    }
  } catch (cause) {
    assertCanvasCurrent(session, isCurrent);
    const current = dataFor(nodeId);
    if (!current || !sameRef((current.productionPlanV2Ref as RemixArtifactRef | null | undefined) ?? null, previous)) return;
    useCanvasStore.getState().updateNodeData(nodeId, {
      productionPlanV2Dirty: true,
      productionPlanV2SaveError: cause instanceof Error ? cause.message : String(cause),
      productionPlanV2Conflict: isRevisionConflict(cause),
    });
    throw cause;
  }
}

/** Debounce edits, but serialize artifact revisions for one plan node. */
export function scheduleProductionPlanV2AutoSave(project: string, nodeId: string): void {
  const session = captureRegisteredFreezoneCanvasSession(project);
  if (!session.isCurrent()) return;
  const queue = queueFor(project, nodeId, session);
  queue.pending = true;
  if (queue.timer !== null) window.clearTimeout(queue.timer);
  queue.timer = window.setTimeout(() => {
    queue.timer = null;
    void flushScoped(project, nodeId, queue, session, () => true).catch(() => undefined);
  }, AUTO_SAVE_DELAY_MS);
}

/** Flush pending edits before a dependent action, or when leaving the editor. */
export async function flushProductionPlanV2AutoSave(project: string, nodeId: string, isCurrent: () => boolean = () => true): Promise<RemixArtifactRef | null> {
  const session = captureRegisteredFreezoneCanvasSession(project);
  return flushScoped(project, nodeId, queueFor(project, nodeId, session), session, isCurrent);
}

async function flushScoped(project: string, nodeId: string, queue: SaveQueue, session: CanvasSession, isCurrent: () => boolean): Promise<RemixArtifactRef | null> {
  if (!session.isCurrent()) disposeExpiredQueue(queue);
  assertCanvasCurrent(session, isCurrent);
  if (queue.timer !== null) window.clearTimeout(queue.timer);
  queue.timer = null;
  assertCanvasCurrent(session, isCurrent);
  if (dataFor(nodeId)?.productionPlanV2Dirty) queue.pending = true;
  if (!queue.running) {
    queue.running = (async () => {
      while (queue.pending) {
        queue.pending = false;
        await saveLatest(project, nodeId, session, isCurrent);
      }
    })().finally(() => {
      queue.running = null;
      if (!session.isCurrent()) disposeExpiredQueue(queue);
      else if (queue.timer === null && !queue.pending && queues.get(queue.key) === queue) queues.delete(queue.key);
    });
  }
  await queue.running;
  assertCanvasCurrent(session, isCurrent);
  return (dataFor(nodeId)?.productionPlanV2Ref as RemixArtifactRef | null | undefined) ?? null;
}
