# B-roll Planning Inside the Script Editor Implementation Plan

> **For agentic workers:** Execute inline in this session. Each behavior change starts with a failing test.

**Goal:** Let users plan and review B-roll beside script events, with safe saved-version gating and clear node status.

**Architecture:** Keep the existing `plan_broll` task and artifact contract. `ScriptNode` passes the current script and plan state to `RemixScriptPanel`; the editor owns the action and per-event decisions. The compact node shows a summary, while `remixJourney` respects user decisions about required supporting shots.

**Tech Stack:** React, TypeScript, Zustand, Vitest, React Testing Library.

## Global Constraints

- Keep visual controls aligned with `DESIGN.md` and existing canvas panel classes.
- Never submit planning against a draft whose current revision has not saved.
- Planning produces advice and suggested prompts; media creation stays in the event controls.
- Keep the canvas toolbar free of the B-roll planning button.

---

### Task 1: Move the action and show plan state

**Files:** `frontend/src/features/canvas/remix/RemixScriptPanel.tsx`, `frontend/src/features/canvas/nodes/ScriptNode.tsx`, `frontend/src/features/canvas/ui/NodeActionToolbar.tsx`, `frontend/src/features/canvas/remix/RemixScriptPanel.test.tsx`, `frontend/src/features/canvas/remix/remixJourneyCanvas.integration.test.tsx`.

- [x] Add tests that require the planning action inside the editor's event section, prohibit toolbar duplication, disable the action until the current draft is saved, and show running, failed, and completed states.
- [x] Run the focused Vitest files and confirm those assertions fail because the editor lacks the action.
- [x] Pass the saved script reference, plan status/error, and an action callback from `ScriptNode`; render the controls beside `台词事件`. Remove the toolbar action. Keep task submission in `startBrollPlan`.
- [x] Run the focused tests and confirm they pass.

### Task 2: Make AI advice reviewable and the card truthful

**Files:** `frontend/src/features/canvas/remix/RemixScriptPanel.tsx`, `frontend/src/features/canvas/nodes/ScriptNode.tsx`, `frontend/src/features/canvas/remix/remixJourney.ts`, their focused tests.

- [x] Add tests for a per-event user decision that can skip an AI-required B-roll without deleting the AI suggestion, and for the compact card to distinguish unplanned, running, failed, and completed states.
- [x] Run those tests and confirm they fail for the missing behavior.
- [x] Persist event decisions on the script node, clear them when script content changes, use them when checking missing required media, and show the effective decision beside each event.
- [x] Run the focused tests and confirm they pass.

### Task 3: Verify and commit

**Files:** All modified files.

- [x] Run the relevant frontend integration/unit tests, `pnpm exec tsc -b --pretty false`, and `git diff --check`.
- [x] Review the full diff against the confirmed design and commit the change.
