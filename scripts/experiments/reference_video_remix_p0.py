#!/usr/bin/env python3
"""Write an auditable, bounded P0 feasibility report for reference-video remix.

This command never installs an ASR package or downloads model weights. It reports
the actual host capabilities and fixture availability so a missing engine cannot be
mistaken for an alignment result.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import math
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from novelvideo.freezone.remix.p0 import (
    WhisperXAlignmentRunner,
    evaluate_p0_gate,
    execute_alignment_case,
    render_sample,
)

try:
    import resource
except ImportError:  # Windows reports the field as unavailable.
    resource = None  # type: ignore[assignment]


def _command_version(command: str) -> str | None:
    if not shutil.which(command):
        return None
    result = subprocess.run(
        [command, "-version"], check=False, capture_output=True, text=True
    )
    if result.returncode:
        return None
    return result.stdout.splitlines()[0] if result.stdout else command


def _probe_output(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,width,height,r_frame_rate,duration",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _peak_memory_mb() -> float | None:
    """Return the process/child high-water mark using the host's native units."""
    if resource is None:
        return None
    peaks = [
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
    ]
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
    return round(max(peaks) / divisor, 3)


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _input_probes(paths: list[Path], asset_root: Path) -> dict[str, Any]:
    probes: dict[str, Any] = {}
    for path in paths:
        if path.suffix.lower() not in {
            ".aac",
            ".flac",
            ".m4a",
            ".mov",
            ".mp3",
            ".mp4",
            ".ogg",
            ".wav",
            ".webm",
        }:
            continue
        try:
            probes[str(path.relative_to(asset_root))] = _probe_output(path)
        except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
            probes[str(path.relative_to(asset_root))] = {"error": str(error)}
    return probes


def _load_annotations(path: Path) -> list[dict[str, Any]]:
    content = json.loads(path.read_text(encoding="utf-8"))
    boundaries = content.get("boundaries")
    if not isinstance(boundaries, list):
        raise ValueError(f"{path} must contain a boundaries array")
    return boundaries


