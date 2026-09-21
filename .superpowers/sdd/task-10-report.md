# Task 10 report — precise invalidation, reuse and recovery

## Red evidence

- `uv run pytest tests/test_remix_operations.py tests/test_remix_result_confirmation.py -q`
  failed with 6 expected failures: missing media hash contract, supersession history,
  cancellation API, stale reason, and deleted output handling.
- `npm exec vitest run src/features/canvas/remix/spawnRemixDownstream.test.ts`
  failed with 4 expected failures because the invalidation planner did not exist.
- Follow-up red cycles proved unknown provider results could not reconcile, cancelled
  and superseded late results were discarded, render recovery could resubmit uncertain
  operations, and frontend recovery did not validate producer/media versions.

## Implementation

- Remix operations now snapshot exact input artifact refs, media hashes, producer
  version, node revision, canvas/node identity, task/provider IDs, and supersession.
- Same-key requests reconcile the durable operation. Provider-unknown and cancelled
  records are returned without a replacement submission. Explicit regeneration creates
  a linked successor while retaining the old record.
- Late cancelled/superseded outputs remain downloadable history with a stale reason;
  changed node/input results never backfill the current node. Missing output files
  invalidate reuse and confirmation.
- Canvas reload recovery accepts only active render operations matching the exact
  manifest revision, media hashes, producer version, and node revision.
- Minimal invalidation is explicit: style rerenders; a visual prompt regenerates that
  visual then rerenders; script or voice changes realign, recompile, then rerender.
- Existing workflow-run cancellation, idempotency, task proof, and restart recovery
  behavior was exercised unchanged by the full workflow recovery suites.

## Verification

- `uv run pytest tests/test_remix_operations.py tests/test_remix_result_confirmation.py tests/test_remix_tasks.py tests/test_freezone_workflow_runs.py tests/test_api_freezone_workflow_runs.py -q` — 218 passed.
- Focused frontend recovery/invalidation suites — 46 passed.
- `npx tsc -b --pretty false` — passed.
- Ruff on changed Python files — passed.
- `git diff --check` — passed.

The only emitted Python warnings are existing third-party Cognee/Pydantic deprecations.

## Independent review fixes

- Confirmation now fails closed on the exact durable canvas fingerprint/current flag,
  manifest ref, node revision, producer version, and every manifest media hash.
- Submission uses an atomic, expiring ownership lease, so simultaneous API requests
  enqueue once; enqueue failures release the lease for a safe retry.
- Style, visual prompt, script, and voice edit handlers now apply their minimal graph
  invalidation directly without invoking unrelated paid providers.
- Force regeneration, cancellation, failed outcome, and unknown provider outcome are
  production API states. The compose UI exposes cancel and force regenerate; registered
  task runners transition failures and timeouts durably.
- Review-fix verification: 234 Python tests and 62 frontend tests passed, plus TypeScript,
  Ruff, and diff checks.

## Final recovery race review

- Generated visual requests are now durable fingerprint inputs. Prompt/model/request
  changes version the visual request and mark its existing output stale; an older
  in-flight generation cannot overwrite the edited request. Manifest preparation is
  blocked until the replacement visual is generated and reattached.
- Result confirmation atomically claims the operation before checking and writing the
  canvas. Cancellation and timeout reconciliation cannot cross that claim, so a canvas
  can no longer show an applied result while its operation says cancelled or failed.
- Cancellation first claims the operation, cancels the exact bound project task through
  the configured task backend, then commits the terminal operation state. Late output is
  retained as stale history.
- `pending_reconciliation` is visible without relying on a generic error. The compose
  node offers both a durable task-status reconciliation action and explicit force
  regeneration.
- Added race coverage for confirm versus cancel, actual backend cancellation, terminal
  transition legality, provider-timeout reconciliation, and persistent visual prompt
  invalidation across recalculation.
- Final focused recovery/workflow verification: 241 Python tests passed; 52 focused
  frontend tests passed; TypeScript, Ruff, and diff checks passed. The repository-wide
  frontend run still has three pre-existing unrelated contract failures (Vietnamese
  locale parity, outline coverage for the two remix node types, and a transcript test
  that expects words rendered as one text node).

## Crash recovery and request-version review

- Every image generation input now passes through one durable mutation gate: prompt
  edits (including translation and mention remapping), model, style, ratio, size,
  quality, model parameters, camera, and reference attach/detach. It increments the
  request revision, marks the old output stale, clears only the obsolete task/busy
  handle, and invalidates downstream compose state.
- Image completion, fallback, background errors, foreground errors, album cleanup,
  waiter cleanup, and final busy-state cleanup all require both the in-memory attempt
  and durable request revision. A late result from an edited request cannot write an
  image/error or clear a newer request.
- Cancel transport errors, timeouts, and unconfirmed responses move to durable
  `pending_reconciliation`; they never reopen the prior pending state.
- `confirming` and `cancelling` carry durable claim times. Confirmation recovery checks
  the exact operation, render URL, and manifest ref already written on canvas before
  completing, otherwise an expired lease safely reopens confirmation. Cancellation
  recovery reads the exact bound project task and uses a single-owner expiring lease
  before any repeated backend cancellation.
- Final crash-recovery verification: 251 Python recovery/workflow tests and 69 focused
  frontend tests passed; TypeScript, Ruff, and diff checks passed.

## Final acceptance fixes

- Initial cancellation and restart reconciliation now use the same expiring owner
  lease. A durable cancellation intent serializes cancel/reconcile/publish/confirm;
  any late provider output is retained only as stale history and cannot become a
  confirmable current result.
- Image generation input invalidation is shared by forms and graph mutations. Style
  node selection, asset-library references, programmatic edge changes, manual connect,
  and manual edge removal all bump the durable visual request revision, clear the old
  task handle, and invalidate only reachable compose output. Late output and error from
  the prior revision are ignored.
- A repeated idempotency key whose prior output disappeared returns that original
  operation as `stale` with `output_unavailable`, `result_available=false`, and
  `output_reusable=false`. It never creates or enqueues replacement paid work; only the
  explicit regenerate path creates a linked successor.
- Final acceptance verification: 253 Python recovery/workflow tests and 78 focused
  frontend tests passed; TypeScript, Ruff, and diff checks passed.
