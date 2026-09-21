# Reference Video Remix Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` or `executing-plans` task by task. Each checkbox is a separately verifiable action. The source specification is `/Users/wwq/Downloads/reference-video-remix-plan.md` (2026-09-15); this document records the remaining work against the 2026-09-21 branch, not against the specification's old baseline.

**Goal:** Make the planned 15–60 second, single-speaker, 9:16 Chinese/English reference-video remix flow complete: reference video → evidence/blueprint → edited script → actual narration → aligned words → semantic timeline → generated or uploaded B-roll → layered preview and MP4 → recoverable rerun.

**Architecture:** Keep existing canvas nodes and versioned remix artifacts. The backend owns evidence, speech, timing, rendering and durable operation state; the canvas edits artifacts and projects their exact revisions. A workflow coordinator submits only registered actions, reconciles existing operations after reload, and invalidates downstream artifacts by exact input revision or media hash.

**Tech Stack:** Python/FastAPI/Pydantic/pytest, FFmpeg/ffprobe, local WhisperX adapter, TypeScript/React/Vitest, existing task backend and Workflow v1 contract.

## Global Constraints

- Implement the **MVP in sections 1 and 8** of the source plan. Cross-language variants, multi-speaker performance, lip sync, HTML animation, batch production and browserless continuous execution are P1 and are not completion gates here. Existing multi-speaker UI remains compatible.
- Preserve ordinary script/audio/video nodes, Recipe v1, old canvas files and old compose rendering. New behavior is opt-in through remix artifact/manifest versions.
- Production narration timing comes from the selected final audio or A-roll take, never from the reference video's clock.
- No provider change is required for this plan. Keep the current text-model routing and the reserved voice-design UI; use uploaded audio or current TTS until the separate NewAPI voice service is available.
- Do not auto-download model weights at request time. Keep WhisperX in its separate environment and expose a readiness error before accepting speech jobs.
- Resolve all required events or block export. Preserve stale task outputs as history; never silently overwrite a newer node or user timing override.
- Read `DESIGN.md` before UI changes; keep existing canvas spacing, type, colors and controls. Keep local media, model weights, generated files and credentials out of Git.
- For each task: write a failing focused test, observe the expected failure, implement, pass focused tests, review the diff and commit. Run broader suites only at the stage gates.

## Baseline verified on 2026-09-21

| Area | Exists now | Missing for the source plan |
| --- | --- | --- |
| P0 | Six fixture categories and measurement harness; local WhisperX installed on this machine | Real approved Chinese/English samples, ≥100 manual boundaries per language, measured P95/runtime/memory and deployment decision |
| M1 | Versioned artifacts and CAS, full-span evidence sampling, transcript and story nodes, editable blueprint rules | Evidence-backed event editing, review state, refresh proof and complete reference-to-script acceptance |
| M2 | Script drafts/selections, multi-role TTS, transcription, strict aligner, timing compiler, timeline node | ASR mismatch diagnosis/recovery; usable word coverage after generated speech; complete timing override and stale-source UX |
| M3 | FFmpeg layered renderer, ASS subtitles, media hash binding, render task, basic canvas export | Image B-roll binding in node flow, non-reference background, 1080×1920 output, fades/zoom/ducking, manifest-based preview and full fixed-media acceptance |
| M4 | Workflow processing descriptors, Recipe v2 structured output, idempotent operation records | Registered action execution in approval/task/accounting flow, one-flow orchestration, graph invalidation and reload/cancel/unknown-outcome recovery |
| M5 | Focused tests and manual synthetic export | Full compatibility/security/resource matrix, actual sample QA, usage docs and release gate |

Code evidence: `src/novelvideo/freezone/remix/whisperx_adapter.py` still requires exact ASR/script equality; `frontend/src/features/canvas/remix/nodeRemixManifest.ts` always uses the reference video as layer 0 and emits 720×1280; `src/novelvideo/freezone/workflow_plan.py` compiles descriptors but does not execute them; `src/novelvideo/api/routes/freezone.py` still advertises `headless_execution=false`. The earlier `docs/experiments/reference-video-remix-implementation-status.md` predates the newest canvas/voice commits and must not be used as the sole status source.

## File and interface map

