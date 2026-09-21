# Task 11 report — MVP release matrix closure

## Scope completed

- Closed the fixed-media release matrix across old Recipe v1, old canvas and compose fixtures, project isolation, missing or replaced media, path traversal, ASS/filter input safety, hard duration/spec limits, worker cancellation/timeout recovery, queue lanes and capacity limits.
- Verified required/optional event gates, rational-frame timing, 1080×1920 at 30 fps, subtitle safe area, continuous narration, preview/export frame parity, precise downstream reruns, refresh/late-result fencing, billing and authorization boundaries.
- Normalized `CRLF` and bare `CR` before ASS newline escaping. This removes raw carriage returns from generated subtitle payloads and keeps Windows/legacy text inputs inside the same safe path as `LF`.
- Kept transcription/analysis on the bounded default lane and renders on the bounded ffmpeg lane; tests now pin those routing contracts explicitly.
- Repaired four repository-wide frontend regression expectations discovered by the release run: Vietnamese locale parity for existing Remix labels, outline visibility for transcript/timeline nodes, clickable word rendering in the transcript fixture, and the Task 10 image-request mutation abstraction in the stale-error static contract.
- Updated the release status with the acceptance matrix, exact fixed sample/output hashes, FFprobe output, model/cache revisions and the external real-speech gate.

## TDD evidence

- RED: `uv run pytest -q tests/test_remix_render.py::test_ass_escapes_markup_and_honours_safe_area` failed after adding `CRLF`/bare-`CR` input because raw `\r` remained in the ASS file and the lines were not emitted as `\\N`.
- GREEN: the same focused test passed after `_ass_escape` normalized `\r\n` and `\r` to `\n` before escaping.
- The first repository-wide frontend run found five failures across four files. Focused reruns proved the locale/outline/test-contract fixes, then a fresh full run passed all 4,702 tests.
- The first repository-wide backend run was contaminated by `/Users/wwq/worknewnew/dramaclaw/.env`: `python-dotenv` walked from the nested worktree into its parent repository and loaded production CE/EE/gateway values. It produced 50 failures and 5 setup errors while still passing 6,576 tests. An empty ignored worktree `.env` isolated the run; the 68 representative failures then all passed. The final clean full-suite result is recorded below.

## Acceptance evidence

- Focused compatibility and resource matrix:
  `uv run pytest -q tests/test_remix_*.py tests/test_workflow_stable_contract.py tests/test_recipe_runtime.py tests/test_recipe_v2_delivery.py tests/test_freezone_canvas_store.py tests/test_api_compose_export_contract.py tests/test_video_composer_result_contract.py tests/test_task_backend_limits.py tests/contract/test_l014_inline_task_backend_concurrency.py`
  — 313 passed.
- Clean repository-wide backend: `uv run pytest -q` — 6,631 passed, 19 skipped, 3 deselected. The only emitted warnings are existing framework/dependency deprecations.
- Release render probe:
  `uv run pytest -q tests/test_remix_render.py::test_v2_fixed_media_gate_renders_vertical_hd_broll_voice_and_subtitles --basetemp=/tmp/remix-release-probe-task11`
  — 1 passed; output SHA-256 `2f5db2ee14115f8e2ac003cba104bcd55ffbe6d0ed73eba544612a4945c402fe`; H.264 1080×1920 30/1, AAC 44.1 kHz mono, 15.000000 seconds. The task-only temporary directory was removed afterward.
- Frontend Remix journey/recovery matrix:
  `pnpm exec vitest run src/features/canvas/remix src/api/remix.test.ts src/__tests__/features/canvas/remix-nodes.test.tsx src/__tests__/features/freezone/workflow-run-recovery-bar.test.tsx src/__tests__/features/superchat/chat-task-status-bar.test.ts`
  — 38 files, 297 passed.
- Full frontend: `pnpm exec vitest run` — 566 files, 4,702 passed.
- TypeScript: `pnpm exec tsc -b --pretty false` — passed.
- Stable workflow contract: `uv run python scripts/generate_workflow_contract.py --check` — passed.
- Secret scan: `UV_CACHE_DIR=/tmp/uv-task11-cache uvx pre-commit run gitleaks --all-files` — passed; the task-only uv cache was removed afterward.
- License inventory regenerated from the complete Git index; `test_license_inventory_covers_current_git_index` passed.
- Ruff on changed Python sources/tests — passed.
- `git diff --check` — passed.

