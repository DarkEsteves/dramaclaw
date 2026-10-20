import type { ProductionPlanV2 } from './productionPlanV2Model';

type Token = { field: string } | { id: string };
export interface PlanDifference { path: Token[]; label: string; local: string; remote: string }

const record = (value: unknown): value is Record<string, unknown> => Boolean(value) && typeof value === 'object' && !Array.isArray(value);
const identified = (value: unknown): value is Array<Record<string, unknown> & { id: string }> =>
  Array.isArray(value) && value.every((item) => record(item) && typeof item.id === 'string');
const display = (value: unknown): string => value === undefined ? '（无）' : JSON.stringify(value, null, 0)?.slice(0, 180) ?? '（无）';

/** Compare saved author fields by stable ID so reordering does not look like a rewrite. */
export function planDifferences(local: ProductionPlanV2, remote: ProductionPlanV2): PlanDifference[] {
  const differences: PlanDifference[] = [];
  const visit = (left: unknown, right: unknown, path: Token[], label: string) => {
    if (JSON.stringify(left) === JSON.stringify(right) || differences.length >= 100) return;
    if (record(left) && record(right)) {
      for (const key of new Set([...Object.keys(left), ...Object.keys(right)])) {
        if (key === 'schema_version') continue;
        visit(left[key], right[key], [...path, { field: key }], label ? `${label}.${key}` : key);
      }
      return;
    }
    if (identified(left) && identified(right)) {
      const localById = new Map(left.map((item) => [item.id, item]));
      const remoteById = new Map(right.map((item) => [item.id, item]));
      for (const id of new Set([...localById.keys(), ...remoteById.keys()])) {
        visit(localById.get(id), remoteById.get(id), [...path, { id }], `${label}[${id}]`);
      }
      return;
    }
    differences.push({ path, label, local: display(left), remote: display(right) });
  };
  visit(local, remote, [], '');
  return differences;
}

/** Accept one remote field or one stable-ID item while keeping all other local edits. */
export function acceptRemoteDifference(local: ProductionPlanV2, remote: ProductionPlanV2, path: Token[]): ProductionPlanV2 {
  const result = structuredClone(local) as unknown;
  let current: unknown = result;
  let source: unknown = remote;
  for (let index = 0; index < path.length; index++) {
    const token = path[index];
    const final = index === path.length - 1;
    if ('field' in token) {
      if (!record(current)) break;
      const next = record(source) ? source[token.field] : undefined;
      if (final) {
        if (next === undefined) delete current[token.field];
        else current[token.field] = structuredClone(next);
      } else {
        current = current[token.field]; source = next;
      }
    } else {
      if (!Array.isArray(current)) break;
      const targetIndex = current.findIndex((item) => record(item) && item.id === token.id);
      const next = Array.isArray(source) ? source.find((item) => record(item) && item.id === token.id) : undefined;
      if (final) {
        if (next === undefined && targetIndex >= 0) current.splice(targetIndex, 1);
        else if (next !== undefined && targetIndex >= 0) current[targetIndex] = structuredClone(next);
        else if (next !== undefined) current.push(structuredClone(next));
      } else {
        if (targetIndex < 0) break;
        current = current[targetIndex]; source = next;
      }
    }
  }
  return result as ProductionPlanV2;
}