- Speech: `src/novelvideo/freezone/remix/{alignment,whisperx_adapter,whisperx_worker}.py`, `frontend/src/features/canvas/nodes/TranscriptNode.tsx`, `tests/test_remix_{alignment,whisperx_adapter}.py`.
- Evidence/blueprint/script: `src/novelvideo/freezone/remix/{evidence,reference_context,script_draft,contracts}.py`, `frontend/src/features/canvas/{nodes/VideoStoryNode.tsx,remix/{ReferenceEventList,RemixScriptPanel}.tsx}`, corresponding `tests/test_remix_*` and Vitest files.
- Timeline: `src/novelvideo/freezone/remix/timing.py`, `frontend/src/features/canvas/{nodes/SemanticTimelineNode.tsx,remix/{semanticTimelineModel,spawnRemixDownstream}.ts}`.
- Media/render: `frontend/src/features/canvas/remix/{nodeRemixManifest,prepareNodeRemix,manifestEditor}.ts`, `frontend/src/features/canvas/nodes/VideoComposeNode.tsx`, `src/novelvideo/freezone/remix/render.py`, `tests/test_remix_render.py`.
- Workflow/recovery: `schemas/workflow/v1/stable-contract.json`, generated Python/TS contract files, `src/novelvideo/freezone/{workflow_plan,workflow_runs,recipe_runtime}.py`, `src/novelvideo/freezone/remix/operations.py`, `src/novelvideo/api/routes/remix.py`, canvas action adapters and task tests.

## Stage A — unblock the vertical slice

### Task 1: Speech comparison with explicit coverage

**Interface:** `compare_spoken_script(script: str, asr: str) -> MatchReport` returns normalized text ranges, substitutions, insertions, omissions and character coverage. `WhisperXSpeechAligner.align()` may force-align only when coverage meets a measured threshold and every required selection is covered; otherwise it returns `script_mismatch` with that report. Keep the final word timeline normalized by `alignment.py`.

**Files:** `src/novelvideo/freezone/remix/{whisperx_adapter,alignment,contracts}.py`, `tests/test_remix_{whisperx_adapter,alignment}.py`, `frontend/src/features/canvas/{remix/transcriptModel.ts,nodes/TranscriptNode.tsx}` and its focused Vitest test.

- [ ] Add failing tests with the generated-speech case: `馋奶茶→茶奶茶`, `不得劲→不得见`, punctuation differences; also a truly omitted required phrase. Assert the former reports low-confidence differences without pretending the ASR is ground truth, and the latter blocks export.
- [ ] Run `uv run pytest tests/test_remix_whisperx_adapter.py tests/test_remix_alignment.py -q`; confirm the new assertions fail for the exact-equality check.
- [ ] Implement normalized comparison and bounded coverage policy. Preserve the original script character offsets, role-prefix stripping, word intervals, mismatch details and provenance. Do not manufacture timestamps for characters WhisperX cannot align.
- [ ] Display differing spans and a `校对台词 / 重新对齐` route in the transcript node; retain current audio and script revisions while the user reviews.
- [ ] Run focused Python/Vitest tests and commit `fix(remix): report and recover speech alignment differences`.

**Gate:** The audio already generated on the demo canvas can either yield a valid aligned timeline with honest coverage or a specific review list. A missing sentence cannot silently pass.

### Task 2: Repeatable local speech startup and bounded fixture gate

**Files:** `src/novelvideo/api/routes/remix.py`, `docs/experiments/reference-video-remix-speech-setup.md`, `scripts/experiments/reference_video_remix_p0.py`, `tests/test_remix_p0.py`, a local development start script under `scripts/`.

- [ ] Add a failing startup/readiness test: configured executable and cached models report ready; missing cache reports a useful error without downloading anything.
- [ ] Add a script that starts the local API with the existing independent WhisperX Python/cache paths, keeps CE settings explicit and checks the readiness endpoint. Never put credentials in the script.
- [ ] Run the six-category P0 manifest against synthetic/local legal fixtures now; record measured latency, memory, FFmpeg output and known blockers in `/tmp/remix-p0/report.json`.
- [ ] When approved real samples and hand annotations arrive, run the same harness unchanged. Require Chinese and English each ≥100 manually annotated boundaries, P95 ≤200 ms, two speech rates, and recorded model/host versions before claiming production speech acceptance.
- [ ] Record WhisperX, ASR/alignment weight versions, licenses, platform support and the chosen CPU/GPU concurrency limit beside the measured report.
- [ ] Commit `test(remix): make local speech and P0 gate repeatable`.

