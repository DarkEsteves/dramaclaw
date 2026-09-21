#!/usr/bin/env python3
"""Write an auditable, bounded P0 feasibility report for reference-video remix.

This command never installs an ASR package or downloads model weights. It reports
the actual host capabilities and fixture availability so a missing engine cannot be
mistaken for an alignment result.
"""

from __future__ import annotations

import argparse
import hashlib
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
            "stream=codec_type,width,height,r_frame_rate,avg_frame_rate,time_base,duration",
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


_REQUIRED_CATEGORIES = {
    "clear_speech",
    "repeated_words_numbers_emoji",
    "no_speech",
    "script_mismatch",
    "vfr_crop_speed",
}
_PROVENANCE_FIELDS = ("source_id", "license", "approved_by")
_PLACEHOLDER_METADATA = {
    "",
    "unknown",
    "unrecorded",
    "unverified",
    "unverified-local-cache",
    "not-applicable",
    "synthetic-none",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_provenance(value: Any, *, fixture: bool = False) -> bool:
    return (
        isinstance(value, dict)
        and (not fixture or value.get("kind") == "approved-real")
        and all(
            isinstance(value.get(field), str) and value[field].strip()
            for field in _PROVENANCE_FIELDS
        )
        and (
            not fixture
            or all(
                isinstance(value.get(field), str) and value[field].strip()
                for field in ("approved_at", "approval_id")
            )
        )
    )


def _consumed_asset_paths(case: dict[str, Any]) -> list[str]:
    """Return every declared or structurally consumed input path for a case."""
    paths = {
        relative
        for relative in case.get("required_assets", [])
        if isinstance(relative, str) and relative
    }
    script_path = case.get("script_path")
    if isinstance(script_path, str) and script_path:
        paths.add(script_path)
    voices = case.get("voices")
    if isinstance(voices, list):
        for voice in voices:
            if not isinstance(voice, dict):
                continue
            for key in ("audio", "annotations"):
                relative = voice.get(key)
                if isinstance(relative, str) and relative:
                    paths.add(relative)
    render = case.get("render")
    if isinstance(render, dict):
        for key in ("background", "broll"):
            relative = render.get(key)
            if isinstance(relative, str) and relative:
                paths.add(relative)
    video_transform = case.get("video_transform")
    if isinstance(video_transform, dict):
        source = video_transform.get("source")
        if isinstance(source, str) and source:
            paths.add(source)
    return sorted(paths)


def _fixture_evidence_blockers(
    fixture_data: dict[str, Any], asset_root: Path
) -> list[str]:
    """Verify every fixture from local bytes and per-fixture provenance."""
    blockers: list[str] = []
    cases = fixture_data.get("cases")
    if not isinstance(cases, list):
        return ["fixture manifest must contain a cases array"]
    categories = {case.get("category") for case in cases if isinstance(case, dict)}
    if not _REQUIRED_CATEGORIES.issubset(categories) or len(cases) < 6:
        missing = sorted(_REQUIRED_CATEGORIES - categories)
        blockers.append(
            f"fixture manifest must cover all six categories; missing {missing}"
        )
    clear_languages = {
        case.get("language")
        for case in cases
        if isinstance(case, dict) and case.get("category") == "clear_speech"
    }
    if not {"zh", "en"}.issubset(clear_languages):
        blockers.append("clear speech fixtures must include both zh and en")

    for case in cases:
        if not isinstance(case, dict):
            blockers.append("fixture case must be an object")
            continue
        case_id = str(case.get("case_id", "unknown"))
        if not _valid_provenance(case.get("provenance"), fixture=True):
            blockers.append(
                f"{case_id} fixture provenance is missing or not approved-real"
            )
        inventory = case.get("asset_inventory")
        if not isinstance(inventory, dict):
            blockers.append(f"{case_id} asset inventory is missing")
            inventory = {}
        for relative in _consumed_asset_paths(case):
            path_value = Path(relative)
            if path_value.is_absolute() or ".." in path_value.parts:
                blockers.append(
                    f"{case_id} inventory path is not project relative: {relative}"
                )
                continue
            entry = inventory.get(relative)
            if not (
                _valid_provenance(entry)
                and isinstance(entry.get("retrieved_at"), str)
                and entry["retrieved_at"].strip()
            ):
                blockers.append(
                    f"{case_id} inventory provenance is missing for {relative}"
                )
                continue
            expected = entry.get("sha256")
            path = asset_root / path_value
            if not isinstance(expected, str) or len(expected) != 64:
                blockers.append(
                    f"{case_id} inventory SHA-256 is invalid for {relative}"
                )
            elif path.is_file() and _sha256(path) != expected.lower():
                blockers.append(f"{case_id} asset SHA-256 mismatch for {relative}")

        voices = case.get("voices") if isinstance(case.get("voices"), list) else []
        if case.get("category") == "clear_speech":
            rates = {
                voice.get("speech_rate") for voice in voices if isinstance(voice, dict)
            }
            if not {"fast", "slow"}.issubset(rates):
                blockers.append(f"{case_id} requires two speech rates: fast and slow")
            rate_audio = {
                voice.get("speech_rate"): asset_root / str(voice.get("audio", ""))
                for voice in voices
                if isinstance(voice, dict)
                and voice.get("speech_rate") in {"fast", "slow"}
            }
            if {"fast", "slow"}.issubset(rate_audio):
                fast_path, slow_path = rate_audio["fast"], rate_audio["slow"]
                if (
                    fast_path == slow_path
                    or not fast_path.is_file()
                    or not slow_path.is_file()
                    or _sha256(fast_path) == _sha256(slow_path)
                ):
                    blockers.append(
                        f"{case_id} fast and slow speech rates must use distinct approved audio bytes"
                    )
        for voice in voices:
            if not isinstance(voice, dict) or "annotations" not in voice:
                continue
            annotation_path = asset_root / str(voice["annotations"])
            try:
                annotation = json.loads(annotation_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            provenance = annotation.get("provenance")
            audio_path = asset_root / str(voice.get("audio", ""))
            source_hash = _sha256(audio_path) if audio_path.is_file() else None
            if not (
                isinstance(provenance, dict)
                and provenance.get("method") == "manual"
                and isinstance(provenance.get("annotator_ids"), list)
                and bool(provenance["annotator_ids"])
                and isinstance(provenance.get("reviewed_by"), str)
                and bool(provenance["reviewed_by"].strip())
                and isinstance(provenance.get("annotated_at"), str)
                and bool(provenance["annotated_at"].strip())
                and provenance.get("source_audio_sha256") == source_hash
            ):
                blockers.append(
                    f"{case_id}/{voice.get('id', 'voice')} manual annotation provenance is missing or does not match its audio"
                )
    return blockers


def _runtime_metadata_blockers(metadata: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    checks = {
        "WhisperX version": metadata.get("whisperx_version"),
        "WhisperX license": metadata.get("whisperx_license"),
        "ASR model name": metadata.get("model"),
        "ASR weight version": metadata.get("asr_weight", {}).get("version"),
        "ASR weight license": metadata.get("asr_weight", {}).get("license"),
        "alignment weight version": metadata.get("alignment_weight", {}).get("version"),
        "alignment weight license": metadata.get("alignment_weight", {}).get("license"),
        "platform support record": metadata.get("platform_support"),
    }
    for label, value in checks.items():
        normalized = str(value or "").strip().casefold()
        if normalized in _PLACEHOLDER_METADATA or normalized.startswith(
            ("synthetic", "unverified")
        ):
            blockers.append(f"{label} must be explicitly recorded and non-placeholder")
    return blockers


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


def _run_vfr_transform(
    case: dict[str, Any], asset_root: Path, output_dir: Path
) -> dict[str, Any]:
    transform = case["video_transform"]
    source = asset_root / transform["source"]
    source_probe = _probe_output(source)
    video = next(
        stream for stream in source_probe["streams"] if stream["codec_type"] == "video"
    )
    variable = video.get("r_frame_rate") != video.get("avg_frame_rate")
    if not variable:
        raise ValueError("VFR fixture does not have variable frame timing")
    crop = transform["crop"]
    speed = float(transform["speed"])
    if speed <= 0:
        raise ValueError("VFR speed must be positive")
    output = output_dir / case["case_id"] / "vfr-crop-speed.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-vf",
            f"crop={crop['width']}:{crop['height']}:{crop['x']}:{crop['y']},setpts=PTS/{speed:g}",
            "-an",
            "-fps_mode",
            "vfr",
            str(output),
        ],
        check=True,
    )
    output_probe = _probe_output(output)
    output_video = next(
        stream for stream in output_probe["streams"] if stream["codec_type"] == "video"
    )
    if (output_video.get("width"), output_video.get("height")) != (
        crop["width"],
        crop["height"],
    ):
        raise ValueError("VFR crop dimensions do not match the requested output")
    source_duration = _media_duration_s(source)
    output_duration = _media_duration_s(output)
    expected_duration = source_duration / speed
    if abs(output_duration - expected_duration) > max(0.1, expected_duration * 0.15):
        raise ValueError("VFR speed transform duration is outside tolerance")
    return {
        "source_variable_frame_rate": True,
        "source_probe": source_probe,
        "output": str(output),
        "output_probe": output_probe,
        "speed": speed,
        "source_duration_s": source_duration,
        "output_duration_s": output_duration,
    }


def _validate_repeated_script_features(case: dict[str, Any], script: str) -> None:
    features = case.get("required_script_features")
    if not isinstance(features, dict):
        raise ValueError("repeated-word fixture must declare required script features")
    repeated = str(features.get("repeated_text") or "")
    number = str(features.get("number") or "")
    emoji = str(features.get("emoji") or "")
    if not repeated or script.count(repeated) < 2:
        raise ValueError(
            "repeated-word fixture does not contain the repeated text twice"
        )
    if not number or number not in script:
        raise ValueError("repeated-word fixture does not contain the required number")
    if not emoji or emoji not in script:
        raise ValueError("repeated-word fixture does not contain the required emoji")


def _case_report(
    case: dict[str, Any],
    asset_root: Path,
    output_dir: Path,
    runner: WhisperXAlignmentRunner | None,
) -> tuple[dict[str, Any], list[float]]:
    started = time.monotonic()
    required_assets = [
        asset_root / relative for relative in _consumed_asset_paths(case)
    ]
    missing = [
        str(path.relative_to(asset_root))
        for path in required_assets
        if not path.is_file()
    ]
    input_probes = _input_probes(
        [path for path in required_assets if path.is_file()], asset_root
    )
    category = case["category"]
    status = "blocked_fixture_missing"
    unresolved = [case.get("required_event", "fixture validation")]
    output_probe: dict[str, Any] | None = None
    errors: list[float] = []
    duration_s: float | None = None
    annotation_count = 0
    voice_results: list[dict[str, Any]] = []
    vfr_transform: dict[str, Any] | None = None
    if missing:
        pass
    elif runner is None:
        status = "blocked_alignment_not_run"
        unresolved = [case.get("required_event", "required event")]
    elif category in {"no_speech", "script_mismatch"}:
        try:
            script = (
                (asset_root / case["script_path"]).read_text(encoding="utf-8").strip()
            )
            expected_message = (
                "ASR produced no speech segments"
                if category == "no_speech"
                else "ASR transcript does not match the controlled fixture script"
            )
            for voice in case["voices"]:
                try:
                    runner.align(
                        asset_root / voice["audio"],
                        language=case["language"],
                        script=script,
                    )
                except ValueError as error:
                    if expected_message not in str(error):
                        raise
                    voice_results.append(
                        {"voice_id": voice["id"], "observed_error": str(error)}
                    )
                else:
                    raise ValueError(
                        f"{voice['id']} did not produce expected {voice['expected']} result"
                    )
        except (OSError, RuntimeError, ValueError, KeyError) as error:
            status = "failed_expected_outcome"
            unresolved = [case.get("required_event", "fixture validation")]
            voice_results.append({"error": str(error)})
        else:
            status = (
                "expected_no_speech"
                if category == "no_speech"
                else "expected_unresolved"
            )
            unresolved = [
                "speech timeline" if category == "no_speech" else "script mismatch"
            ]
    else:
        durations: list[float] = []
        try:
            script = (
                (asset_root / case["script_path"]).read_text(encoding="utf-8").strip()
            )
            if category == "repeated_words_numbers_emoji":
                _validate_repeated_script_features(case, script)
            fps = int(case.get("fps") or case.get("render", {}).get("fps") or 30)
            if category == "vfr_crop_speed":
                vfr_transform = _run_vfr_transform(case, asset_root, output_dir)
            for voice in case["voices"]:
                annotations = _load_annotations(asset_root / voice["annotations"])
                annotation_count += len(annotations)
                result = execute_alignment_case(
                    case=case,
                    audio=asset_root / voice["audio"],
                    script=script,
                    annotations=annotations,
                    runner=runner,
                    fps=fps,
                )
                durations.append(_media_duration_s(asset_root / voice["audio"]))
                errors.extend(result.pop("boundary_errors_ms"))
                result["voice_id"] = voice["id"]
                if "render" in case:
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
                        fps=fps,
                    )
                    result["output"] = str(output)
                    result["output_probe"] = _probe_output(output)
                voice_results.append(result)
        except (
            OSError,
            RuntimeError,
            ValueError,
            KeyError,
            subprocess.CalledProcessError,
        ) as error:
            status = "failed_alignment_or_render"
            unresolved = [case.get("required_event", "required event")]
            output_probe = None
            voice_results.append({"error": str(error)})
            duration_s = None
        else:
            status = "rendered" if "render" in case else "validated"
            unresolved = []
            if "render" in case:
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
        "process_cumulative_high_water_mb": _peak_memory_mb(),
        "memory_scope": "process cumulative high-water mark at case completion",
        "boundary_p95_ms": (
            errors[math.ceil(len(errors) * 0.95) - 1] if errors else None
        ),
        "annotated_boundary_count": annotation_count,
        "unresolved_required_events": unresolved,
        "output_probe": output_probe,
        "status": status,
        "missing_assets": missing,
        "input_probes": input_probes,
    }
    if voice_results:
        report["voice_results"] = voice_results
    if vfr_transform is not None:
        report["vfr_transform"] = vfr_transform
    return report, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--asset-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--device", default="cpu")
    parser.add_argument(
        "--model", help="Explicit ASR model name; required for a passing run"
    )
    parser.add_argument("--compute-type", default="int8")
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--model-cache", type=Path)
    parser.add_argument(
        "--fixture-kind",
        choices=("approved-real", "synthetic-local", "unclassified"),
        default="unclassified",
        help="Human-readable run label; gate qualification uses per-fixture evidence.",
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
    asr_executable = whisperx_available and torch_available and bool(args.model)
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
            if case["language"] == language and case["category"] == "clear_speech"
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
            if case["language"] == language and case["category"] == "clear_speech"
        )
        for language in ("zh", "en")
    }
    runtime_metadata = {
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
    }
    evidence_blockers = _fixture_evidence_blockers(fixture_data, args.asset_root)
    metadata_blockers = _runtime_metadata_blockers(runtime_metadata)
    evidence_qualified = not evidence_blockers
    eligible_counts = counts if evidence_qualified else {"zh": 0, "en": 0}
    eligible_p95 = (
        {
            language: errors[math.ceil(len(errors) * 0.95) - 1] if errors else None
            for language, errors in language_errors.items()
        }
        if evidence_qualified
        else {"zh": None, "en": None}
    )
    gate = evaluate_p0_gate(
        cases=reports,
        asr_executable=asr_executable,
        annotated_boundary_counts=eligible_counts,
        boundary_p95_ms=eligible_p95,
    )
    gate["blockers"][:0] = evidence_blockers + metadata_blockers
    gate["status"] = "passed" if not gate["blockers"] else "blocked"
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
        "speech_runtime": runtime_metadata,
        "fixture_evidence": {
            "verified": evidence_qualified,
            "blockers": evidence_blockers,
        },
        "cases": reports,
        "manual_boundary_counts": counts,
        "gate_manual_boundary_counts": eligible_counts,
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
