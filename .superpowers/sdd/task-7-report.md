# Task 7 report — manifest preview and export parity

## TDD evidence

Red tests were added before implementation:

- `manifestEditor.test.ts`: five failures because `previewStatus`, manifest projection, override application/rebase and SRT export did not exist.
- `test_remix_render.py -k preview_frame`: collection failed because `render_preview_frame` did not exist.

After implementation the focused tests passed.

Independent review then produced four regression tests before fixes:

- rapid manifest-ref switching exposed the old subtitle draft while the new artifact request was pending;
- repeated visual/SFX rows accepted ambiguous legacy identities and had no stable row ID;
- an unrelated edit left the save button enabled while recompile conflicts existed;
- FFmpeg coverage was expanded from one frame to before/inside/after boundaries plus decoded RMS audio measurement.

The first three tests failed for the reviewed reasons, then passed after the fixes.

A follow-up review added two more red assertions: MP4 preview/update controls were still exposed during a rapid ref switch, and update remained enabled for an unsaved draft. Exact-ref readiness now gates every manifest-bound action and dirty drafts must be saved before recompilation.

Closure review added two stale-detection red tests: replacing a source URL while stale `remixMedia` metadata remained did not change the fingerprint, and a deep ancestor revision did not rerender the compose node. The fingerprint now includes current visual URLs and the node subscribes to the computed full ancestor-graph fingerprint.

## What changed

- The remix compose node loads the exact saved `composition_manifest.v1` revision and projects its visual layers, subtitle events and audio events into the existing remix editor.
- Layer timing, subtitle text and audio volume edits update the effective manifest and append durable `manual_overrides`; export stays disabled until the new artifact revision is saved.
- Recompiling a manifest reapplies compatible overrides. A changed/missing base target produces an explicit node conflict and blocks export instead of silently dropping the user's edit.
- Every compiled layer and audio event has a stable `row_id`; ambiguous legacy identities conflict instead of choosing the first repeated row.
- Artifact state is keyed to the loaded ref, so a rapid ref switch immediately hides and disables the previous draft, save, export and structured downloads.
- Existing override conflicts block save as well as export; an unrelated edit cannot clear them.
- MP4 preview/download and update are hidden or disabled until the exact artifact ref loads. Update is also disabled while local overrides are dirty, and the action handler rejects indirect update requests in that state.
- Replacing `videoUrl`, `imageUrl` or `previewImageUrl` invalidates the prepared manifest even if old attached-media metadata remains.
- The compose node reactively subscribes to the complete ancestor graph fingerprint, so deep artifact/media revisions mark preview stale immediately.
- Preview freshness compares both the exact rendered manifest ref and the current upstream graph fingerprint. The node labels the confirmed result current or stale.
- The current confirmed result exposes inline video preview plus MP4, exact manifest JSON and frame-derived SRT downloads.
- `render_preview_frame` extracts a boundary frame from the same full FFmpeg manifest render path used by export. The probe compares its bytes with the matching exported frame.

## Verification

- `pnpm exec vitest run src/features/canvas/remix` — 30 files, 208 tests passed.
- `uv run pytest tests/test_remix_render.py tests/test_remix_tasks.py tests/test_remix_result_confirmation.py tests/test_api_remix_artifacts.py -q` — 44 passed, 8 existing dependency warnings.
- `pnpm exec tsc -b --pretty false` — passed.
- `uv run ruff check src/novelvideo/freezone/remix/render.py tests/test_remix_render.py` — passed.
- `git diff --check` — passed.

The FFmpeg tests use real generated picture and audio inputs. They compare exact-render frames immediately before, inside and after visual/subtitle boundaries, and decode the exported audio to verify that narration volume `0.2` produces the expected RMS ratio against volume `1.0`.
