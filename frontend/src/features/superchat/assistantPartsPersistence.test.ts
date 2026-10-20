import { afterEach, expect, it, vi } from "vitest";
import { createAssistantPartsPersistence } from "./assistantPartsPersistence";

afterEach(() => vi.useRealTimers());

it("coalesces streamed updates and suppresses duplicate render writes", async () => {
  vi.useFakeTimers();
  const write = vi.fn(async (_key: string, _payload: unknown) => {});
  const queue = createAssistantPartsPersistence(write);
  queue.enqueue("turn-a", { text: "first" });
  for (let n = 0; n < 200; n++) queue.enqueue("turn-a", { text: `word-${n}` });
  queue.enqueue("turn-a", { text: "word-199" });
  await vi.advanceTimersByTimeAsync(500);
  expect(write).toHaveBeenCalledTimes(2);
  expect(write).toHaveBeenLastCalledWith("turn-a", { text: "word-199" });
  queue.enqueue("turn-a", { text: "word-199" });
  await vi.advanceTimersByTimeAsync(1000);
  expect(write).toHaveBeenCalledTimes(2);
});

it("serializes requests and flushes the final update with its original scope", async () => {
  vi.useFakeTimers();
  let finish!: () => void;
  const calls: Array<[string, unknown]> = [];
  const queue = createAssistantPartsPersistence(async (key, value) => {
    calls.push([key, value]);
    if (calls.length === 1) await new Promise<void>((resolve) => { finish = resolve; });
  });
  queue.enqueue("scope-a:turn", { text: "first" });
  queue.enqueue("scope-a:turn", { text: "final" });
  queue.enqueue("scope-b:turn", { text: "other canvas" });
  await vi.advanceTimersByTimeAsync(1000);
  expect(calls).toHaveLength(1);
  queue.flush();
  finish();
  await vi.advanceTimersByTimeAsync(0);
  expect(calls).toEqual([
    ["scope-a:turn", { text: "first" }],
    ["scope-a:turn", { text: "final" }],
    ["scope-b:turn", { text: "other canvas" }],
  ]);
});

it("allows retrying an identical snapshot after a failed request", async () => {
  vi.useFakeTimers();
  let failed = true;
  const write = vi.fn(async () => { if (failed) { failed = false; throw new Error("offline"); } });
  const queue = createAssistantPartsPersistence(write);
  queue.enqueue("turn", { text: "final" });
  await vi.advanceTimersByTimeAsync(500);
  queue.enqueue("turn", { text: "final" });
  await vi.advanceTimersByTimeAsync(500);
  expect(write).toHaveBeenCalledTimes(2);
});
