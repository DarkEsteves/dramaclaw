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
