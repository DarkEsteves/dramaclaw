"""Full-timeline video evidence extraction for reference-video remixes."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import Any


DEFAULT_REPRESENTATIVE_FRAME_BUDGET = 12
SCENE_CUT_THRESHOLD = 0.3
_MICROSECONDS_PER_SECOND = 1_000_000


def build_reference_evidence(media_path: Path) -> dict[str, Any]:
    """Inspect every video frame and return bounded, timestamped evidence.

    The input must be a local readable media file.  The result is the payload
    data for ``reference_evidence.v1``: its video time base and representative
    frame PTS values retain the source clock, while ``duration_us`` and
    ``time_us`` provide exact integer-microsecond values rounded upward only for
    whole-media duration.  Representative frames are evenly selected from the
    complete decoded frame list, always including the first and final frame.

    This function does not write extracted images. Later artifact storage owns
    stable paths for thumbnails; the returned PTS/time-base pairs are enough to
    extract the same source frames deterministically.
    """
    if not media_path.is_file():
        raise FileNotFoundError(media_path)
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is required to build reference evidence")

    stream = _probe_stream(media_path)
    time_base = _parse_time_base(stream.get("time_base"))
    frames = _probe_all_video_frames(media_path)
    if not frames:
        raise ValueError("media contains no video frames")

    duration_us = _duration_us(stream, frames, time_base)
    representatives = _representative_frames(frames, stream["time_base"], time_base)
    cuts = _detect_scene_cuts(media_path, stream["time_base"], time_base)
    return {
        "schema": "reference_evidence.v1",
        "media_hash": _sha256(media_path),
        "time_base": stream["time_base"],
        "duration_us": duration_us,
        "frame_count": len(frames),
        "cuts": cuts,
        "keyframes": [
            {"pts": frame["pts"], "time_us": _pts_to_us(frame["pts"], time_base)}
            for frame in frames
            if frame["key_frame"]
        ],
        "representative_frames": representatives,
    }


def _probe_stream(media_path: Path) -> dict[str, Any]:
    payload = _run_ffprobe(
        media_path,
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=time_base,duration_ts",
        "-show_streams",
    )
    streams = payload.get("streams", [])
    if not streams or not isinstance(streams[0], dict):
        raise ValueError("media contains no video stream")
    stream = streams[0]
    if not isinstance(stream.get("time_base"), str):
        raise ValueError("video stream has no time base")
    return stream


def _probe_all_video_frames(media_path: Path) -> list[dict[str, int]]:
    payload = _run_ffprobe(
        media_path,
        "-select_streams",
        "v:0",
        "-show_entries",
        "frame=best_effort_timestamp,key_frame,pkt_duration",
        "-show_frames",
    )
    frames: list[dict[str, int]] = []
    for raw_frame in payload.get("frames", []):
        if not isinstance(raw_frame, dict):
            continue
        raw_pts = raw_frame.get("best_effort_timestamp")
        if raw_pts in {None, "N/A"}:
            continue
        try:
            pts = int(raw_pts)
            duration = int(raw_frame.get("pkt_duration") or 0)
            key_frame = int(raw_frame.get("key_frame") or 0)
        except (TypeError, ValueError) as error:
            raise ValueError("video frame has invalid PTS data") from error
        frames.append({"pts": pts, "duration": duration, "key_frame": key_frame == 1})
    return frames


def _run_ffprobe(media_path: Path, *arguments: str) -> dict[str, Any]:
    process = subprocess.run(
        ["ffprobe", "-v", "error", *arguments, "-of", "json", str(media_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode:
        detail = process.stderr.strip()[-500:] or "unknown ffprobe error"
        raise ValueError(f"ffprobe failed: {detail}")
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise ValueError("ffprobe returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("ffprobe returned an invalid payload")
    return payload


def _parse_time_base(raw_time_base: object) -> Fraction:
    if not isinstance(raw_time_base, str):
        raise ValueError("video stream has no time base")
    try:
        numerator, denominator = raw_time_base.split("/", 1)
        result = Fraction(int(numerator), int(denominator))
    except (TypeError, ValueError, ZeroDivisionError) as error:
        raise ValueError("video stream has an invalid time base") from error
    if result <= 0:
        raise ValueError("video stream has an invalid time base")
    return result


def _duration_us(stream: dict[str, Any], frames: list[dict[str, int]], time_base: Fraction) -> int:
    duration_ts = stream.get("duration_ts")
    try:
        duration_ticks = int(duration_ts)
    except (TypeError, ValueError):
        last_frame = frames[-1]
        duration_ticks = last_frame["pts"] + max(last_frame["duration"], 1)
    if duration_ticks <= 0:
        raise ValueError("video stream has an invalid duration")
    numerator = duration_ticks * time_base.numerator * _MICROSECONDS_PER_SECOND
    denominator = time_base.denominator
    return -(-numerator // denominator)


def _detect_scene_cuts(media_path: Path, raw_time_base: str, time_base: Fraction) -> list[dict[str, Any]]:
    """Run FFmpeg's pixel-difference detector over the complete source stream."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required to detect reference-video cuts")
    with tempfile.TemporaryDirectory(prefix="remix-scene-") as temporary:
        metadata_path = Path(temporary) / "scene-metadata.txt"
        process = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-i",
                str(media_path),
                "-vf",
                f"select='gt(scene,{SCENE_CUT_THRESHOLD})',metadata=print:file={metadata_path}",
                "-an",
                "-f",
                "null",
                "-",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if process.returncode:
            detail = process.stderr.strip()[-500:] or "unknown ffmpeg error"
            raise ValueError(f"ffmpeg scene detection failed: {detail}")
        contents = metadata_path.read_text(encoding="utf-8") if metadata_path.exists() else ""
    return _parse_scene_metadata(contents, raw_time_base, time_base)


