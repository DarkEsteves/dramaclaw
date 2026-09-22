# Script-Owned Remix Settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the two-dialog remix flow with one script-node editor in which every derived script branch owns an independent copy of its adaptation rules, blueprint artifact, dialogue, and semantic ranges.

**Architecture:** The story node remains the source of reviewed reference events and evidence. `createScriptFromBlueprint` creates a connected script node immediately and seeds that node with a copy of the story's adaptation defaults while leaving its branch blueprint reference empty. The script editor embeds `ReferenceBlueprintPanel`, saves a new branch blueprint before generation or script save, and records the exact branch blueprint reference on the script node.

**Tech Stack:** React 19, TypeScript, Zustand canvas store, React Flow, Vitest, Testing Library, Tailwind CSS.

## Global Constraints

- Keep the existing canvas node and dialog visual system from `DESIGN.md`.
- Every click on “生成改编脚本” creates a fresh connected script node.
- Editing one script branch must not mutate the story node or another script branch.
- Reference evidence and reviewed events remain owned by the story node.
- Write a failing regression test before each production behavior change.

---

### Task 1: Seed Every Derived Script Branch

**Files:**
- Modify: `frontend/src/features/canvas/remix/createScriptFromBlueprint.ts`
- Modify: `frontend/src/features/canvas/remix/createScriptFromBlueprint.test.ts`

**Interfaces:**
- Consumes: `createScriptFromBlueprint(storyId: string): string`
- Produces: a new script node containing copied `blueprintNarrativeRole`, `blueprintPreserveRule`, `blueprintReplaceRule`, `referenceEvidenceRef`, `sourceMediaPath`, and `remixBlueprintSeedRef`, with no saved branch blueprint reference.

- [ ] **Step 1: Write the failing branch ownership test**

Assert that two calls create two script nodes, both inherit the story defaults, neither receives the story's saved blueprint as its own `referenceBlueprintRef`, and editing one node's copied rule does not change the other node or story.

- [ ] **Step 2: Run the focused test and verify RED**

Run: `pnpm exec vitest run src/features/canvas/remix/createScriptFromBlueprint.test.ts --reporter=dot`

Expected: FAIL because the new script node currently receives no branch settings.

- [ ] **Step 3: Seed the child node data**

Pass the copied defaults and evidence metadata as the third argument to `store.addNode`. Store the parent's saved blueprint only as `remixBlueprintSeedRef`; keep `referenceBlueprintRef` and `remixBlueprintRef` null so the branch creates its own artifact.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run: `pnpm exec vitest run src/features/canvas/remix/createScriptFromBlueprint.test.ts --reporter=dot`

Expected: PASS.

### Task 2: Open the Script Editor Directly

**Files:**
- Modify: `frontend/src/features/canvas/nodes/VideoStoryNode.tsx`
- Modify: `frontend/src/features/canvas/remix/storyScriptEntry.test.tsx`

**Interfaces:**
- Consumes: canvas event `remix-node/action` with action `continue_story`
- Produces: one immediate call to `createScriptFromBlueprint(id)` and no story-owned adaptation dialog.

- [ ] **Step 1: Replace the existing dialog expectation with a failing direct-spawn test**

Publish `continue_story`, assert a script node is created immediately, and assert no dialog named “改编脚本设置” exists.

- [ ] **Step 2: Run the focused test and verify RED**

Run: `pnpm exec vitest run src/features/canvas/remix/storyScriptEntry.test.tsx --reporter=dot`

Expected: FAIL because the story node still opens the preliminary settings dialog.

- [ ] **Step 3: Remove the preliminary dialog**

Delete the story-owned `ReferenceBlueprintPanel`, modal state, save handler, and portal. Make `continue_story` call `createScriptFromBlueprint(id)` directly and report creation errors on the story node.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run: `pnpm exec vitest run src/features/canvas/remix/storyScriptEntry.test.tsx --reporter=dot`

Expected: PASS.

### Task 3: Save a Branch Blueprint Inside the Script Dialog

**Files:**
- Modify: `frontend/src/features/canvas/remix/blueprintPanel.tsx`
- Modify: `frontend/src/features/canvas/remix/blueprintPanel.test.tsx`
- Modify: `frontend/src/features/canvas/remix/RemixScriptPanel.tsx`
- Modify: `frontend/src/features/canvas/remix/RemixScriptPanel.test.tsx`
- Modify: `frontend/src/features/canvas/nodes/ScriptNode.tsx`

