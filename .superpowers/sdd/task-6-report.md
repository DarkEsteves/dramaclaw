# Task 6 report — selected background, vertical output, motion and sound

## Implemented

- Added `composition_manifest` version 2 while leaving manifests without a version on the existing `legacy_v1` renderer route. The validated contract keeps old payloads byte-for-byte unchanged.
- Stopped using the analyzed reference video as an implicit output background. Node preparation now requires one connected, explicitly selected `background`/A-roll media node. The existing video media-purpose panel exposes **成片背景 / A-roll**; image and video media are both accepted by the manifest builder.
- Version 2 manifests declare a fixed 1080×1920, 30 fps canvas, subtitle safe-area margins, one full-duration selected background, continuous narration, and immutable media hashes.
- Added layer crop, cover/contain/stretch fit, position, size, opacity, z-order, alpha fades, and frame-deterministic zoom interpolation. Short image/video B-roll stays bounded to its semantic event.
- Added deterministic BGM ducking through an explicit `duck_under_narration` factor. Narration coverage is validated across the entire output duration and remains mixed under B-roll.
- Kept every FFmpeg input and output as a separate argv entry. FFmpeg runs inside its private staging directory so the ASS filter only sees the fixed `captions.ass` name; user media paths containing spaces, commas, quotes, and Chinese never enter filter syntax.
- ASS content escapes backslashes, braces, and newlines, keeps quotes/commas/Chinese text readable, and uses the declared subtitle safe-area margins.

## TDD evidence

- Initial Vitest run: 4 expected failures because the manifest had no v2 marker, still bound `reference`, omitted motion, and accepted a missing explicit background.
- Initial pytest run failed during collection because the new renderer routing/path safety helpers did not exist.
- A real special-path media test initially failed when an apostrophe reached the FFmpeg filter parser. The implementation was changed to run from the private staging directory with a constant ASS filename; the same 15-second real-media test then passed.
- Compatibility regression initially caught v1 artifact validation injecting `manifest_version: 1`. The field is now nullable and omitted from old persisted payloads.

## Verification

- `uv run pytest tests/test_remix_render.py tests/test_api_compose_export_contract.py -q` — 32 passed, 8 existing dependency deprecation warnings.
- `npm test -- --run src/features/canvas/remix/nodeRemixManifest.test.ts src/features/canvas/remix/prepareNodeRemix.test.ts src/features/canvas/remix/RemixMediaInput.test.tsx` — 3 files, 22 tests passed.
- Broader remix Vitest run — 30 files, 196 tests passed.
- Broader remix artifact/task/result/render/export regression — 70 tests passed.
- `npx tsc -b --pretty false` — passed.
- `uv run ruff check src/novelvideo/freezone/remix/contracts.py src/novelvideo/freezone/remix/render.py tests/test_remix_render.py` — passed.
- `git diff --check` — passed.

## Fixed-media gate

The real FFmpeg gate generates a 15-second MP4 from media stored under a directory containing Chinese, spaces, a comma, and an apostrophe. `ffprobe` confirms 1080×1920 at 30 fps with audio. Pixel probes confirm the selected blue background before/after the two-second red B-roll, the B-roll during its event, and rendered subtitle pixels inside the safe area while continuous narration and ducked BGM remain present.

## Review fix

- The first implementation routed v1 and v2 separately but still validated v2-only crop/fade/zoom/duck fields and consumed `subtitle_safe_area`/crop in the shared renderer. This could change an old permissive v1 payload that happened to contain extra keys.
- Added a real-media golden regression whose v1 manifest contains deliberately invalid v2 extension values. It must validate and render the same blue-background/red-overlay sequence as the historical composer.
- Version 1 now ignores every v2-only extension in both validation and rendering; version 2 retains all strict checks and effects.
- Post-fix verification: specified render/export suite 33 passed; artifact/task/result compatibility suite 38 passed; Ruff and diff checks passed.
