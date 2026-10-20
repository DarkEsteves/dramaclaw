import type { ProductionPlanV2 } from './productionPlanV2Model';

/** Order playback instances by authored dialogue; a shared instance cannot straddle another. */
export function orderPlanSpeechTakes(plan: ProductionPlanV2): void {
 const takes = plan.speech_takes ?? [];
 const ranks = new Map<string, number>();
 const turnRanks = new Map<string, number>();
 let rank = 0;
 for (const beat of plan.beats) {
  const own = takes.filter(take => take.beat_id === beat.id);
  const owners = new Map<string, string>();
  for (const take of own) for (const id of take.turn_ids) { if (owners.has(id)) throw new Error(`Speech turn ${id} belongs to multiple takes`); owners.set(id, take.id); }
  const seen = new Set<string>(); let previous: string | undefined;
  for (const turn of beat.turns) {
   turnRanks.set(turn.id, rank++);
   const owner = owners.get(turn.id); if (!owner) continue;
   if (owner !== previous && seen.has(owner)) throw new Error(`Speech take ${owner} is interleaved; assign separate playback instances`);
   if (!ranks.has(owner)) ranks.set(owner, turnRanks.get(turn.id)!);
   seen.add(owner); previous = owner;
  }
 }
 // All checks precede mutations, so callers can keep the previous author state on failure.
 for (const take of takes) take.turn_ids.sort((a,b) => (turnRanks.get(a) ?? Infinity) - (turnRanks.get(b) ?? Infinity));
 takes.sort((a,b) => (ranks.get(a.id) ?? Infinity) - (ranks.get(b.id) ?? Infinity));
}