**Interfaces:**
- Add to `ReferenceBlueprintPanel`: optional `seedRef?: RemixArtifactRef | null`, used only to restore initial form values.
- Add to `RemixScriptPanel`: `blueprintSettings: ReactNode`, `onEnsureBlueprint: () => Promise<RemixArtifactRef>`, and `onSave(ref, blueprintRef)`.
- The branch's `referenceBlueprintRef` is created before “按蓝图生成” or “保存脚本版本”.

- [ ] **Step 1: Write failing seed restoration and save-order tests**

Verify that `seedRef` populates empty branch fields without becoming the saved branch reference. Verify that clicking “按蓝图生成” calls `onEnsureBlueprint` before `generateRemixScriptDraft`, and saving calls it before `saveRemixScriptArtifact`.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `pnpm exec vitest run src/features/canvas/remix/blueprintPanel.test.tsx src/features/canvas/remix/RemixScriptPanel.test.tsx --reporter=dot`

Expected: FAIL because the seed and ensure interfaces do not exist.

- [ ] **Step 3: Implement seed-only restoration**

Load `draft.referenceBlueprintRef ?? seedRef`, restore missing rule values, and never write `seedRef` into `draft.referenceBlueprintRef`.

- [ ] **Step 4: Make script generation and save ensure the branch blueprint**

Render `blueprintSettings` as the first section of the script dialog. Await `onEnsureBlueprint()` in both generation and save paths, use its returned reference for the API call, and return that exact reference through `onSave`.

- [ ] **Step 5: Wire ScriptNode to the reviewed story source**

Find the connected story node, compute its `BlueprintSources`, pass its rows and reviewed events to `ReferenceBlueprintPanel`, map `onPatch` to the script node data, and prefer the branch's `referenceBlueprintRef` over the story seed. Save the confirmed reference as `remixBlueprintRef` with the script artifact.

- [ ] **Step 6: Run the focused tests and verify GREEN**

Run: `pnpm exec vitest run src/features/canvas/remix/blueprintPanel.test.tsx src/features/canvas/remix/RemixScriptPanel.test.tsx --reporter=dot`

Expected: PASS.

### Task 4: Verify the Single-Dialog Journey

**Files:**
- Modify: `frontend/src/features/canvas/remix/remixJourneyCanvas.integration.test.tsx`
- Modify: `frontend/src/__tests__/features/canvas/remix-nodes.test.tsx`

**Interfaces:**
- Produces: an end-to-end canvas contract from story toolbar click to a selected connected script node with one open “改编脚本与语义区间” dialog.

- [ ] **Step 1: Write the failing journey assertion**

Assert that the toolbar click creates the script node, opens only “改编脚本与语义区间”, shows inherited adaptation rules there, and creates a distinct blueprint reference when generating.

- [ ] **Step 2: Run the integration tests and verify RED**

Run: `pnpm exec vitest run src/features/canvas/remix/remixJourneyCanvas.integration.test.tsx src/__tests__/features/canvas/remix-nodes.test.tsx --reporter=dot`

Expected: FAIL until the complete single-dialog wiring is present.

- [ ] **Step 3: Finish dialog layout and compatibility fixes**

Keep the existing script editor controls, add the adaptation rules above “新主题”, remove duplicate blueprint-role presentation, and preserve saved older script nodes by falling back to their upstream story blueprint when no branch-owned reference exists.

- [ ] **Step 4: Run focused integration and type checks**

Run: `pnpm exec vitest run src/features/canvas/remix/remixJourneyCanvas.integration.test.tsx src/__tests__/features/canvas/remix-nodes.test.tsx --reporter=dot`

Run: `pnpm exec tsc -b --pretty false`

Expected: PASS with no TypeScript errors.

- [ ] **Step 5: Run project verification**

Run: `pnpm exec vitest run --reporter=dot`

Run: `npx @google/design.md lint DESIGN.md`

Expected: all frontend tests pass and DESIGN lint reports 0 errors.

- [ ] **Step 6: Verify in the live canvas**

Click “生成改编脚本”, confirm that one connected script node appears and only its combined editor opens. Create a second branch and verify the two nodes retain different rule values without modifying the story node.
