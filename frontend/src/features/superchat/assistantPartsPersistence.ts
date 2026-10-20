// Keep live rendering immediate, but persist only the latest snapshot in a burst.
// One request at a time also prevents slower responses from reordering snapshots.
export function createAssistantPartsPersistence<T>(write: (key: string, payload: T) => Promise<void>) {
  type Snapshot = { payload: T; serialized: string };
  const pending = new Map<string, Snapshot>();
  const saved = new Map<string, string>();
  let active: { key: string; snapshot: Snapshot } | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let nextWriteAt = 0;
  let flushing = false;

  const pump = () => {
    if (active) return;
    if (!pending.size) { flushing = false; return; }
    const delay = flushing ? 0 : Math.max(0, nextWriteAt - Date.now());
    if (delay) {
      if (timer === undefined) timer = setTimeout(() => { timer = undefined; pump(); }, delay);
      return;
    }
    const [key, snapshot] = pending.entries().next().value!;
    pending.delete(key);
    if (saved.get(key) === snapshot.serialized) { pump(); return; }
    active = { key, snapshot };
    void write(key, snapshot.payload).then(() => {
      saved.delete(key);
      saved.set(key, snapshot.serialized);
      if (saved.size > 128) saved.delete(saved.keys().next().value!);
    }).catch(() => undefined).finally(() => {
      active = undefined;
      nextWriteAt = Date.now() + 500;
      pump();
    });
  };

  return {
    enqueue(key: string, payload: T) {
      const serialized = JSON.stringify(payload);
      const latest = pending.get(key)?.serialized
        ?? (active?.key === key ? active.snapshot.serialized : saved.get(key));
      if (latest === serialized) return;
      pending.set(key, { payload, serialized });
      pump();
    },
    flush() {
      flushing = true;
      if (timer !== undefined) { clearTimeout(timer); timer = undefined; }
      pump();
    },
  };
}
