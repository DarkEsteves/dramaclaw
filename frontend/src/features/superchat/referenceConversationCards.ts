import type { DirectorArtifactRef } from '@/api/referenceDirector';
import type { ChatMessage } from './types';

/** Snapshots supply identity only. Authorization and status come from the host read. */
export function productionCardRefs(messages: ChatMessage[], project: string, canvas: string, agent: string, commission: string): DirectorArtifactRef[] {
  const refs = new Map<string, DirectorArtifactRef>();
  for (const message of messages) {
    for (const event of [...(message.uiEvents ?? []), ...(message.parts ?? []).flatMap((part) => part.type === 'reference_production' ? [part.event] : [])]) {
      if (!event || typeof event !== 'object') continue;
      const row = event as Record<string, unknown>;
      if (row.type !== 'reference.production.card' || row.schema === 'material_work_package.v1'
        || row.schema === 'material_review.v1'
        || row.project_id !== project || row.canvas_id !== canvas || row.agent_id !== agent) continue;
      const owner = row.commission_ref as DirectorArtifactRef | undefined;
      const ref = row.ref as DirectorArtifactRef | undefined;
      if (owner?.artifact_id !== commission || !ref || typeof ref.artifact_id !== 'string' || !Number.isSafeInteger(ref.revision) || ref.revision < 1 || ref.project_id && ref.project_id !== project) continue;
      refs.set(ref.artifact_id, ref);
    }
  }
  return [...refs.values()];
}
