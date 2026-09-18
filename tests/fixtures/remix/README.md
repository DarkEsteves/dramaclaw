# Reference-video-remix P0 fixtures

This directory contains the reproducible fixture manifest, not media. Source media, model weights, and rendered outputs must stay outside Git.

Obtain each asset listed in `cases.json` from the controlled test-asset store, place it below the `--asset-root` directory with the listed relative path, and record its SHA-256, source identifier, and retrieval timestamp in that store's inventory. Do not substitute synthetic tone audio for a human Chinese or English narration sample.

For `zh-clear` and `en-clear`, provide `script.txt`, the same approved script as two actual narration takes with different speaking rates, `fast-annotations.json` and `slow-annotations.json`, plus a background image and B-roll video. Each annotation file is JSON with a `boundaries` array. Every entry identifies an exact Unicode code-point interval with `start_char`, `end_char`, `start_us`, and `end_us`; it is compared with the actual runner output. The harness uses no saved aligner output as a substitute for execution.

The no-speech case must have no narration track; omission and insertion cases must preserve their script mismatch in the annotation. The clear-speech entries also declare a required event selection, which avoids ambiguous first-match handling for repeated text.

The checked-in manifest intentionally has no media, annotations, or hashes. It is a declaration of missing evidence, so the P0 command reports `blocked` until controlled assets and annotations are supplied. The command counts annotation records from the supplied JSON files; do not edit the manifest to manufacture a boundary count.
