# Remix B-roll Background Task Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run B-roll planning as a durable project task that appears in the task center, survives reloads, and applies output only to the exact saved script revision that started it.

**Architecture:** Register `plan_broll` in the existing remix action pipeline and publish its output from a `remix_plan_broll` runner. Move script-node orchestration into a focused frontend helper that submits the operation, persists its identity on the node, polls the existing operation/task APIs after submission or reload, and validates the exact script reference before applying the artifact.

**Tech Stack:** FastAPI, Pydantic, Python project task backend, RemixArtifactStore/RemixOperationStore, React, TypeScript, Vitest, pytest.

## Global Constraints

- Preserve the current synchronous `/broll-plans` endpoint for compatibility; the canvas must stop calling it.
- Use exact immutable `{artifact_id, revision}` script references at every backend boundary.
- Reuse `/freezone/remix/actions`, operation reconciliation, and project task state rather than creating a second task protocol.
- A result for an older script revision must never update a newer script revision.
- Existing completed plan references and script nodes without operation metadata require no migration.

---

### Task 1: Register B-roll Planning as a Project Action

**Files:**
- Modify: `src/novelvideo/api/routes/remix.py`
- Test: `tests/test_remix_tasks.py`

**Interfaces:**
- Consumes: `ProcessActionBody`, `_action_inputs`, `prepare_registered_action`, `enqueue_process_action`.
- Produces: action literal `plan_broll`, task type `remix_plan_broll`, task payload `{"script_ref": {"artifact_id": str, "revision": int}}`, default queue submission.

- [ ] **Step 1: Write a failing enqueue contract test**

Add a test that creates a `remix_script.v1` artifact, submits:

```python
response = api.post(
    "/api/v1/projects/demo/freezone/remix/actions",
    json={
        "action": "plan_broll",
        "payload": {"script_ref": {"artifact_id": script["id"], "revision": script["revision"]}},
        "idempotency_key": "broll-script-1-r1",
        "node_revision": 2,
    },
)
```

Assert status 200, response task type `remix_plan_broll`, default queue, exact `script_ref` in the queued payload, and the script reference in the operation input refs. Add rejection assertions for a missing revision and a non-`remix_script.v1` artifact.

- [ ] **Step 2: Run the enqueue contract test and verify RED**

Run: `uv run pytest tests/test_remix_tasks.py -k broll_planning_action -q`

Expected: FAIL because `plan_broll` is rejected by `ProcessActionBody` or is absent from `_PROCESS_TASK_TYPES`.

- [ ] **Step 3: Register and validate the action**

Change the action literal and registries to include:

```python
"plan_broll"

_PROCESS_TASK_TYPES = {
    # existing entries
    "plan_broll": "remix_plan_broll",
}

expected = {
    # existing entries
    "plan_broll": ({"script_ref"}, set()),
}
```

Keep `script_ref: remix_script.v1` validation in the shared `schema_by_ref` loop, so malformed, missing, cross-project, and wrong-schema references fail before enqueue.

- [ ] **Step 4: Run the focused backend contract test and verify GREEN**

Run: `uv run pytest tests/test_remix_tasks.py -k broll_planning_action -q`

Expected: PASS.

- [ ] **Step 5: Commit the registered action**

```bash
git add src/novelvideo/api/routes/remix.py tests/test_remix_tasks.py
git commit -m "feat(remix): queue broll planning actions"
```

### Task 2: Execute and Publish the B-roll Plan Artifact

**Files:**
- Modify: `src/novelvideo/task_backend/runners/remix.py`
- Test: `tests/test_remix_processing_tasks.py`

**Interfaces:**
- Consumes: task payload `script_ref`, `generate_broll_plan(script, username)`, `RemixArtifactStore`.
- Produces: `run_remix_plan_broll(envelope, ctx) -> {"status": "completed", "artifact_ref": exact_ref}` and an operation output reference.

- [ ] **Step 1: Write a failing runner test**

Create an exact saved script, monkeypatch the imported planner with:

```python
async def generate(*, script, username):
    assert script["dialogue"] == "新品登场"
    return {
        **script,
        "media_requirements": [
            {"event_id": "product", "required": True, "purpose": "展示产品", "suggested_query": "产品特写"}
        ],
    }
```

