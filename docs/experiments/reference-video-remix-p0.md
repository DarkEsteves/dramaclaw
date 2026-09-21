# Reference-video-remix P0 experiment

Run the bounded host and fixture audit with:

```bash
uv run python scripts/experiments/reference_video_remix_p0.py \
  --cases tests/fixtures/remix/cases.json \
  --asset-root /tmp/remix-fixtures \
  --output-dir /tmp/remix-p0 \
  --fixture-kind unclassified \
  --model '<model-name>' \
  --asr-weight-version '<repository>@<revision>' \
  --asr-weight-license '<SPDX identifier>' \
  --alignment-weight-version '<repository/file>@<revision-or-sha256>' \
  --alignment-weight-license '<SPDX identifier>'
```

The command writes `/tmp/remix-p0/report.json`. It exits 2 when the gate is blocked, which is the expected state until the controlled assets, ASR/forced-alignment environment, and manual annotations exist. It does not install packages, download weights, or infer an accuracy figure.

The report has one record per fixture with the P0 fields: `case_id`, `engine_version`, `device`, `duration_s`, `elapsed_s`, `process_cumulative_high_water_mb`, `boundary_p95_ms`, `unresolved_required_events`, `output_probe`, and `status`. Memory is the process cumulative high-water mark observed when a case ends; it is not attributed as isolated per-case usage. `p0_gate.blockers` distinguishes missing execution evidence from a measured failure.

`--fixture-kind` is only a human-readable run label. It never qualifies evidence. A passing run requires every case to carry `provenance.kind: approved-real`, `source_id`, license, approver, approval timestamp and approval ID. Every required asset needs a per-case `asset_inventory` entry with its SHA-256, source ID, license, approver and retrieval timestamp; the harness hashes the local bytes. Each annotation JSON needs manual provenance with annotator IDs, reviewer, annotation timestamp and the exact source-audio SHA-256. Missing or mismatched evidence zeroes the eligible boundary counts even if the command line says `--fixture-kind approved-real`.

The checked-in manifest covers six categories: clear Chinese, clear English, repeated words/numbers/emoji, no speech, script omission plus insertion, and VFR source with crop/speed editing. The harness executes all six: it aligns and renders both clear languages at explicit `fast` and `slow` rates, checks the declared repeat/number/emoji content before alignment, observes actual no-speech and mismatch failures from the runner, and validates VFR input plus requested crop dimensions and speed-adjusted duration with FFmpeg. Add the verified provenance and inventory fields above before running a measured candidate.

For each clear-speech case, the manifest supplies `script_path`, two voice audio and annotation paths, an explicit character-span event selection, and renderer inputs. Annotation JSON must contain `{"boundaries": [...]}`; each boundary has exact `start_char`, `end_char`, `start_us`, and `end_us` values. The harness invokes its injected runner for each take, compares both start and end timestamps against annotations, calculates P95 over all measured endpoint errors for each language, and renders `output-dir/<case>/<voice>/output.mp4` using the resulting event frames.

The default runner is a lazy WhisperX adapter. It performs ASR, rejects a transcript that differs from the controlled script after casefolding and whitespace removal, then force-aligns that supplied script. It uses WhisperX's documented `load_model`, `load_audio`, `transcribe`, `load_align_model`, and `align` calls with `local_files_only=True` and `model_cache_only=True`; this run cannot silently download model weights. The API sequence was checked against the upstream [WhisperX README](https://github.com/m-bain/whisperX#python-usage) and [alignment implementation](https://github.com/m-bain/whisperX/blob/main/whisperx/alignment.py). It remains unexecuted here because WhisperX and torch are absent, so compatibility with a specific installed WhisperX release and candidate model weights is still a P0 test item.

The repository test verifies the fixed timing sample independently with FFmpeg: it samples blue background pixels before and after the selected interval, red B-roll during the selected interval, and confirms that the narration audio stream survives the video overlay. This is renderer feasibility only. It cannot establish ASR or forced-alignment accuracy.

The P0 gate may pass only after an executable, versioned aligner runs every verified fixture; clear Chinese and clear English each have at least 100 manually annotated boundaries; each language's measured P95 is at most 200 ms; and every required event is aligned or a runner-observed no-speech/script-mismatch result. Placeholder or unverified WhisperX/model/weight versions and licenses block the gate. Record CPU/GPU type, cumulative process high-water memory, cold and warm elapsed times, model/weight versions, and any rejection point from the minimal Recipe and process-action binding experiments in the controlled run report.
