# Reference-video-remix P0 experiment

Run the bounded fixture audit against the configured NewAPI `audio-transcribe` gateway:

```bash
uv run python scripts/experiments/reference_video_remix_p0.py \
  --cases tests/fixtures/remix/cases.json \
  --asset-root /tmp/remix-fixtures \
  --output-dir /tmp/remix-p0 \
  --fixture-kind unclassified \
  --model-version '<gateway-reported-revision>'
```

Configure `NEWAPI_BASE_URL` and `NEWAPI_API_KEY` as described in [speech setup](reference-video-remix-speech-setup.md). The command writes `/tmp/remix-p0/report.json` and exits 2 while the evidence gate is blocked. It does not invent timing when the gateway is unavailable or fails to return word timestamps.

The checked-in manifest covers six categories: clear Chinese, clear English, repeated words/numbers/emoji, no speech, script mismatch, and variable-frame-rate source with crop/speed editing. A passing run requires every fixture to have approved source and license records, every consumed file to have a matching SHA-256 inventory entry, and every manually annotated boundary to be tied to its source-audio hash, annotators, and reviewer. A command-line `--fixture-kind approved-real` label alone never qualifies evidence.

The runner uses the same NewAPI adapter as the canvas: it compares the recognized text with the controlled script and uses only the returned word start/end times. Annotation ranges must correspond to complete timed words; subword or character-only boundaries cannot be measured by this provider and must not be interpolated. Each clear-language case needs at least 100 manually annotated word boundaries, a measured P95 endpoint error no greater than 200 ms, and conclusive results for all required events. The provider model revision must be recorded rather than left as a placeholder. This gate has not yet been shown to pass on qualified samples.

The renderer test checks that a selected event interval displays B-roll and preserves narration audio. It does not establish transcription or timing accuracy.
