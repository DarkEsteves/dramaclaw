"""Evidence extraction must scan the complete source timeline."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from novelvideo.freezone.remix.evidence import (
    DEFAULT_REPRESENTATIVE_FRAME_BUDGET,
    build_reference_evidence,
)


pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg is required"
)


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def _video_stream(path: Path) -> dict[str, object]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=time_base,duration_ts",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)["streams"][0]


def test_build_reference_evidence_preserves_source_pts_and_covers_late_frames(tmp_path: Path) -> None:
    video = tmp_path / "source.mp4"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=64x64:rate=5:duration=5",
        "-an",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(video),
    )

    evidence = build_reference_evidence(video)
    stream = _video_stream(video)

    assert evidence["schema"] == "reference_evidence.v1"
    assert evidence["time_base"] == stream["time_base"]
    assert evidence["duration_us"] == 5_000_000
    assert evidence["frame_count"] == 25
    assert 2 <= len(evidence["representative_frames"]) <= DEFAULT_REPRESENTATIVE_FRAME_BUDGET
    assert evidence["representative_frames"][0]["pts"] == 0
    assert evidence["representative_frames"][-1]["pts"] < int(stream["duration_ts"])
    assert evidence["representative_frames"][-1]["time_us"] == 4_800_000
    assert evidence["representative_frames"][-1]["time_base"] == stream["time_base"]


def test_build_reference_evidence_rejects_missing_media(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        build_reference_evidence(tmp_path / "missing.mp4")


def test_build_reference_evidence_detects_visual_cut_without_codec_keyframe(tmp_path: Path) -> None:
    video = tmp_path / "hard-cut.mp4"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=c=red:s=64x64:r=5:d=2",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=64x64:r=5:d=3",
        "-filter_complex",
        "[0:v][1:v]concat=n=2:v=1:a=0",
        "-an",
        "-c:v",
        "libx264",
        "-x264-params",
        "scenecut=0:keyint=250:min-keyint=250",
        "-pix_fmt",
        "yuv420p",
        str(video),
    )

    evidence = build_reference_evidence(video)

    assert evidence["cuts"][0]["pts"] == 20_480
    assert evidence["cuts"][0]["time_us"] == 2_000_000
    assert evidence["cuts"][0]["time_base"] == evidence["time_base"]
    assert evidence["cuts"][0]["scene_score"] == pytest.approx(0.4)
    assert evidence["keyframes"] == [{"pts": 0, "time_us": 0}]
    assert evidence["representative_frames"][-1]["time_us"] == 4_800_000