**Gate:** Development demo runs without manual environment guessing. The real accuracy gate remains explicitly pending until the user supplies legal samples and annotations; it does not block unrelated implementation.

## Stage B — finish reference, script and timing artifacts

### Task 3: Evidence-backed story review

**Files:** `src/novelvideo/freezone/remix/{evidence,reference_context,contracts}.py`, `tests/test_remix_{evidence,reference_context}.py`, `frontend/src/features/canvas/{nodes/VideoStoryNode.tsx,remix/{ReferenceEventList,blueprintModel}.ts*}`.

- [ ] Add failing tests for first/middle/last video coverage, PTS/time_base on VFR input, each blueprint event's frame/transcript reference, and edits persisting after reload.
- [ ] Expose each story event's observed frame/time, linked spoken range, model inference, user-edited narrative role and review state in the existing story node. Keep original evidence immutable and save user overrides as a new blueprint revision.
- [ ] Block script generation only for missing required structure or broken evidence references; allow explicitly reviewed uncertainty.
- [ ] Run `uv run pytest tests/test_remix_evidence.py tests/test_remix_reference_context.py -q` and the `ReferenceEventList`/blueprint Vitest files; commit `feat(remix): make reference events reviewable`.

**Gate:** Reference video → evidence → reviewed blueprint survives reload and supplies an exact version to the script node.

### Task 4: Script selections and actual-audio timeline

**Files:** `src/novelvideo/freezone/remix/{script_draft,timing}.py`, `tests/test_remix_{script_draft,timing}.py`, `frontend/src/features/canvas/{remix/{RemixScriptPanel,selection,semanticTimelineModel,spawnRemixDownstream}.ts*,nodes/SemanticTimelineNode.tsx}`.

- [ ] Add failing cases for repeated words, emoji/code-point offsets, changed dialogue, replaced voice take, speed/crop edit map, fixed-time override conflict, and required-event failure.
- [ ] Preserve selection IDs only when the edited text can be mapped unambiguously; otherwise mark the selection invalid and require re-selection. Ensure the compiler consumes the final selected narration hash and exact script/alignment revisions.
- [ ] Expose semantic offset, minimum duration, fixed-time override and conflict indication in the existing timeline node; recompile without overwriting manual edits.
- [ ] Run Python timing and focused Vitest tests, then commit `feat(remix): finish semantic timing edits`.

**Gate:** Changing narration speed moves its linked subtitles/B-roll within one output frame; an obsolete take or unresolved required selection blocks export.

## Stage C — deliver the actual remixed picture and sound

### Task 5: B-roll assets and graph binding

**Files:** `frontend/src/features/canvas/{remix/{RemixScriptPanel,nodeRemixManifest,prepareNodeRemix}.ts*,nodes/{ImageNode,VideoNode}.tsx}`, existing canvas media generation actions, `tests/test_remix_render.py`, `frontend/src/features/canvas/remix/nodeRemixManifest.test.ts`.

- [ ] Add failing manifest tests for an uploaded image, uploaded video, generated visual node, missing required visual, and changed media hash.
- [ ] From a semantic selection, create or attach an existing image/video node, store its node ID and media revision in the script event, and provide `生成/上传/替换素材` through the existing canvas affordance. Do not claim every visual must use a new node type.
- [ ] Extend `buildNodeRemixManifest` and `prepareNodeRemix` to bind image **and** video media with exact project path/hash; reject absent required media before a render task is submitted.
- [ ] Run the focused manifest tests and real FFmpeg image/video overlay tests; commit `feat(remix): bind generated and uploaded b-roll`.

**Gate:** A fixed legal image or video can replace a selected interval while the same narration continues.

### Task 6: New background, output spec, motion and sound

**Files:** `frontend/src/features/canvas/remix/{nodeRemixManifest,prepareNodeRemix}.ts`, `src/novelvideo/freezone/remix/{contracts,render}.py`, `tests/test_remix_render.py`, `frontend/src/features/canvas/remix/nodeRemixManifest.test.ts`.