def _parse_scene_metadata(contents: str, raw_time_base: str, time_base: Fraction) -> list[dict[str, Any]]:
    cuts: list[dict[str, Any]] = []
    current_pts: int | None = None
    for line in contents.splitlines():
        fields = line.split()
        if line.startswith("frame:"):
            current_pts = None
            for field in fields:
                if field.startswith("pts:"):
                    current_pts = int(field.removeprefix("pts:"))
                    break
        elif line.startswith("lavfi.scene_score=") and current_pts is not None:
            score = float(line.removeprefix("lavfi.scene_score="))
            cuts.append(
                {
                    "pts": current_pts,
                    "time_base": raw_time_base,
                    "time_us": _pts_to_us(current_pts, time_base),
                    "scene_score": score,
                }
            )
    return cuts


def _representative_frames(
    frames: list[dict[str, int]], raw_time_base: str, time_base: Fraction
) -> list[dict[str, Any]]:
    count = min(len(frames), DEFAULT_REPRESENTATIVE_FRAME_BUDGET)
    if count == 1:
        indexes = [0]
    else:
        # Choose by source-clock time rather than decoded frame ordinal.  Frame
        # ordinal sampling over-represents a high-frame-rate section of VFR
        # media and can leave the middle of the actual video unobserved.
        first_pts = frames[0]["pts"]
        last_pts = frames[-1]["pts"]
        indexes = []
        # A VFR gap can make two adjacent target times resolve to the same
        # decoded frame. Reduce the sample count until all retained samples
        # still represent evenly spaced source-clock targets.
        for target_count in range(count, 1, -1):
            candidates = [
                min(
                    range(len(frames)),
                    key=lambda candidate: (
                        abs(
                            frames[candidate]["pts"]
                            - (first_pts + (last_pts - first_pts) * slot / (target_count - 1))
                        ),
                        candidate,
                    ),
                )
                for slot in range(target_count)
            ]
            if len(set(candidates)) == len(candidates):
                indexes = candidates
                break
        if not indexes:
            indexes = [0, len(frames) - 1]
    return [
        {
            "frame_index": index,
            "pts": frames[index]["pts"],
            "time_base": raw_time_base,
            "time_us": _pts_to_us(frames[index]["pts"], time_base),
            "key_frame": frames[index]["key_frame"],
        }
        for index in indexes
    ]


def _pts_to_us(pts: int, time_base: Fraction) -> int:
    return pts * time_base.numerator * _MICROSECONDS_PER_SECOND // time_base.denominator


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1_048_576), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"
