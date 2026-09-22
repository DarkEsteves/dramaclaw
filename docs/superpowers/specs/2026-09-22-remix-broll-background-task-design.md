# Remix B-roll Background Task Design

## Goal

Move B-roll planning from a browser-bound HTTP request into the existing remix background-task system. The operation must appear in the task center, survive reloads, resume from persisted state, and never publish a result for an outdated script revision.

## Current Problem

`POST /freezone/remix/broll-plans` waits for model generation and artifact publication before returning. The selected script node stores only a local `running` flag while that request is active. The request is invisible to the task center, and a reload or frontend restart loses the in-memory promise while leaving the node looking busy.

## Chosen Design

Reuse the registered remix action and operation pipeline already used by reference analysis, transcription, alignment, and semantic timeline compilation.

### Backend

Add `plan_broll` to the registered processing actions and map it to a `remix_plan_broll` project task on the default queue.

The enqueue endpoint accepts:

- An exact immutable `script_ref` with artifact ID and revision.
- An idempotency key derived from the canvas node and exact script revision.
- The current node revision.

Action preparation validates that the reference exists and uses the `remix_script.v1` schema. The operation records the exact script input reference. The task runner reads that revision, calls the existing validated B-roll planner, publishes the resulting `remix_script.v1` artifact with the source script as its upstream reference, and completes the operation with the new artifact reference.

The existing synchronous B-roll endpoint may remain temporarily for compatibility, but the canvas workflow no longer calls it.

### Frontend

When the user starts B-roll planning, the script node submits `plan_broll` through `submitRemixProcessAction` and persists the returned operation ID, task ID, task type, task scope, script reference, and running state.

The node observes the persisted operation using the same operation and task-status APIs as other remix actions. This observation runs after reload as well as immediately after submission.

On completion, the node:

1. Verifies that the operation input reference still equals the node's currently saved script reference.
2. Reads the output artifact.
3. Stores the B-roll plan reference, its source script reference, and the validated media requirements.
4. Clears the active operation fields and marks planning complete.

If the script changed, the stale operation result is ignored. If the operation or task fails, the node records the error and exposes `重新规划 B-roll`.

### Task Center and Recovery

Because `remix_plan_broll` is a normal project task, it appears in the existing task center without a separate progress UI. Reloading the page does not cancel it. The node resumes observation from its stored operation ID and eventually resolves to completed, failed, or reconciliation-required state.

### Idempotency and Concurrency

- Repeated submission for the same node revision and exact script revision returns the same operation.
- A newer saved script produces a new idempotency key and invalidates the older result.
- Output publication is tied to the immutable source script revision.
- The frontend applies an output only when the operation and current script still match.

## Error Handling

- Validation errors are returned before enqueueing.
- Enqueue uncertainty uses the existing operation reconciliation path.
- Worker failures transition the operation and project task to failed with a user-visible reason.
- A missing or malformed output reference is treated as a failed planning result rather than leaving the node in a waiting state.

## Compatibility

Existing saved script nodes without operation fields continue to work. Their next action remains `规划 B-roll`. Existing completed B-roll plan references keep their current behavior. No migration is required.

## Tests

Backend tests cover action registration, exact script validation, queue submission, runner publication, operation output references, and idempotent replay.

Frontend tests cover submission metadata, reload recovery, completion, worker failure, stale-script rejection, and task-center compatible task metadata. Existing remix journey tests continue to verify button labels and downstream gating.

## Acceptance Criteria

- Starting B-roll planning creates a visible background task.
- The canvas remains usable while planning runs.
- Reloading during planning preserves and resumes the state.
- Completion automatically advances the script node to the next valid remix step.
- Failure shows `重新规划 B-roll` and an actionable error.
- An older task can never overwrite planning for a newer script revision.
