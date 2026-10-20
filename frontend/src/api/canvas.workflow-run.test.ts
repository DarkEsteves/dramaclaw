import { expect, it, vi } from "vitest";

import { apiCall } from "./client";
import { createFreezoneWorkflowRun } from "./canvas";

vi.mock("./client", () => ({ apiCall: vi.fn(), apiCallEnvelope: vi.fn() }));

it("sends a bridge receipt only for a bridged workflow run", async () => {
  vi.mocked(apiCall).mockResolvedValue({ run_id: "run" });
  const actions = [{ node_id: "plan-a", action: "generate_production_plan_draft" }];

  await createFreezoneWorkflowRun("project", "canvas", actions, "attempt", "runner", "bridge.executor.token");
  expect(apiCall).toHaveBeenLastCalledWith(
    "projects/project/freezone/canvases/canvas/workflow-runs",
    { method: "POST", json: { actions, idempotency_key: "attempt", runner_id: "runner",
      bridge_key: "bridge.executor.token" } },
  );

  await createFreezoneWorkflowRun("project", "canvas", actions, "manual", "runner");
  expect(apiCall).toHaveBeenLastCalledWith(
    "projects/project/freezone/canvases/canvas/workflow-runs",
    { method: "POST", json: { actions, idempotency_key: "manual", runner_id: "runner" } },
  );
});
