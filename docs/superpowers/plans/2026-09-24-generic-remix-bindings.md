# Generic Remix Bindings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make canvas remix reference use optional and generic, support one take across several dialogue turns, and remove prescribed next-step copy.

**Architecture:** Preserve `remix_script.v1` while adding ordered generic reference bindings and optional spoken ranges to each performance segment. Resolve legacy fields at the boundary. Keep media generation in image/video/audio nodes and compile the resulting takes into the existing semantic timeline.

**Tech Stack:** React, TypeScript, Vitest, Python 3.11, Pydantic, pytest.

## Global Constraints

- Do not change existing saved media or immutable artifact revisions.
- Do not add mandatory person, product, or setting fields.
- Keep visual styling consistent with `DESIGN.md`; do not change design tokens.
- Use existing image, video, audio, transcript, timeline, and compose nodes.
- Each behavior change begins with a failing focused test.

---

### Task 1: Generic binding contract and legacy adaptation

**Files:** `frontend/src/features/canvas/remix/remixScriptModel.ts`, `frontend/src/features/canvas/remix/scriptDraftTask.ts`, `src/novelvideo/freezone/remix/contracts.py`, related focused tests.

**Interfaces:** `RemixReferenceBinding = { nodeId: string; purpose: string }`; `referenceBindingsForSegment(segment)` returns explicit bindings when present, otherwise adapts the five legacy fields in their existing order. Saved payload uses `reference_bindings`.

- [ ] Add frontend tests that save and reload two arbitrary references, preserve legacy person/product/setting fields, and reject a blank purpose or duplicate node ID.
- [ ] Run `cd frontend && pnpm vitest run src/features/canvas/remix/remixScriptModel.test.ts src/features/canvas/remix/scriptDraftTask.test.ts`; verify the new cases fail for the missing generic property.
- [ ] Implement the binding type, normalization, serialization, and deserialization. Add the Pydantic binding contract with nonempty `node_id` and `purpose` and duplicate rejection.
- [ ] Run the focused frontend tests and `uv run pytest tests/test_remix_script_draft.py tests/test_remix_recipe_contract.py`; require zero failures.
- [ ] Commit the contract change.

### Task 2: Generic input creation and take construction

**Files:** `frontend/src/features/canvas/remix/takeCanvasBinding.ts`, `frontend/src/features/canvas/nodes/ScriptNode.tsx`, `frontend/src/features/canvas/remix/takeCanvasBinding.test.ts`.

**Interfaces:** `createPerformanceReferenceNode` creates an ordinary upload or image-generation node without a fixed identity category. `createPerformanceTakeNode` consumes `referenceBindingsForSegment`, sets stable `referenceOrder`, and writes each binding purpose into the prompt.

- [ ] Add a test in `takeCanvasBinding.test.ts` with a video reference and an audio reference whose purposes are custom strings. Assert both upstream edges, stable order, model reference mode, and absence of an unconditional product/scene replacement sentence. Add a legacy reference test.
- [ ] Run that test and verify the new assertions fail.
- [ ] Implement generic reference creation and take prompt construction. Reference purpose is a prompt instruction, while input modality comes from its canvas node.
- [ ] Run `cd frontend && pnpm vitest run src/features/canvas/remix/takeCanvasBinding.test.ts`; require zero failures.
- [ ] Commit the binding-to-take change.

### Task 3: Optional reference editor and bulk application

**Files:** `frontend/src/features/canvas/remix/RemixScriptPanel.tsx`, a focused `SegmentReferenceEditor.tsx`, `frontend/src/features/canvas/nodes/ScriptNode.tsx`, focused component tests.

**Interfaces:** `SegmentReferenceEditor` receives current bindings, all usable media nodes, and `onChange`. Batch application targets an explicit set of segment IDs, preserving unrelated segment bindings and invalidating only affected take selections.

- [ ] Add component tests for no reference selected, add/remove, custom purpose, mixed image/video/audio choices, explicit batch target selection, and legacy data display.
- [ ] Run the component tests and confirm failures for the old three-column editor.
- [ ] Implement the compact list and batch target picker. Keep “create upload node” and “create image node” shortcuts; actual upload/generation remains in those nodes.
- [ ] Run `cd frontend && pnpm vitest run src/features/canvas/remix/RemixScriptPanel.test.tsx src/features/canvas/remix/SegmentReferenceEditor.test.tsx`; require zero failures.
- [ ] Commit the editor change.

### Task 4: One take for multiple spoken turns

**Files:** `frontend/src/features/canvas/remix/remixScriptModel.ts`, `frontend/src/features/canvas/remix/RemixScriptPanel.tsx`, `src/novelvideo/freezone/remix/contracts.py`, `src/novelvideo/freezone/remix/take_timing.py`, `src/novelvideo/task_backend/runners/remix.py`, focused tests.

**Interfaces:** A segment may carry `speechRanges: [{start, end}]` / `speech_ranges: [{start_char, end_char}]`. `spokenTextForSegment` joins only those ranges with newline. The timing compiler maps each aligned word back to the corresponding source range; legacy segments use the outer range.

- [ ] Add a Python test with `男：你好\n女：再见` where one take aligns `你好\n再见`, proving semantic events on both turns map to different frames. Add invalid range and legacy tests. Add a UI test for merging adjacent turns into one take.
- [ ] Run `uv run pytest tests/test_remix_take_timing.py` and the focused UI test; verify the new cases fail for missing range handling.
- [ ] Implement range validation, text projection, alignment runner input, global offset projection, and the merge control. Keep the original speaker labels in the script only.
- [ ] Run the focused tests plus `uv run pytest tests/test_remix_processing_tasks.py tests/test_remix_tasks.py`; require zero failures.
- [ ] Commit the multi-turn take change.

### Task 5: Node-local status and end-to-end verification

**Files:** `frontend/src/features/canvas/nodes/ScriptNode.tsx`, `frontend/src/features/canvas/remix/remixJourney.ts` if remaining callers require it, relevant integration tests.

**Interfaces:** The script node shows saved revision and counts of events and bound takes. No single “next step” appears in its preview; other nodes retain their own operations.

- [ ] Add an integration assertion that a saved script with no product reference and an independent video take displays factual status and can compile.
- [ ] Run the focused integration test and verify failure on the existing next-step label.
- [ ] Replace the status derivation while preserving existing node actions and preflight diagnostics.
- [ ] Run frontend typecheck, focused remix tests, backend remix tests, and a production frontend build. Inspect the edited panel in both themes. Verify `git diff --check`.
- [ ] Commit the status and verification change.

## Self-review

- Every feature in the design has a task: optional refs (1–3), multi-turn takes (4), factual status (5), compatibility (1 and 4), validation (1, 4, 5).
- No database migration is needed: persisted v1 objects gain optional fields and older revisions remain readable.
- Reference order, media node modality, and model capability validation remain separate responsibilities.