## Fixed sample and model record

- P0 report `/tmp/remix-p0/report.json`: SHA-256 `95c6d68dd2ae58c51bd13afc3162672bb5e329187942b3dbb327e563996754fb`.
- VFR/crop/speed output: `433a6672d325b1a25e4dc93cc7c9f59b62b372a9ccf4dcf3b0607e7653b3e1fd`.
- Reference video: `3acc06646ed54d5ced9b62c9d2b9d620190ed89180e1641b4ad4505a9673995d`.
- Fast/slow narration: `330bf5854b4283fc7c145a9878e09c4a80fff1f1ec83980fb36e4f7a3d80dbcc` / `2a647c902d073eeff3a770f832154a3ba8534e175e520834573b711a7fa78228`.
- WhisperX 3.8.6, CPU int8, batch 1, concurrency 1; `Systran/faster-whisper-small@536b0662742c02347bc0e980a01041f333bce120`; zh-cn align `99ccb2737be22b8bb50dcfcc39ad4d567fb90cfd`; English align cache SHA-256 `488fd4f16de84438ffc945334278c1b9fb9b7159a806c1080b16111a958c945d`.

## Release decision

The native fixed-media canvas journey is **demo-ready**. It is **not production speech-ready**: there are zero eligible Chinese and English manual boundary fixtures, so P95 alignment is unmeasured, and the locally cached ASR/alignment weights still lack the required verified license/source record. Production approval requires at least 100 human-reviewed boundaries per language, P95 ≤200 ms, the six-class real-media provenance manifest, verified model licenses, and target Linux/GPU cold/warm latency, memory and concurrency measurements.

## Review closure: speech admission

- WhisperX transcription and forced alignment now use a dedicated `speech` lane. Its CE global concurrency, project concurrency and project-user concurrency all default to 1; the global pending bound is 64. Reference-frame evidence analysis remains on `default` because it does not load WhisperX.
- A process-wide bounded semaphore also wraps the actual adapter call, so accidental direct calls cannot load two speech engines inside one API/worker process.
- The concurrency regression starts speech work for two different projects/users, proves only one enters while an unrelated default-lane task completes, then cancels the queued speech task and proves it never starts.
- Focused queue, limit, registry, alignment, action-routing, task-state and inline-backend verification passed 379 tests. Manual browser verification was deliberately not claimed in this report; use the two-terminal steps below.
- The operator's first browser run exposed a real external-worker packaging failure: launching `whisperx_worker.py` by path imported its sibling outside the `novelvideo` package, so that sibling's relative contract import failed. After that fix, the next run exposed a second boundary error: the isolated WhisperX environment correctly had no application-side Pydantic. The worker now adds its package root, while the adapter's internal speech-comparison DTOs use only the standard library. The external environment therefore needs WhisperX and its own dependencies only.
- A real subprocess regression runs the configured external-Python path through an isolated `python -S` wrapper with no application site-packages and without mocking `subprocess.run`. Readiness now starts the actual worker in offline `probe` mode, so package/runtime dependency failures are rejected before a job is accepted. The actual local `~/.local/share/dramaclaw/whisperx-venv/bin/python` probe returned code 0 and valid JSON. The operator still needs to repeat the browser journey after the API restart, so this is recorded as found and fixed rather than as a manual pass.
- Repository-wide backend after the speech-lane and initial worker packaging fix: 6,639 passed, 19 skipped, 3 deselected. After removing the worker's application dependency and changing readiness to probe the worker itself, the complete affected speech/task subset passed 102 tests. Ruff, stable workflow contract, license-inventory coverage, `git diff --check`, and full-tree gitleaks all passed.

### Local manual browser journey

Terminal 1, from this worktree:

```sh
scripts/start-remix-speech.sh
```

Terminal 2:

```sh
cd frontend
pnpm dev --host 127.0.0.1 --port 5173
```

Open `http://127.0.0.1:5173`, create/open a project and Freezone canvas, add a reference video, run transcription, then submit an alignment while the first speech task is active. In the task center, check that the second speech task waits or is rejected by the per-user admission gate, ordinary canvas actions still run, and a queued speech task can be cancelled. Continue through blueprint, script, B-roll, narration, timing and compose export. This is an operator checklist, not a recorded manual pass.