Start a real `RemixOperationStore` operation and call `run_remix_plan_broll` with its ID and the exact script reference. Assert the new artifact schema, upstream reference, payload, returned reference, and operation output reference.

- [ ] **Step 2: Run the runner test and verify RED**

Run: `uv run pytest tests/test_remix_processing_tasks.py -k broll_plan -q`

Expected: FAIL because `run_remix_plan_broll` and the runner registration do not exist.

- [ ] **Step 3: Implement the runner**

Import `asyncio` and `generate_broll_plan`, then add a synchronous task runner that:

```python
def run_remix_plan_broll(envelope: dict[str, Any], ctx: ProjectContext) -> dict[str, Any]:
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
    ref = payload.get("script_ref") if isinstance(payload.get("script_ref"), dict) else {}
    artifact_id, revision = ref.get("artifact_id"), ref.get("revision")
    if not isinstance(artifact_id, str) or type(revision) is not int or revision < 1:
        raise ValueError("B-roll planning script reference is incomplete")
    store = RemixArtifactStore(Path(ctx.output_dir).resolve(), ctx.project_id)
    script = store.read(artifact_id, revision)
    if script["schema"] != "remix_script.v1":
        raise ValueError("B-roll planning requires a remix script")
    planned = asyncio.run(generate_broll_plan(script=script["payload"], username=ctx.requester_username))
    artifact = store.create("remix_script.v1", planned, [{"artifact_id": artifact_id, "revision": revision}])
    output = {"artifact_id": artifact["id"], "revision": artifact["revision"]}
    _publish_operation_result(payload, ctx, output)
    return {"status": "completed", "artifact_ref": output}
```

Register the runner through `_with_operation_failure_transition` with `requires_home_node=True` and the default lane.

- [ ] **Step 4: Run runner and route tests**

Run: `uv run pytest tests/test_remix_processing_tasks.py -k broll_plan -q && uv run pytest tests/test_remix_tasks.py -k broll_planning_action -q`

Expected: PASS.

- [ ] **Step 5: Commit the runner**

```bash
git add src/novelvideo/task_backend/runners/remix.py tests/test_remix_processing_tasks.py
git commit -m "feat(remix): execute broll planning tasks"
```

### Task 3: Add a Reload-safe Frontend B-roll Task Controller

**Files:**
- Create: `frontend/src/features/canvas/remix/brollPlanTask.ts`
- Create: `frontend/src/features/canvas/remix/brollPlanTask.test.ts`
- Modify: `frontend/src/api/remix.ts`
- Modify: `frontend/src/features/canvas/nodes/ScriptNode.tsx`

**Interfaces:**
- Consumes: `submitRemixProcessAction`, `fetchRemixOperation`, `fetchRemixTaskStatus`, `fetchRemixArtifact`, canvas store node data.
- Produces: `startBrollPlan(nodeId: string): Promise<void>` and `resumeBrollPlan(nodeId: string): Promise<void>`.
- Persists: `remixBrollOperationId`, `remixBrollOperationScriptRef`, `remixBrollPlanStatus`, `remixBrollPlanError`, `remixBrollPlanRef`, `remixBrollScriptRef`, `remixBrollRequirements`.

- [ ] **Step 1: Extend the frontend action request type and write failing controller tests**

Add this request variant:

```ts
| { action: 'plan_broll'; scriptRef: RemixArtifactRef; idempotencyKey: string; nodeRevision: number }
```

In `submitRemixProcessAction`, encode it as `{ script_ref: exactRef(request.scriptRef) }`.

Write tests with a small in-memory canvas store fixture for these behaviors:

1. `startBrollPlan` submits `plan_broll`, then stores the returned operation ID and the exact source script reference.
2. `resumeBrollPlan` polls an existing operation after a simulated remount and applies its `remix_script.v1` output.
3. A failed project task clears the operation ID, sets `failed`, and preserves the error.
4. An output for script revision 1 is ignored when the node now references revision 2.

Use fake timers to advance the one-second polling interval. Assert submission includes a stable idempotency key containing node ID, artifact ID, and revision rather than `crypto.randomUUID()`.

- [ ] **Step 2: Run the controller test and verify RED**

Run: `cd frontend && pnpm exec vitest run src/features/canvas/remix/brollPlanTask.test.ts --maxWorkers=1`