- [ ] Add failing tests showing that the reference video is an analysis input and is **not automatically** the rendered background. Test an explicit selected A-roll/background, or a generated/image background, 1080×1920 at 30 fps, short B-roll, subtitle safe area and continuous narration.
- [ ] Make the output canvas spec explicit in the manifest. Add image/video fit, crop, opacity, z-order, basic fade and zoom interpolation, and deterministic BGM ducking. Preserve the old composer path through manifest version routing.
- [ ] Use structured FFmpeg arguments and escaped ASS text; test inputs containing quotes, commas, Chinese and path spaces.
- [ ] Run `uv run pytest tests/test_remix_render.py tests/test_api_compose_export_contract.py -q` and the manifest Vitest tests; commit `feat(remix): render selected background and layered effects`.

**Gate:** A fixed-media 15–60 second MP4 is 1080×1920/30 fps, shows the selected picture, preserves narration under B-roll, and renders readable Chinese/English subtitles.

### Task 7: Manifest-based preview and export parity

**Files:** `frontend/src/features/canvas/{nodes/VideoComposeNode.tsx,remix/{RemixEditor,manifestEditor}.tsx}`, `src/novelvideo/freezone/remix/render.py`, `tests/test_remix_render.py`, focused compose Vitest tests.

- [ ] Add failing tests that compare a preview frame at known event boundaries to FFmpeg output generated from the **same manifest revision**. Cover picture, subtitle visibility and audio role/volume.
- [ ] Project compiled layers and audio events into the existing compose editor. Store edits as manifest overrides, not unrelated UI-only clip state; export from that exact revision.
- [ ] Provide preview, download and JSON/SRT access from the current compose result. Mark preview stale when upstream media/selection/timing changes.
- [ ] Run screenshot/audio probe tests plus compose Vitest tests; commit `feat(remix): preview the exact export manifest`.

**Gate:** Preview and exported file agree at sampled event boundaries; manual changes survive a recompile or appear as an explicit conflict.

## Stage D — one flow, safe reruns and recovery

### Task 8: Registered processing actions in Workflow

**Files:** `schemas/workflow/v1/stable-contract.json` and generated contracts, `src/novelvideo/freezone/{workflow_plan,workflow_runs,workflow_schema}.py`, `src/novelvideo/api/routes/remix.py`, `tests/test_{workflow_plan,workflow_runs,remix_processing_tasks}.py`.

- [ ] Add failing plan/execution tests for `analyze_reference_video`, `transcribe_media`, `align_script`, `compile_semantic_timeline` and `render_manifest`: unique typed inputs, authorization, preflight, task state and artifact proof. A client-supplied shell command or unknown action must be rejected.
- [ ] Reconcile the stable contract with the action API (`analyze_reference_video` exists in the API but not the current generated processing-action list). Regenerate Python/TS/MCP contract artifacts together.
- [ ] Bind the registered actions into the existing confirmed workflow run, with exact input refs, billing/usage hooks where applicable, cancellation and task envelope proof. Do not require a Recipe ID for deterministic processing.
- [ ] Run workflow contract, plan, preflight, transaction and processing-task suites; commit `feat(remix): execute registered workflow processing steps`.

**Gate:** One approved workflow run can dispatch each step in dependency order and report its durable artifact/result, without a manual API call for every node.

### Task 9: Creation Recipes and one-click canvas journey

**Files:** `src/novelvideo/freezone/{agent_catalog_schema,recipe_runtime}.py`, built-in reference Recipes, `frontend/src/features/canvas/{nodes/VideoStoryNode.tsx,ui/NodeActionToolbar.tsx}` and the canvas workflow adapter, `tests/test_{recipe_v2_delivery,remix_recipe_contract}.py`.

- [ ] Add failing end-to-end planning tests for upload → analysis → new-topic script → selected narration → required B-roll → alignment → timing → manifest → render. Include a manual upload branch where no voice provider is configured.
- [ ] Use Recipe v2 structured outputs for creative judgments only; validate JSON/schema before publishing a blueprint, script or B-roll plan. Keep Recipe v1 output behavior unchanged.
- [ ] Add a concise project Skill that states the creative goal, which content may change, the exact stage dependencies, review points and rerun rules. It calls registered actions/Recipes and does not embed shell commands or duplicate their implementation.
- [ ] Provide a single visible `继续改编` entry that advances to the next incomplete node/action and shows what input is needed; retain each node's direct button for corrections.
- [ ] Run Recipe delivery and browser journey tests; commit `feat(remix): connect the canvas remix journey`.

