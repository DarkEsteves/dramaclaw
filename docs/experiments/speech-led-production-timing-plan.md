# Speech-led production timing implementation plan

**Goal:** A bound speaking take determines its passage length, and later semantic placements follow the spoken performance without requiring users to resize every beat by hand.

**Architecture:** Keep `production_plan.v2` as author data. `duration_frames` remains the estimate used before a speaking source is ready and for wordless passages. For a speech-led beat, derive the effective duration from the selected source's playable media range and speed, then place following beats sequentially. Map aligned words inside that take and resolve selections, subtitles, pictures, graphics and sounds from their stable semantic anchors. Explicit `program_frame` anchors stay clock-based. The server remains authoritative; the client mirrors its projection for immediate feedback.

**Tech Stack:** Pydantic/Python timing compiler, React/TypeScript editor projection, pytest, Vitest.

## Global constraints

- Do not copy Hypit syntax, import Hypit Studio, or add HTML animation in this change.
- Do not truncate speech recognition or media to the estimated beat length.
- Preserve existing manually authored absolute clock anchors and wordless timing.
- Preserve the original video compose node, its edit/export controls, and saved source versioning.
- Respect variable output size, frame rate and duration; no new vertical/30 fps/60 s assumption.

## Tasks

### 1. Define effective speech duration and source range

**Files:** `src/novelvideo/freezone/remix/production_plan_v2.py`, `tests/test_production_plan_v2.py`, `frontend/src/features/canvas/compose/productionComposeModel.ts`, `frontend/src/features/canvas/compose/productionComposeModel.test.ts`.

- [x] Add a failing backend test: a 90-frame estimated beat with a 4.1-second speaking source at 30 fps resolves to about 123 frames, and the following beat starts at that edge.
- [x] Add the same failing client projection test.
- [x] Compute each selected, non-looping speaking source's playback span from the bound resource duration or an explicit source out point, source in point, and speed; use the beat estimate only when no usable speaking source exists. Preserve an explicit fixed beat mode for intentional clock-driven sections.
- [x] Run the focused tests and confirm backend and client frame counts match.

### 2. Map speech and dependent anchors after reflow

**Files:** the same compiler/projection files and tests, plus `frontend/src/features/canvas/remix/productionSpeechReadiness.ts`.

- [x] Add failing tests for a sentence with words ending after the old estimate: word anchors, subtitles and a later beat-relative visual resolve from actual media time; changing source duration moves later dependent items.
- [x] Rebuild beat, turn, word and placement anchors in that order, avoiding a cycle between speaking placement and its turn. Keep an intentionally absolute `program_frame` placement fixed.
- [x] Use the resolved source span in speech readiness and alignment status; a full-media alignment inside the bound source no longer reports “台词超出段落” solely because the estimate was short.
- [x] Run backend and client timing tests.

### 3. Upgrade authored beat-owned items and explain timing in the editor

**Files:** `src/novelvideo/freezone/remix/production_plan_v2.py`, `tests/test_production_plan_v2.py`, `frontend/src/features/canvas/nodes/ProductionPlanV2Editor.tsx`, `frontend/src/features/canvas/nodes/ProductionPlanV2Materials.tsx`, and relevant UI tests.

- [x] Make v1-to-v2 migration use beat-relative anchors for beat-owned visuals/audio/graphics instead of copied global frames; add a regression test.
- [x] For already saved migrated v2 plans, provide a repeatable, reviewable upgrade of matching beat-owned absolute anchors without rewriting arbitrary clock-locked placements; save through existing revision/autosave flow.
- [x] Show estimated and effective duration distinctly in the plan UI and remove the instruction to manually extend an estimated beat after full-media recognition.
- [x] Run focused UI/API tests and build.

### 4. Verify composition and lifecycle

**Files:** `tests/test_production_plan_v2.py`, existing compose tests, relevant documentation.

- [x] Assert compiled manifest, incomplete projection, and client projection agree on total duration and dependent start/end frames.
- [x] Verify wordless plans, explicit source trim, changed script/stale timing, manual absolute anchors, and non-integer frame rates.
- [x] Run focused pytest/Vitest suites, frontend build, and inspect diff for accidental edits to unrelated existing work.
- [x] Update the workbench design/status docs with the resulting timing authority and any limitation observed in verification.

## Verification and behavior delivered

- Backend: `uv run pytest tests/test_production_plan_v2.py tests/test_remix_render.py tests/test_api_remix_artifacts.py tests/test_remix_processing_tasks.py -q` — 106 passed.
- Frontend: six focused Vitest files covering projection, compose editor, plan editor, migration and readiness — 70 passed; `pnpm build` passed; `git diff --check` passed.
- The default automatic layout begins when the selected bound speaking take has current word timing and direct beat or turn anchors. The playable source range and speed determine that take's span. Subsequent beats, subtitles and semantic effects follow it. A beat with no usable aligned speaking take keeps its estimate; `timing_mode: fixed` preserves deliberate timing. Explicit program frame anchors remain fixed.
- Recognition still covers the full source media. The edit's source in/out points determine which words are usable in the compiled timeline. If a trim removes a spoken word, the editor asks for review and export does not silently drop that word.
- Previously saved v1-migrated v2 plans upgrade only matching generated cue IDs whose original absolute bounds lie inside their original beat. Other absolute anchors remain untouched.