Expected: FAIL because the controller functions do not exist.

- [ ] **Step 3: Implement the controller**

Create one module-level map to deduplicate active observers per node. `startBrollPlan` must:

```ts
const task = await submitRemixProcessAction(project, {
  action: 'plan_broll',
  scriptRef,
  idempotencyKey: `broll:${nodeId}:${scriptRef.artifactId}:${scriptRef.revision}`,
  nodeRevision: Number(node.data.nodeRevision ?? 0) + 1,
});
patch(nodeId, {
  remixBrollOperationId: task.operation_id,
  remixBrollOperationScriptRef: scriptRef,
  remixBrollPlanStatus: 'running',
  remixBrollPlanError: null,
});
await observe(nodeId, project, task.operation_id, scriptRef);
```

The observer must poll `fetchRemixOperation`, check task failure through `fetchRemixTaskStatus`, fetch the first exact artifact output, require schema `remix_script.v1`, and require both the stored operation source and current `remixScriptRef` to equal the initiating script reference before applying the result. Clear operation metadata on completed or failed terminal states. Leave reconciliation states observable rather than submitting a duplicate operation.

- [ ] **Step 4: Replace the browser-bound request in `ScriptNode`**

Remove `generateRemixBrollPlan`, `activeBrollPlans`, and the effect that converts every reloaded `running` flag into failure. Publish `startBrollPlan(id)` for the `plan_broll` action. Add a mount effect:

```ts
useEffect(() => {
  if (data.remixBrollPlanStatus === 'running' && data.remixBrollOperationId) {
    void resumeBrollPlan(id);
  }
}, [data.remixBrollOperationId, data.remixBrollPlanStatus, id]);
```

When a script draft changes or a new script version is saved, clear `remixBrollOperationId` and `remixBrollOperationScriptRef` along with the existing B-roll fields so an old observer cannot be treated as current.

- [ ] **Step 5: Run frontend tests and verify GREEN**

Run:

```bash
cd frontend
pnpm exec vitest run src/features/canvas/remix/brollPlanTask.test.ts src/api/remix.test.ts src/features/canvas/remix/RemixJourneyButton.test.tsx src/features/canvas/remix/remixJourney.test.ts src/features/canvas/remix/remixJourneyCanvas.integration.test.tsx --maxWorkers=1
pnpm exec tsc -b --pretty false
```

Expected: all selected tests PASS and TypeScript exits 0.

- [ ] **Step 6: Commit the frontend controller**

```bash
git add frontend/src/api/remix.ts frontend/src/api/remix.test.ts frontend/src/features/canvas/remix/brollPlanTask.ts frontend/src/features/canvas/remix/brollPlanTask.test.ts frontend/src/features/canvas/nodes/ScriptNode.tsx
git commit -m "feat(remix): resume broll planning tasks"
```

### Task 4: End-to-end Verification

**Files:**
- Modify only if verification exposes a contract defect.

**Interfaces:**
- Consumes: completed backend and frontend task pipeline.
- Produces: evidence that task-center visibility, reload recovery, completion, failure, and stale-result protection work together.

- [ ] **Step 1: Run the focused backend suite**

Run:

```bash
uv run pytest tests/test_remix_tasks.py tests/test_remix_processing_tasks.py tests/test_remix_script_draft.py -q
```

Expected: PASS.

- [ ] **Step 2: Run the focused frontend suite and typecheck**

Run the Task 3 verification commands again after all backend contracts are finalized.

Expected: PASS.

- [ ] **Step 3: Verify the live browser flow**

On the selected saved script node:

1. Click `规划 B-roll`.
2. Confirm the node shows disabled `等待 B-roll 规划` with a spinner.
3. Confirm task center lists `remix_plan_broll` as running.
4. Reload the page while it is running and confirm the waiting state resumes.
5. Confirm completion advances to either `补齐必需 B-roll` or the next valid journey step.
6. Use the frontend controller unit test's failed task response to verify `重新规划 B-roll` plus the worker error; do not alter the live provider configuration.

- [ ] **Step 4: Inspect the final diff and commit any verification-only fixes**

Run: `git diff --check && git status --short`

Expected: no whitespace errors and no unintended files.
