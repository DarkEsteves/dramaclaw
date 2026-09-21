# Task 7 report — manifest preview and export parity

## TDD evidence

Red tests were added before implementation:

- `manifestEditor.test.ts`: five failures because `previewStatus`, manifest projection, override application/rebase and SRT export did not exist.
- `test_remix_render.py -k preview_frame`: collection failed because `render_preview_frame` did not exist.

After implementation the focused tests passed.

## What changed

- The remix compose node loads the exact saved `composition_manifest.v1` revision and projects its visual layers, subtitle events and audio events into the existing remix editor.
- Layer timing, subtitle text and audio volume edits update the effective manifest and append durable `manual_overrides`; export stays disabled until the new artifact revision is saved.
- Recompiling a manifest reapplies compatible overrides. A changed/missing base target produces an explicit node conflict and blocks export instead of silently dropping the user's edit.
- Preview freshness compares both the exact rendered manifest ref and the current upstream graph fingerprint. The node labels the confirmed result current or stale.
- The current confirmed result exposes inline video preview plus MP4, exact manifest JSON and frame-derived SRT downloads.
- `render_preview_frame` extracts a boundary frame from the same full FFmpeg manifest render path used by export. The probe compares its bytes with the matching exported frame.

## Verification

- `pnpm exec vitest run src/features/canvas/remix` — 30 files, 201 tests passed.
- `uv run pytest tests/test_remix_render.py tests/test_remix_tasks.py tests/test_remix_result_confirmation.py tests/test_api_remix_artifacts.py -q` — 43 passed, 8 existing dependency warnings.
- `pnpm exec tsc -b --pretty false` — passed.
- `uv run ruff check src/novelvideo/freezone/remix/render.py tests/test_remix_render.py` — passed.
- `git diff --check` — passed.

The FFmpeg test uses real generated picture and audio inputs. Existing remix render coverage also probes the output audio stream, narration/BGM roles and volume/ducking validation.
