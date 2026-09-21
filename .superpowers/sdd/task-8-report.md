# Task 8 implementation report

## Result

Registered deterministic remix processing is now part of the durable Workflow run path. The stable contract, generated Python/TypeScript/MCP artifacts, plan compiler, action API, task envelope, dependency dispatch, cancellation identity, and artifact proof all use the same five actions:

- `analyze_reference_video`
- `transcribe_media`
- `align_script`
- `compile_semantic_timeline`
- `render_manifest`

## TDD evidence

The first focused run failed because Workflow left `analyze_reference_video` pending and accepted an extra client `command` field. The artifact-proof test then failed because processing tasks were treated as having no required durable result. The dependency-order test exposed an operation-identity collision with Recipe billing settlement; processing operations now have a separate `processing_operation_id`.

The review-fix tests first demonstrated four additional gaps: an unresolved timing result was marked complete, a task result could complete without its bound operation and exact artifact schema, semantic compilation dropped the narration and blueprint identities, and deterministic processing could enqueue before a later Recipe admission failed. A further task-identity test proved that an output from a different task was accepted before the operation/task check was tightened.

## Implementation

- Added action roles for reference analysis and manifest rendering to `stable-contract.json`; regenerated Python, TypeScript and MCP contract outputs together.
- Made process descriptor compilation fail closed for unregistered processing actions and require each unique typed role.
- Added exact payload preflight for every processing action. Project-relative media is hashed before admission; artifact inputs require an exact project-scoped artifact revision.
- Added generic `render_manifest` action preparation using the existing render task and FFmpeg queue.
- Persisted processing payloads, node revisions, dependency node IDs, processing operation identity and durable result refs in Workflow v1 run actions, including migration of existing SQLite databases.
- Added dependency validation (unknown/self/cycle rejection) and dispatch of only the ready frontier. Poll/reconciliation advances the next frontier after durable task completion.
- Included workflow run/node IDs in task envelopes and retained task keys for existing cancellation handling.
- Kept deterministic processing outside Recipe admission/billing. Model Recipe actions retain their existing usage reservation path.
- Required durable proof: exact remix artifact revision for analysis/transcription/alignment/timing, and an existing non-empty project-local MP4 for rendering.
- Rejected unknown actions and client shell-style actions at run creation.
- Kept `needs_review` timing results blocked so downstream rendering cannot start until review is resolved.
- Reconciled processing only when the task ID, immutable operation envelope, exact normalized payload, exact input refs, operation output and action-specific output schema all agree.
- Preserved semantic compilation's exact narration file/hash, script revision, aligned-word revision and reference-blueprint revision through the client request, API preflight, operation record and runner artifact lineage.
- Completed all Recipe admissions for a mixed run before enqueueing any deterministic processing task, so a failed metered admission leaves no processing work in flight.

## Verification

- `uv run pytest tests/test_workflow_stable_contract.py tests/test_workflow_plan.py tests/test_workflow_preflight.py tests/test_workflow_transactions.py tests/test_freezone_workflow_runs.py tests/test_api_freezone_workflow_runs.py tests/test_remix_processing_tasks.py -q`
  - 350 passed; 8 existing dependency deprecation warnings.
- `npx vitest run src/features/canvas/remix/semanticTimelineModel.test.ts src/features/canvas/remix/semanticTimelineNode.test.tsx`
  - 14 passed.
- `uv run python scripts/generate_workflow_contract.py --check`
- `uv run ruff check ...` on every changed Python/test file
- `npx tsc -b --pretty false`
- `git diff --check`