**Gate:** A user can start from one reference video and reach an editable exported MP4 using the native canvas, without manually creating hidden protocol artifacts.

### Task 10: Precise invalidation, reuse and recovery

**Files:** `src/novelvideo/freezone/remix/{operations,store,results}.py`, `src/novelvideo/freezone/workflow_runs.py`, `frontend/src/features/canvas/remix/{prepareNodeRemix,spawnRemixDownstream}.ts`, affected node task hooks, `tests/test_remix_{operations,result_confirmation}.py`.

- [ ] Add failing tests for reload during a task, an old result arriving after script revision change, a deleted media file, same idempotency key, intentional regenerate, provider timeout with unknown outcome, cancellation and simultaneous tabs.
- [ ] Reconcile by existing operation/task ID before submitting a replacement. Validate exact input refs, media hashes, producer version and node revision before reuse or node backfill; retain stale output in history.
- [ ] Implement downstream invalidation: style-only edits rerender; visual prompt edits regenerate that visual and rerender; script/voice changes realign/recompile/rerender. Do not resend unrelated paid model calls.
- [ ] Run operation/result/workflow recovery suites; commit `feat(remix): recover and rerun only affected work`.

**Gate:** Refresh and late results never overwrite newer edits, and a local change sends only the necessary downstream jobs.

## Stage E — release gate, then detail refinement

### Task 11: Compatibility, isolation and resource envelope

**Files:** `tests/test_remix_*.py`, `tests/test_workflow_stable_contract.py`, compose compatibility tests, `tests/fixtures/remix/cases.json`, `docs/experiments/reference-video-remix-implementation-status.md`.

- [ ] Run old Recipe/canvas/compose fixtures plus cross-project access and missing-file/cache tests. Add regressions for path traversal, ASS/filter escaping, oversized duration/spec, worker cancellation/timeout and queued resource limits.
- [ ] Check the full source-plan acceptance matrix: required events, timeline ≤1 frame, 1080×1920/30 fps, subtitle safe area, continuous narration, preview/export parity, local rerun, refresh/late result and billing/authorization boundaries.
- [ ] Run `uv run pytest` and focused frontend Vitest/TypeScript checks. Run the repository pre-commit secret scan on changed configuration/gateway files.
- [ ] Record exact passing commands, sample hashes, output probes, model versions and remaining externally blocked real-speech evidence in the status document; commit `test(remix): close the MVP release matrix`.

**Gate:** The complete fixed-media native-canvas journey passes automatically and manually. Production accuracy is stated only after Task 2's real annotated corpus passes; otherwise the MVP is labeled demo-ready, not production speech-ready.

### Task 12: User documentation and review pass

**Files:** `docs/experiments/reference-video-remix-implementation-status.md`, a new operator/user guide in `docs/`, existing speech setup documentation.

- [ ] Write a one-page canvas walkthrough with the exact buttons and how to recover from no voice, mismatch, missing required visual and stale output. Add deployment prerequisites and supported 15–60 second formats.
- [ ] Have a fresh reviewer check the source plan section by section against Tasks 1–11 and a real canvas recording. Fix omissions before UI polish or copy tweaks.
- [ ] Commit `docs(remix): document the complete native canvas flow`.

**Gate:** An unfamiliar teammate can reproduce the workflow from a clean project and identify every required external prerequisite.

## Execution order and checkpoints

1. Tasks 1–2 unblock speech and make the current demo usable. Task 2's real annotation measurement can finish later when assets arrive.
2. Tasks 3–4 complete the reference/script/time contract. Task 5 can start after Task 4's event IDs and media binding are stable.
3. Tasks 5–7 create an independently usable fixed-media MP4 before broad workflow orchestration. This is the first full visual demo checkpoint.
4. Tasks 8–10 turn that explicit node chain into a one-flow, recoverable product. This is the functional MVP checkpoint.
5. Tasks 11–12 close compatibility, evidence and documentation. Only after these gates should layout, wording and finer controls be adjusted.

Do not mark a stage complete because a node exists or a model task returned success. Its stated gate must pass on the actual output and current artifact revisions.
