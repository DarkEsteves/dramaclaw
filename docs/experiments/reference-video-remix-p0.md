# Reference-video-remix P0 experiment

Run the bounded host and fixture audit with:

```bash
uv run python scripts/experiments/reference_video_remix_p0.py \
  --cases tests/fixtures/remix/cases.json \
  --asset-root /tmp/remix-fixtures \
  --output-dir /tmp/remix-p0
```

The command writes `/tmp/remix-p0/report.json`. It exits 2 when the gate is blocked, which is the expected state until the controlled assets, ASR/forced-alignment environment, and manual annotations exist. It does not install packages, download weights, or infer an accuracy figure.

The report has one record per fixture with the P0 fields: `case_id`, `engine_version`, `device`, `duration_s`, `elapsed_s`, `peak_memory_mb`, `boundary_p95_ms`, `unresolved_required_events`, `output_probe`, and `status`. `p0_gate.blockers` distinguishes missing execution evidence from a measured failure.

The checked-in manifest covers six categories: clear Chinese, clear English, repeated words/numbers/emoji, no speech, script omission plus insertion, and VFR source with crop/speed editing. Add asset source IDs and SHA-256 values in the controlled asset inventory before running a measured candidate. For the two clear-speech cases, retain the same script at two speaking rates and verify that the required product reveal event follows the aligned word in each output.

For each clear-speech case, the manifest supplies `script_path`, two voice audio and annotation paths, an explicit character-span event selection, and renderer inputs. Annotation JSON must contain `{"boundaries": [...]}`; each boundary has exact `start_char`, `end_char`, `start_us`, and `end_us` values. The harness invokes its injected runner for each take, compares both start and end timestamps against annotations, calculates P95 over all measured endpoint errors for each language, and renders `output-dir/<case>/<voice>/output.mp4` using the resulting event frames.

The default runner is a lazy WhisperX adapter. It performs ASR, rejects a transcript that differs from the controlled script after casefolding and whitespace removal, then force-aligns that supplied script. It uses WhisperX's documented `load_model`, `load_audio`, `transcribe`, `load_align_model`, and `align` calls with `local_files_only=True` and `model_cache_only=True`; this run cannot silently download model weights. The API sequence was checked against the upstream [WhisperX README](https://github.com/m-bain/whisperX#python-usage) and [alignment implementation](https://github.com/m-bain/whisperX/blob/main/whisperx/alignment.py). It remains unexecuted here because WhisperX and torch are absent, so compatibility with a specific installed WhisperX release and candidate model weights is still a P0 test item.

The repository test verifies the fixed timing sample independently with FFmpeg: it samples blue background pixels before and after the selected interval, red B-roll during the selected interval, and confirms that the narration audio stream survives the video overlay. This is renderer feasibility only. It cannot establish ASR or forced-alignment accuracy.

The P0 gate may pass only after an executable, versioned aligner runs the controlled fixtures; Chinese and English each have at least 100 manually annotated boundaries; each language's measured P95 is at most 200 ms; and every required event is aligned or a deliberately expected no-speech/script-mismatch result. Record CPU/GPU type, peak memory, cold and warm elapsed times, model/weight versions, and any rejection point from the minimal Recipe and process-action binding experiments in the controlled run report.
