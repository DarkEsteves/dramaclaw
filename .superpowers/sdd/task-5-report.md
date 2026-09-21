# Task 5 report — B-roll assets and graph binding

## Implemented

- Added immutable B-roll bindings for both still images and videos. Every selected visual stores the canvas node ID plus the exact SHA-256 media revision in the saved script event and media requirement.
- Added script editor actions for `生成`, `上传`, and `替换素材`. Generate/upload create an ordinary upstream image generation/upload node and connect it to the script; existing upstream image/video nodes can be attached without introducing a remix-only node type.
- Added project media attachment for generated/uploaded canvas visuals. The attachment records project path, hash, kind, and marks the node as a visual before script publication.
- Extended remix media ingestion to PNG/JPEG/WebP with ffprobe validation, project-local immutable storage, dimensions, and content hash.
- Extended manifest preparation to collect image and video nodes. Manifest building uses the exact project path/hash and rejects missing visuals or a hash that changed after selection before publishing/render submission.
- Added a real FFmpeg still-image overlay test alongside the existing real video overlay test. Both verify the narration audio remains present while the selected interval is visually replaced.

## TDD evidence

- Initial manifest/model run failed 4 tests: image bindings were rejected as missing and media revisions were not serialized.
- Initial API/render run had 1 failure: PNG upload returned HTTP 422; the existing 20 tests passed.
- Initial graph preparation test failed because image nodes were excluded from collected visual bindings.
- Initial UI run failed because selected media revisions were not captured and the create/upload affordances did not exist.
- Initial attachment UI test failed because unversioned existing visuals were not attached.
- Initial save guard test failed because scripts could publish a visual node without a media revision.

## Verification

- `npm test -- --run src/features/canvas/remix/*.test.ts src/features/canvas/remix/*.test.tsx` — 29 files, 187 tests passed.
- Focused final RemixScriptPanel/manifest/preparation/model run — 4 files, 28 tests passed.
- `uv run pytest -q tests/test_api_remix_media.py tests/test_remix_render.py` — 21 passed; 8 existing dependency deprecation warnings.
- `npx tsc -b --pretty false` — passed.
- `uv run ruff check src/novelvideo/api/routes/remix_media.py tests/test_api_remix_media.py tests/test_remix_render.py` — passed.
- `git diff --check` — passed.

## Known unrelated baseline

- `frontend/src/__tests__/features/canvas/remix-nodes.test.tsx` still has the pre-existing assertion that searches for one `Hello world` text node. TranscriptNode now intentionally renders separate word buttons, so 1 of its 5 tests fails. Task 5 does not modify TranscriptNode.
