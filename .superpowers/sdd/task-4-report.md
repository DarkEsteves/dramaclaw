# Task 4 report — Script selections and actual-audio timeline

## Result

Implemented the complete Task 4 timing and selection closure.

- Script edits preserve a semantic selection ID only when its selected text has one unambiguous destination. Repeated or edited phrases stay visible as invalid and must be selected again. Unicode offsets remain code-point based.
- Timing compilation now records the exact script revision, aligned-word artifact revision, and final narration content hash. The runner derives these bindings from immutable artifact revisions.
- Crop and speed edit maps survive recompilation. Semantic offset, minimum duration, fixed start/end frames, and fixed-time conflicts are exposed in the existing timeline node without clearing manual values.
- Minimum duration is applied after source-to-output speed mapping, and linked event timing follows the output frame grid.
- Compose/export is blocked for unresolved required selections, fixed-time conflicts, obsolete narration takes, and legacy timing artifacts that do not identify their narration take.
- Replacing narration media immediately invalidates its connected alignment artifact and pending operation, so the script toolbar cannot keep pointing at an old alignment task. A cleared narration cannot reuse an old transcript.
- Narration identity comparison canonicalizes both the upload API's raw 64-character digest and timing artifacts' `sha256:<digest>` form, so the same production take is accepted while a different digest still blocks export.
- Fixed-frame edits require a complete, increasing start/end pair. Start-only, end-only, and reversed ranges remain in the controls and produce an inline error before any compile task is submitted.
- Clearing a narration's `audioUrl` blocks compose even if its former `remixMedia`, alignment, and timing nodes are still connected; the reference video's audio cannot silently mask the cleared selected take.
- The same cleared/replaced narration checks run when an existing compose node rebuilds its manifest. The persisted graph fingerprint now includes `audioUrl`, so clearing or replacing audio makes an already prepared manifest stale and blocks render submission.

## TDD evidence

New tests were first observed failing for:

- ambiguous repeated-word selection rebasing;
- exact alignment revision and narration hash capture;
- minimum-duration timing;
- manual crop/speed map preservation;
- unresolved/obsolete/unknown narration export blockers;
- narration replacement invalidating an in-flight or completed alignment.
- production-format media hash prefix normalization;
- start-only, end-only, and reversed fixed-frame edits.
- cleared narration with retained media and timing state.
- existing compose preparation and already prepared manifest export after narration clear/replace.

The narration-speed gate verifies a one-second semantic event moves from frames `30–60` at 1x to `15–30` at 2x at 30 fps.

## Verification

- `uv run pytest tests/test_remix_timing.py tests/test_remix_processing_tasks.py tests/test_remix_artifacts.py tests/test_remix_script_draft.py tests/test_remix_alignment.py tests/test_remix_render.py -q` — 77 passed.
- `npm test -- --run src/features/canvas/remix` — 178 passed.
- `npx tsc -b --pretty false` — passed.
- `uv run ruff check ...` for all changed Python modules/tests — passed.
- `git diff --check` — passed.

The Python run reports eight existing dependency deprecation warnings; no Task 4 test failures or new lint/type errors remain.
