"""P0: a real FFmpeg render must follow the aligned narration clock."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from novelvideo.freezone.remix import p0
from novelvideo.freezone.remix.p0 import compile_events, render_sample


ROOT = Path(__file__).resolve().parents[1]


def _remix_client() -> TestClient:
    from novelvideo.api.auth import get_api_user
    from novelvideo.api.routes import remix

    app = FastAPI()
    app.include_router(remix.router, prefix="/api/v1")
    app.dependency_overrides[get_api_user] = lambda: {"username": "local-test"}
    return TestClient(app)


def test_speech_readiness_accepts_configured_executable_and_cache_without_running_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "models"
    cache.mkdir()
    python = tmp_path / "whisperx-python"
    python.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    python.chmod(0o755)
    monkeypatch.setenv("DRAMACLAW_REMIX_SPEECH_ENGINE", "whisperx")
    monkeypatch.setenv("DRAMACLAW_REMIX_MODEL_CACHE", str(cache))
    monkeypatch.setenv("DRAMACLAW_REMIX_WHISPERX_PYTHON", str(python))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("readiness must not execute or download anything"),
    )

    response = _remix_client().get(
        "/api/v1/projects/local/freezone/remix/speech-readiness"
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "data": {
            "ready": True,
            "engine": "whisperx",
            "python": "external",
            "model_cache": "available",
            "downloads_allowed": False,
            "error": None,
        },
    }


def test_speech_readiness_reports_missing_cache_without_downloading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = tmp_path / "whisperx-python"
    python.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    python.chmod(0o755)
    missing = tmp_path / "missing-model-cache"
    monkeypatch.setenv("DRAMACLAW_REMIX_SPEECH_ENGINE", "whisperx")
    monkeypatch.setenv("DRAMACLAW_REMIX_MODEL_CACHE", str(missing))
    monkeypatch.setenv("DRAMACLAW_REMIX_WHISPERX_PYTHON", str(python))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("readiness must not execute or download anything"),
    )

    response = _remix_client().get(
        "/api/v1/projects/local/freezone/remix/speech-readiness"
    )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "ready": False,
        "engine": "whisperx",
        "python": "external",
        "model_cache": "missing",
        "downloads_allowed": False,
        "error": f"WhisperX model cache directory is missing: {missing}",
    }


def test_local_speech_launcher_pins_ce_paths_and_checks_readiness() -> None:
    launcher = ROOT / "scripts" / "start-remix-speech.sh"

    result = subprocess.run(
        ["bash", "-n", str(launcher)], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr
    source = launcher.read_text(encoding="utf-8")
    assert "DRAMACLAW_REMIX_SPEECH_ENGINE=whisperx" in source
    assert "DRAMACLAW_REMIX_WHISPERX_PYTHON" in source
    assert "DRAMACLAW_REMIX_MODEL_CACHE" in source
    assert "ST_EDITION=ce" in source
    assert "ST_CONTROL_PLANE_DSN=" in source
    assert "/speech-readiness" in source
    assert "API_KEY" not in source


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required"
)
def test_p0_command_records_synthetic_fixture_evidence_without_qualifying_it(
    tmp_path: Path,
) -> None:
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    source = asset_root / "silent.mp4"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=160x90:r=30:d=0.2",
        "-an",
        str(source),
    )
    manifest = tmp_path / "cases.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "cases": [
                    {
                        "case_id": "no-speech",
                        "language": "en",
                        "category": "no_speech",
                        "required_assets": ["silent.mp4"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "report"

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "experiments" / "reference_video_remix_p0.py"),
            "--cases",
            str(manifest),
            "--asset-root",
            str(asset_root),
            "--output-dir",
            str(output_dir),
            "--fixture-kind",
            "synthetic-local",
            "--speech-concurrency",
            "1",
            "--asr-weight-version",
            "synthetic-none",
            "--asr-weight-license",
            "not-applicable",
            "--alignment-weight-version",
            "synthetic-none",
            "--alignment-weight-license",
            "not-applicable",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2, result.stderr
    report = json.loads((output_dir / "report.json").read_text(encoding="utf-8"))
    assert report["fixture_kind"] == "synthetic-local"
    assert report["host"]["platform"]
    assert report["speech_runtime"]["concurrency_limit"] == 1
    assert report["speech_runtime"]["asr_weight"] == {
        "version": "synthetic-none",
        "license": "not-applicable",
    }
    assert report["cases"][0]["elapsed_s"] >= 0
    assert report["cases"][0]["peak_memory_mb"] > 0
    assert report["cases"][0]["input_probes"]["silent.mp4"]["streams"][0][
        "codec_type"
    ] == "video"
    assert any(
        "approved-real" in blocker for blocker in report["p0_gate"]["blockers"]
    )


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def _probe(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _pixel_at(path: Path, *, at_seconds: float) -> tuple[int, int, int]:
    """Return the center RGB pixel from an exact decoded output frame."""
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            str(at_seconds),
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            "crop=2:2:90:160",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
    )
    return tuple(result.stdout[:3])


def _audio_peak(path: Path, *, at_seconds: float) -> int:
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            str(at_seconds),
            "-i",
            str(path),
            "-t",
            "0.05",
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            "8000",
            "-f",
            "s16le",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
    )
    samples = memoryview(result.stdout).cast("h")
    return max(abs(sample) for sample in samples)


def test_compile_events_uses_new_voice_boundaries() -> None:
    selection = {"id": "product", "start_char": 4, "end_char": 6}
    fast = [
        {
            "text": "产品",
            "start_char": 4,
            "end_char": 6,
            "start_us": 800_000,
            "end_us": 1_200_000,
        }
    ]
    slow = [
        {
            "text": "产品",
            "start_char": 4,
            "end_char": 6,
            "start_us": 2_100_000,
            "end_us": 2_900_000,
        }
    ]

    assert compile_events(selection, fast, fps=30) == {
        "start_frame": 24,
        "end_frame": 36,
    }
    assert compile_events(selection, slow, fps=30) == {
        "start_frame": 63,
        "end_frame": 87,
    }


def test_compile_events_rejects_missing_necessary_selection() -> None:
    with pytest.raises(ValueError, match="unresolved selection"):
        compile_events({"id": "product", "start_char": 4, "end_char": 6}, [], fps=30)


def test_compile_events_rejects_a_gap_inside_required_selection() -> None:
    with pytest.raises(ValueError, match="unresolved selection"):
        compile_events(
            {"id": "product", "start_char": 4, "end_char": 7},
            [
                {
                    "start_char": 4,
                    "end_char": 5,
                    "start_us": 100_000,
                    "end_us": 200_000,
                },
                {
                    "start_char": 6,
                    "end_char": 7,
                    "start_us": 300_000,
                    "end_us": 400_000,
                },
            ],
            fps=30,
        )


@pytest.mark.parametrize("start_us,end_us", [(100_000, 100_000), (200_000, 100_000)])
def test_compile_events_rejects_zero_or_negative_aligned_spans(
    start_us: int, end_us: int
) -> None:
    with pytest.raises(ValueError, match="invalid aligned time"):
        compile_events(
            {"id": "product", "start_char": 4, "end_char": 6},
            [
                {
                    "start_char": 4,
                    "end_char": 5,
                    "start_us": start_us,
                    "end_us": end_us,
                },
                {
                    "start_char": 5,
                    "end_char": 6,
                    "start_us": 300_000,
                    "end_us": 400_000,
                },
            ],
            fps=30,
        )


def test_alignment_case_uses_injected_runner_and_manual_boundaries() -> None:
    class FakeAligner:
        engine_version = "fake-aligner-for-test"
        device = "test"

        def align(self, audio: Path, *, language: str, script: str) -> list[dict]:
            assert audio == Path("voice.wav")
            assert language == "en"
            assert script == "new product"
            return [
                {
                    "start_char": 0,
                    "end_char": 3,
                    "start_us": 100_000,
                    "end_us": 400_000,
                },
                {
                    "start_char": 4,
                    "end_char": 11,
                    "start_us": 500_000,
                    "end_us": 900_000,
                },
            ]

    result = p0.execute_alignment_case(
        case={
            "case_id": "en-clear",
            "language": "en",
            "required_event_selection": {
                "id": "product",
                "start_char": 4,
                "end_char": 11,
            },
        },
        audio=Path("voice.wav"),
        script="new product",
        annotations=[
            {"start_char": 0, "end_char": 3, "start_us": 110_000, "end_us": 390_000},
            {"start_char": 4, "end_char": 11, "start_us": 500_000, "end_us": 910_000},
        ],
        runner=FakeAligner(),
        fps=30,
    )

    assert result["engine_version"] == "fake-aligner-for-test"
    assert result["boundary_p95_ms"] == 10.0
    assert result["annotated_boundary_count"] == 2
    assert result["event_frames"] == {"start_frame": 15, "end_frame": 27}


def test_boundary_p95_rejects_annotation_without_aligned_span() -> None:
    with pytest.raises(ValueError, match="no aligned span"):
        p0.boundary_p95_ms(
            [{"start_char": 0, "end_char": 2, "start_us": 0, "end_us": 10}],
            [{"start_char": 3, "end_char": 4, "start_us": 0, "end_us": 10}],
        )


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required"
)
def test_render_sample_overlays_broll_and_preserves_narration(tmp_path: Path) -> None:
    background = tmp_path / "background.png"
    broll = tmp_path / "broll.mp4"
    narration = tmp_path / "narration.wav"
    output = tmp_path / "output.mp4"
    _ffmpeg(
        "-f", "lavfi", "-i", "color=c=blue:s=180x320", "-frames:v", "1", str(background)
    )
    _ffmpeg("-f", "lavfi", "-i", "color=c=red:s=180x320:r=30:d=1", "-an", str(broll))
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(narration))

    render_sample(
        background=background,
        broll=broll,
        narration=narration,
        caption="Product",
        start_frame=15,
        end_frame=45,
        duration_frames=60,
        output=output,
        width=180,
        height=320,
        fps=30,
    )

    streams = _probe(output)["streams"]
    video = next(stream for stream in streams if stream["codec_type"] == "video")
    assert (video["width"], video["height"]) == (180, 320)
    assert video["r_frame_rate"] == "30/1"
    audio = next(stream for stream in streams if stream["codec_type"] == "audio")
    assert float(audio["duration"]) >= 1.95
    # Red B-roll is visible only on frames [15, 45), which map to [0.5, 1.5) seconds.
    before = _pixel_at(output, at_seconds=0.25)
    during = _pixel_at(output, at_seconds=0.75)
    after = _pixel_at(output, at_seconds=1.75)
    assert before[2] > before[0] * 2
    assert during[0] > during[2] * 2
    assert after[2] > after[0] * 2
    assert _audio_peak(output, at_seconds=0.25) > 100
    assert _audio_peak(output, at_seconds=0.75) > 100
    assert _audio_peak(output, at_seconds=1.75) > 100


def test_p0_gate_is_blocked_without_executable_asr_or_100_boundaries() -> None:
    report = p0.evaluate_p0_gate(
        cases=[
            {
                "case_id": "zh-clear",
                "status": "rendered",
                "unresolved_required_events": [],
            },
            {
                "case_id": "en-clear",
                "status": "rendered",
                "unresolved_required_events": [],
            },
        ],
        asr_executable=False,
        annotated_boundary_counts={"zh": 0, "en": 0},
        boundary_p95_ms={"zh": None, "en": None},
    )

    assert report["status"] == "blocked"
    assert "ASR/forced alignment is not executable" in report["blockers"]
    assert any(
        item.startswith("zh requires 100 manually annotated boundaries")
        for item in report["blockers"]
    )
