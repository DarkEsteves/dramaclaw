import type { ProductionPlanV2 } from './productionPlanV2Model';

type Segment = string | { id: string };
interface FieldChange { path: Segment[]; before: unknown; after: unknown; index?: number }
export interface ProductionPlanHistoryEntry { changes: FieldChange[] }

const object = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value);
const entityList = (value: unknown[]): value is Array<Record<string, unknown> & { id: string }> =>
  value.every((row) => object(row) && typeof row.id === 'string');
const copy = <T,>(value: T): T => value === undefined ? value : structuredClone(value);
const equal = (left: unknown, right: unknown): boolean => JSON.stringify(left) === JSON.stringify(right);

function differences(before: unknown, after: unknown, path: Segment[], changes: FieldChange[]): void {
  if (equal(before, after)) return;
  if (Array.isArray(before) && Array.isArray(after) && entityList(before) && entityList(after)) {
    const oldIds = before.map((row) => row.id);
    const newIds = after.map((row) => row.id);
    if (new Set(oldIds).size !== oldIds.length || new Set(newIds).size !== newIds.length) throw new Error('作品方案中存在重复对象 ID');
    if (oldIds.length === newIds.length && oldIds.every((id) => newIds.includes(id)) && !equal(oldIds, newIds)) {
      changes.push({ path: [...path, '$order'], before: oldIds, after: newIds });
    }
    for (const [index, row] of before.entries()) if (!newIds.includes(row.id)) {
      changes.push({ path: [...path, { id: row.id }], before: copy(row), after: undefined, index });
    }
    for (const [index, row] of after.entries()) {
      const previous = before.find((item) => item.id === row.id);
      if (!previous) changes.push({ path: [...path, { id: row.id }], before: undefined, after: copy(row), index });
      else differences(previous, row, [...path, { id: row.id }], changes);
    }
    return;
  }
  if (object(before) && object(after)) {
    for (const key of new Set([...Object.keys(before), ...Object.keys(after)])) {
      if (key !== 'id') differences(before[key], after[key], [...path, key], changes);
    }
    return;
  }
  changes.push({ path, before: copy(before), after: copy(after) });
}

/** Records only the fields touched by one local edit, never a whole plan snapshot. */
export function productionPlanHistoryEntry(before: ProductionPlanV2, after: ProductionPlanV2): ProductionPlanHistoryEntry {
  const changes: FieldChange[] = [];
  differences(before, after, [], changes);
  return { changes };
}

/** A lost save response can be reconciled only when every edited field is already present. */
export function productionPlanHistoryAlreadyApplied(current: ProductionPlanV2, entry: ProductionPlanHistoryEntry): boolean {
  return entry.changes.every((change) => parentAt(current, change.path.slice(0, -1)) !== undefined
    && equal(read(current, change.path), change.after));
}

function parentAt(root: unknown, path: Segment[]): unknown {
  let value = root;
  for (const segment of path) {
    if (typeof segment === 'string') value = object(value) ? value[segment] : undefined;
    else value = Array.isArray(value) ? value.find((row) => object(row) && row.id === segment.id) : undefined;
  }
  return value;
}

function read(root: unknown, path: Segment[]): unknown {
  const final = path[path.length - 1];
  const parent = parentAt(root, path.slice(0, -1));
  if (final === '$order') return Array.isArray(parent) ? parent.map((row) => row.id) : undefined;
  if (typeof final === 'string') return object(parent) ? parent[final] : undefined;
  return Array.isArray(parent) ? parent.find((row) => object(row) && row.id === final.id) : undefined;
}

function write(root: unknown, change: FieldChange, value: unknown): void {
  const path = change.path;
  const final = path[path.length - 1];
  const parent = parentAt(root, path.slice(0, -1));
  if (final === '$order' && Array.isArray(parent) && Array.isArray(value)) {
    const ordered = value.map((id) => parent.find((row) => object(row) && row.id === id));
    parent.splice(0, parent.length, ...ordered);
  } else if (typeof final === 'string' && object(parent)) {
    if (value === undefined) delete parent[final];
    else parent[final] = copy(value);
  } else if (typeof final !== 'string' && Array.isArray(parent)) {
    const index = parent.findIndex((row) => object(row) && row.id === final.id);
    if (value === undefined && index >= 0) parent.splice(index, 1);
    else if (value !== undefined && index < 0) parent.splice(Math.min(change.index ?? parent.length, parent.length), 0, copy(value));
    else if (value !== undefined) parent[index] = copy(value);
  } else throw new Error('作品方案结构已变化，无法撤销或重做');
}

/** A later edit of the same field blocks replay; edits elsewhere remain intact. */
export function applyProductionPlanHistory(
  current: ProductionPlanV2, entry: ProductionPlanHistoryEntry, direction: 'undo' | 'redo',
): ProductionPlanV2 {
  const next = structuredClone(current);
  const changes = direction === 'undo' ? [...entry.changes].reverse() : entry.changes;
  for (const change of changes) {
    const expected = direction === 'undo' ? change.after : change.before;
    if (!equal(read(next, change.path), expected)) throw new Error('作品方案对应字段已变化，无法撤销或重做');
    write(next, change, direction === 'undo' ? change.before : change.after);
  }
  return next;
}