def _media_duration_s(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(json.loads(result.stdout)["format"]["duration"])


def _case_report(
    case: dict[str, Any],
    asset_root: Path,
    output_dir: Path,
    runner: WhisperXAlignmentRunner | None,
) -> tuple[dict[str, Any], list[float]]:
    started = time.monotonic()
    required_assets = [asset_root / relative for relative in case["required_assets"]]
    missing = [
        str(path.relative_to(asset_root))
        for path in required_assets
        if not path.is_file()
    ]
    input_probes = _input_probes(
        [path for path in required_assets if path.is_file()], asset_root
    )
    category = case["category"]
    if missing:
        status = "blocked_fixture_missing"
        unresolved = [case.get("required_event", "fixture validation")]
        output_probe: dict[str, Any] | None = None
        errors: list[float] = []
        duration_s: float | None = None
        annotation_count = 0
    elif category == "no_speech":
        status = "expected_no_speech"
        unresolved = ["speech timeline"]
        output_probe = None
        errors = []
        duration_s = None
        annotation_count = 0
    elif category == "script_mismatch":
        status = "expected_unresolved"
        unresolved = ["script mismatch"]
        output_probe = None
        errors = []
        duration_s = None
        annotation_count = 0
    elif runner is None:
        status = "blocked_alignment_not_run"
        unresolved = [case.get("required_event", "required event")]
        output_probe = None
        errors = []
        duration_s = None
        annotation_count = 0
    elif category != "clear_speech":
        status = "blocked_case_execution_not_implemented"
        unresolved = [case.get("required_event", "fixture validation")]
        output_probe = None
        errors = []
        duration_s = None
        annotation_count = 0
    else:
        script = (asset_root / case["script_path"]).read_text(encoding="utf-8").strip()
        voice_results: list[dict[str, Any]] = []
        errors = []
        durations: list[float] = []
        annotation_count = 0
        try:
            for voice in case["voices"]:
                annotations = _load_annotations(asset_root / voice["annotations"])
                annotation_count += len(annotations)
                result = execute_alignment_case(
                    case=case,
                    audio=asset_root / voice["audio"],
                    script=script,
                    annotations=annotations,
                    runner=runner,
                    fps=case["render"]["fps"],
                )
                durations.append(_media_duration_s(asset_root / voice["audio"]))
                output = output_dir / case["case_id"] / voice["id"] / "output.mp4"
                render_sample(
                    background=asset_root / case["render"]["background"],
                    broll=asset_root / case["render"]["broll"],
                    narration=asset_root / voice["audio"],
                    caption=case["render"]["caption"],
                    start_frame=result["event_frames"]["start_frame"],
                    end_frame=result["event_frames"]["end_frame"],
                    duration_frames=case["render"]["duration_frames"],
                    output=output,
                    width=case["render"]["width"],
                    height=case["render"]["height"],
                    fps=case["render"]["fps"],
                )
                errors.extend(result.pop("boundary_errors_ms"))
                result["voice_id"] = voice["id"]
                result["output"] = str(output)
                result["output_probe"] = _probe_output(output)
                voice_results.append(result)
        except (
            OSError,
            RuntimeError,
            ValueError,
            subprocess.CalledProcessError,
        ) as error:
            status = "failed_alignment_or_render"
            unresolved = [case.get("required_event", "required event")]
            output_probe = None
            voice_results.append({"error": str(error)})
            duration_s = None
        else:
            status = "rendered"
            unresolved = []
            output_probe = {
                "voices": [result["output_probe"] for result in voice_results]
            }
            duration_s = max(durations)
    errors.sort()
    report = {
        "case_id": case["case_id"],
        "engine_version": runner.engine_version if runner else "whisperx-unavailable",
        "device": runner.device if runner else "unavailable",
        "duration_s": duration_s,
        "elapsed_s": round(time.monotonic() - started, 6),
        "peak_memory_mb": _peak_memory_mb(),
        "boundary_p95_ms": errors[math.ceil(len(errors) * 0.95) - 1]
        if errors
        else None,
        "annotated_boundary_count": annotation_count,
        "unresolved_required_events": unresolved,
        "output_probe": output_probe,
        "status": status,
        "missing_assets": missing,
        "input_probes": input_probes,
    }
    if "voice_results" in locals():
        report["voice_results"] = voice_results
    return report, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--asset-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--model", default="large-v2")
    parser.add_argument("--compute-type", default="int8")
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--model-cache", type=Path)
    parser.add_argument(
        "--fixture-kind",
        choices=("approved-real", "synthetic-local", "unclassified"),
        default="unclassified",
        help="Only approved-real fixtures may contribute to the production accuracy gate.",
    )
    parser.add_argument("--speech-concurrency", type=int, default=1)
    parser.add_argument("--asr-weight-version", default="unrecorded")
    parser.add_argument("--asr-weight-license", default="unrecorded")
    parser.add_argument("--alignment-weight-version", default="unrecorded")
    parser.add_argument("--alignment-weight-license", default="unrecorded")
    args = parser.parse_args()
    if args.speech_concurrency < 1:
        parser.error("--speech-concurrency must be at least 1")

    fixture_data = json.loads(args.cases.read_text(encoding="utf-8"))
    whisperx_available = importlib.util.find_spec("whisperx") is not None
    torch_available = importlib.util.find_spec("torch") is not None
    asr_executable = whisperx_available and torch_available
    runner = (
        WhisperXAlignmentRunner(
            device=args.device,
            model_name=args.model,
            compute_type=args.compute_type,
            batch_size=args.batch_size,
            model_cache=args.model_cache,
        )
        if asr_executable
        else None
    )
    case_runs = [
        _case_report(case, args.asset_root, args.output_dir, runner)
        for case in fixture_data["cases"]
    ]
    reports = [report for report, _errors in case_runs]
    language_errors = {
        language: [
            error
            for case, (_report, errors) in zip(
                fixture_data["cases"], case_runs, strict=True
            )
            if case["language"] == language
            for error in errors
        ]
        for language in ("zh", "en")
    }
    for errors in language_errors.values():
        errors.sort()
    counts = {
        language: sum(
            report["annotated_boundary_count"]
            for case, report in zip(fixture_data["cases"], reports, strict=True)
            if case["language"] == language
        )
        for language in ("zh", "en")
    }
    eligible_counts = counts if args.fixture_kind == "approved-real" else {"zh": 0, "en": 0}
    eligible_p95 = (
        {
            language: errors[math.ceil(len(errors) * 0.95) - 1]
            if errors
            else None
            for language, errors in language_errors.items()
        }
        if args.fixture_kind == "approved-real"
        else {"zh": None, "en": None}
    )
    gate = evaluate_p0_gate(
        cases=reports,
        asr_executable=asr_executable,
        annotated_boundary_counts=eligible_counts,
        boundary_p95_ms=eligible_p95,
    )
    if args.fixture_kind != "approved-real":
        gate["blockers"].insert(
            0,
            "Only approved-real fixtures and manual annotations qualify for production speech acceptance",
        )
    report = {
        "schema_version": 1,
        "fixture_manifest": str(args.cases),
        "fixture_kind": args.fixture_kind,
        "asset_root": str(args.asset_root),
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "ffmpeg": _command_version("ffmpeg"),
            "ffprobe": _command_version("ffprobe"),
            "whisperx_available": whisperx_available,
            "torch_available": torch_available,
        },
        "speech_runtime": {
            "whisperx_version": _package_version("whisperx"),
            "whisperx_license": "BSD-2-Clause",
            "model": args.model,
            "device": args.device,
            "compute_type": args.compute_type,
            "batch_size": args.batch_size,
            "concurrency_limit": args.speech_concurrency,
            "asr_weight": {
                "version": args.asr_weight_version,
                "license": args.asr_weight_license,
            },
            "alignment_weight": {
                "version": args.alignment_weight_version,
                "license": args.alignment_weight_license,
            },
            "platform_support": "measured host/device only; other operating systems and accelerators remain unverified",
            "downloads_allowed": False,
        },
        "cases": reports,
        "manual_boundary_counts": counts,
        "boundary_p95_ms": {
            language: errors[math.ceil(len(errors) * 0.95) - 1] if errors else None
            for language, errors in language_errors.items()
        },
        "p0_gate": gate,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    destination = args.output_dir / "report.json"
    destination.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(destination)
    print(f"P0 gate: {gate['status']}")
    return 0 if gate["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
