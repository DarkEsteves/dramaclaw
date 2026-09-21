"""P0: a real FFmpeg render must follow the aligned narration clock."""

from __future__ import annotations

import json
import hashlib
import importlib.util
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


def _harness_module():
    path = ROOT / "scripts" / "experiments" / "reference_video_remix_p0.py"
    spec = importlib.util.spec_from_file_location("reference_video_remix_p0", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_cached_models(cache: Path) -> None:
    asr = cache / "models--Systran--faster-whisper-small" / "snapshots" / "asr-rev"
    asr.mkdir(parents=True)
    for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt"):
        (asr / name).write_text(name, encoding="utf-8")
    zh = (
        cache
        / "models--jonatasgrosman--wav2vec2-large-xlsr-53-chinese-zh-cn"
        / "snapshots"
        / "align-rev"
    )
    zh.mkdir(parents=True)
    for name in ("pytorch_model.bin", "config.json", "vocab.json"):
        (zh / name).write_text(name, encoding="utf-8")
    (cache / "wav2vec2_fairseq_base_ls960_asr_ls960.pth").write_bytes(b"model")


def _remix_client() -> TestClient:
    from novelvideo.api.auth import get_api_user
    from novelvideo.api.routes import remix

    app = FastAPI()
    app.include_router(remix.router, prefix="/api/v1")
    app.dependency_overrides[get_api_user] = lambda: {"username": "local-test"}
    return TestClient(app)


def test_speech_readiness_probes_whisperx_import_and_expected_cached_models_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "models"
    _write_cached_models(cache)
    python = tmp_path / "whisperx-python"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    monkeypatch.setenv("DRAMACLAW_REMIX_SPEECH_ENGINE", "whisperx")
    monkeypatch.setenv("DRAMACLAW_REMIX_MODEL_CACHE", str(cache))
    monkeypatch.setenv("DRAMACLAW_REMIX_WHISPERX_PYTHON", str(python))
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        assert kwargs["env"]["HF_HUB_OFFLINE"] == "1"
        assert kwargs["env"]["TRANSFORMERS_OFFLINE"] == "1"
        return subprocess.CompletedProcess(command, 0, "3.8.6\n", "")

    from novelvideo.api.routes import remix as routes

    monkeypatch.setattr(routes.subprocess, "run", fake_run)

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
            "whisperx_version": "3.8.6",
            "downloads_allowed": False,
            "error": None,
        },
    }
    assert len(calls) == 1


def test_speech_readiness_reports_missing_cache_without_downloading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = tmp_path / "whisperx-python"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    missing = tmp_path / "missing-model-cache"
    monkeypatch.setenv("DRAMACLAW_REMIX_SPEECH_ENGINE", "whisperx")
    monkeypatch.setenv("DRAMACLAW_REMIX_MODEL_CACHE", str(missing))
    monkeypatch.setenv("DRAMACLAW_REMIX_WHISPERX_PYTHON", str(python))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail(
            "readiness must not execute or download anything"
        ),
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
        "whisperx_version": None,
        "downloads_allowed": False,
        "error": f"WhisperX model cache directory is missing: {missing}",
    }


def test_speech_readiness_rejects_incomplete_cache_before_import_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "models"
    cache.mkdir()
    python = tmp_path / "whisperx-python"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    monkeypatch.setenv("DRAMACLAW_REMIX_SPEECH_ENGINE", "whisperx")
    monkeypatch.setenv("DRAMACLAW_REMIX_MODEL_CACHE", str(cache))
    monkeypatch.setenv("DRAMACLAW_REMIX_WHISPERX_PYTHON", str(python))
    from novelvideo.api.routes import remix as routes

    calls = []
    monkeypatch.setattr(
        routes.subprocess,
        "run",
        lambda *args, **kwargs: calls.append(args)
        or subprocess.CompletedProcess(args[0], 1, "", "cannot import whisperx"),
    )

    response = _remix_client().get(
        "/api/v1/projects/local/freezone/remix/speech-readiness"
    )

    assert response.json()["data"]["ready"] is False
    assert "cached model files are incomplete" in response.json()["data"]["error"]
    assert calls == []


def test_speech_readiness_rejects_python_that_cannot_import_whisperx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "models"
    _write_cached_models(cache)
    python = tmp_path / "whisperx-python"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    monkeypatch.setenv("DRAMACLAW_REMIX_SPEECH_ENGINE", "whisperx")
    monkeypatch.setenv("DRAMACLAW_REMIX_MODEL_CACHE", str(cache))
    monkeypatch.setenv("DRAMACLAW_REMIX_WHISPERX_PYTHON", str(python))
    from novelvideo.api.routes import remix as routes

    monkeypatch.setattr(
        routes.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 1, "", "cannot import whisperx"
        ),
    )

    response = _remix_client().get(
        "/api/v1/projects/local/freezone/remix/speech-readiness"
    )

    assert response.json()["data"]["ready"] is False
    assert "cannot import WhisperX" in response.json()["data"]["error"]


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
def test_p0_command_does_not_trust_global_approved_label_without_fixture_provenance(
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
            "approved-real",
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
    assert report["fixture_kind"] == "approved-real"
    assert report["host"]["platform"]
    assert report["speech_runtime"]["concurrency_limit"] == 1
    assert report["speech_runtime"]["asr_weight"] == {
        "version": "synthetic-none",
        "license": "not-applicable",
    }
    assert report["cases"][0]["elapsed_s"] >= 0
    assert report["cases"][0]["process_cumulative_high_water_mb"] > 0
    assert (
        report["cases"][0]["input_probes"]["silent.mp4"]["streams"][0]["codec_type"]
        == "video"
    )
    assert any(
        "fixture provenance" in blocker for blocker in report["p0_gate"]["blockers"]
    )
    assert any(
        "all six categories" in blocker for blocker in report["p0_gate"]["blockers"]
    )
    assert any(
        "ASR weight version" in blocker for blocker in report["p0_gate"]["blockers"]
    )


def test_fixture_evidence_requires_six_categories_hashes_rates_and_manual_provenance(
    tmp_path: Path,
) -> None:
    harness = _harness_module()
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"voice")
    annotation = tmp_path / "annotations.json"
    annotation.write_text(json.dumps({"boundaries": []}), encoding="utf-8")
    case = {
        "case_id": "zh-clear",
        "language": "zh",
        "category": "clear_speech",
        "required_assets": ["voice.wav", "annotations.json"],
        "voices": [
            {
                "id": "fast",
                "speech_rate": "fast",
                "audio": "voice.wav",
                "annotations": "annotations.json",
            }
        ],
        "provenance": {
            "kind": "approved-real",
            "source_id": "controlled-1",
            "license": "test-license",
            "approved_by": "reviewer",
            "approved_at": "2026-09-21",
            "approval_id": "approval-1",
        },
        "asset_inventory": {
            "voice.wav": {
                "sha256": "0" * 64,
                "source_id": "controlled-1",
                "license": "test-license",
                "approved_by": "reviewer",
                "retrieved_at": "2026-09-21",
            },
            "annotations.json": {
                "sha256": hashlib.sha256(annotation.read_bytes()).hexdigest(),
                "source_id": "controlled-1",
                "license": "test-license",
                "approved_by": "reviewer",
                "retrieved_at": "2026-09-21",
            },
        },
    }

    blockers = harness._fixture_evidence_blockers(
        {"schema_version": 1, "cases": [case]}, tmp_path
    )

    assert any("all six categories" in blocker for blocker in blockers)
    assert any("two speech rates" in blocker for blocker in blockers)
    assert any("SHA-256 mismatch" in blocker for blocker in blockers)
    assert any("manual annotation provenance" in blocker for blocker in blockers)


def test_fixture_evidence_derives_every_consumed_path_when_required_assets_is_empty(
    tmp_path: Path,
) -> None:
    harness = _harness_module()
    case = {
        "case_id": "structural-paths",
        "language": "en",
        "category": "vfr_crop_speed",
        "required_assets": [],
        "script_path": "case/script.txt",
        "voices": [
            {
                "id": "normal",
                "audio": "case/voice.wav",
                "annotations": "case/annotations.json",
            }
        ],
        "render": {
            "background": "case/background.png",
            "broll": "case/broll.mp4",
        },
        "video_transform": {"source": "case/source-vfr.mp4"},
        "provenance": {
            "kind": "approved-real",
            "source_id": "controlled-structural-paths",
            "license": "LicenseRef-Controlled-Test",
            "approved_by": "asset-reviewer",
            "approved_at": "2026-09-21",
            "approval_id": "approval-structural-paths",
        },
        "asset_inventory": {},
    }

    blockers = harness._fixture_evidence_blockers({"cases": [case]}, tmp_path)

    for path in (
        "case/script.txt",
        "case/voice.wav",
        "case/annotations.json",
        "case/background.png",
        "case/broll.mp4",
        "case/source-vfr.mp4",
    ):
        assert (
            f"structural-paths inventory provenance is missing for {path}" in blockers
        )


def test_fixture_evidence_accepts_verified_six_category_inventory(
    tmp_path: Path,
) -> None:
    harness = _harness_module()

    def case_provenance(case_id: str) -> dict:
        return {
            "kind": "approved-real",
            "source_id": f"source-{case_id}",
            "license": "LicenseRef-Controlled-Test",
            "approved_by": "asset-reviewer",
            "approved_at": "2026-09-21",
            "approval_id": f"approval-{case_id}",
        }

    def inventory(paths: list[str], case_id: str) -> dict:
        return {
            path: {
                "sha256": hashlib.sha256((tmp_path / path).read_bytes()).hexdigest(),
                "source_id": f"source-{case_id}",
                "license": "LicenseRef-Controlled-Test",
                "approved_by": "asset-reviewer",
                "retrieved_at": "2026-09-21",
            }
            for path in paths
        }

    def annotated_voice(
        case_id: str, voice_id: str, rate: str
    ) -> tuple[dict, list[str]]:
        audio_path = f"{case_id}/{voice_id}.wav"
        annotation_path = f"{case_id}/{voice_id}.json"
        audio = tmp_path / audio_path
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(f"{case_id}-{voice_id}-{rate}".encode())
        audio_hash = hashlib.sha256(audio.read_bytes()).hexdigest()
        (tmp_path / annotation_path).write_text(
            json.dumps(
                {
                    "boundaries": [],
                    "provenance": {
                        "method": "manual",
                        "annotator_ids": ["annotator-1"],
                        "reviewed_by": "annotation-reviewer",
                        "annotated_at": "2026-09-21",
                        "source_audio_sha256": audio_hash,
                    },
                }
            ),
            encoding="utf-8",
        )
        return {
            "id": voice_id,
            "speech_rate": rate,
            "audio": audio_path,
            "annotations": annotation_path,
        }, [audio_path, annotation_path]

    cases = []
    for language in ("zh", "en"):
        case_id = f"{language}-clear"
        fast, fast_paths = annotated_voice(case_id, "fast", "fast")
        slow, slow_paths = annotated_voice(case_id, "slow", "slow")
        paths = fast_paths + slow_paths
        cases.append(
            {
                "case_id": case_id,
                "language": language,
                "category": "clear_speech",
                "required_assets": paths,
                "voices": [fast, slow],
                "provenance": case_provenance(case_id),
                "asset_inventory": inventory(paths, case_id),
            }
        )
    repeated, repeated_paths = annotated_voice(
        "repeated-numbers-emoji", "edge", "normal"
    )
    vfr, vfr_paths = annotated_voice("vfr-crop-speed", "normal", "normal")
    for case_id, category, voice, paths in (
        (
            "repeated-numbers-emoji",
            "repeated_words_numbers_emoji",
            repeated,
            repeated_paths,
        ),
        ("vfr-crop-speed", "vfr_crop_speed", vfr, vfr_paths),
    ):
        cases.append(
            {
                "case_id": case_id,
                "language": "en",
                "category": category,
                "required_assets": paths,
                "voices": [voice],
                "provenance": case_provenance(case_id),
                "asset_inventory": inventory(paths, case_id),
            }
        )
    for case_id, category in (
        ("no-speech", "no_speech"),
        ("script-mismatch", "script_mismatch"),
    ):
        path = f"{case_id}/fixture.bin"
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(case_id.encode())
        cases.append(
            {
                "case_id": case_id,
                "language": "en",
                "category": category,
                "required_assets": [path],
                "provenance": case_provenance(case_id),
                "asset_inventory": inventory([path], case_id),
            }
        )

    assert harness._fixture_evidence_blockers({"cases": cases}, tmp_path) == []
    assert (
        harness._runtime_metadata_blockers(
            {
                "whisperx_version": "3.8.6",
                "whisperx_license": "BSD-2-Clause",
                "model": "small",
                "asr_weight": {"version": "repo@abc123", "license": "MIT"},
                "alignment_weight": {
                    "version": "repo@def456",
                    "license": "Apache-2.0",
                },
                "platform_support": "macOS arm64 CPU measured",
            }
        )
        == []
    )


class _ExactRunner:
    engine_version = "exact-test-runner"
    device = "test"

    def __init__(self) -> None:
        self.calls: list[Path] = []

    def align(self, audio: Path, *, language: str, script: str) -> list[dict]:
        self.calls.append(audio)
        timeline = []
        for index, character in enumerate(script):
            if character.isspace():
                continue
            timeline.append(
                {
                    "start_char": index,
                    "end_char": index + 1,
                    "start_us": index * 100_000,
                    "end_us": (index + 1) * 100_000,
                }
            )
        return timeline


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required"
)
def test_p0_executes_repeated_number_emoji_alignment_case(tmp_path: Path) -> None:
    harness = _harness_module()
    script = "go 2️⃣ go"
    (tmp_path / "script.txt").write_text(script, encoding="utf-8")
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=1",
        str(tmp_path / "voice.wav"),
    )
    annotations = [
        {
            "start_char": 7,
            "end_char": 8,
            "start_us": 700_000,
            "end_us": 800_000,
        }
    ]
    (tmp_path / "annotations.json").write_text(
        json.dumps({"boundaries": annotations}), encoding="utf-8"
    )
    case = {
        "case_id": "repeated-numbers-emoji",
        "language": "en",
        "category": "repeated_words_numbers_emoji",
        "required_assets": ["script.txt", "voice.wav", "annotations.json"],
        "script_path": "script.txt",
        "voices": [
            {
                "id": "edge",
                "speech_rate": "normal",
                "audio": "voice.wav",
                "annotations": "annotations.json",
            }
        ],
        "required_event_selection": {"id": "second-go", "start_char": 7, "end_char": 8},
        "required_script_features": {
            "repeated_text": "go",
            "number": "2",
            "emoji": "️⃣",
        },
        "fps": 30,
    }
    runner = _ExactRunner()

    report, errors = harness._case_report(case, tmp_path, tmp_path / "out", runner)

    assert report["status"] == "validated"
    assert report["voice_results"][0]["event_frames"] == {
        "start_frame": 21,
        "end_frame": 24,
    }
    assert errors == [0.0, 0.0]
    assert runner.calls == [tmp_path / "voice.wav"]


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required"
)
def test_p0_executes_vfr_crop_and_speed_case(tmp_path: Path) -> None:
    harness = _harness_module()
    source = tmp_path / "source-vfr.mp4"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=s=160x120:r=30:d=1",
        "-vf",
        "setpts=PTS+if(gte(N\\,15)\\,0.2/TB\\,0)",
        "-fps_mode",
        "vfr",
        str(source),
    )
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=1",
        str(tmp_path / "voice.wav"),
    )
    (tmp_path / "script.txt").write_text("go", encoding="utf-8")
    (tmp_path / "annotations.json").write_text(
        json.dumps(
            {
                "boundaries": [
                    {
                        "start_char": 0,
                        "end_char": 1,
                        "start_us": 0,
                        "end_us": 100_000,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    case = {
        "case_id": "vfr-crop-speed",
        "language": "en",
        "category": "vfr_crop_speed",
        "required_assets": [
            "source-vfr.mp4",
            "voice.wav",
            "script.txt",
            "annotations.json",
        ],
        "script_path": "script.txt",
        "voices": [
            {
                "id": "normal",
                "speech_rate": "normal",
                "audio": "voice.wav",
                "annotations": "annotations.json",
            }
        ],
        "required_event_selection": {"id": "go", "start_char": 0, "end_char": 1},
        "fps": 30,
        "video_transform": {
            "source": "source-vfr.mp4",
            "crop": {"x": 10, "y": 10, "width": 100, "height": 80},
            "speed": 2.0,
        },
    }

    report, _errors = harness._case_report(
        case, tmp_path, tmp_path / "out", _ExactRunner()
    )

    assert report["status"] == "validated"
    assert report["vfr_transform"]["source_variable_frame_rate"] is True
    output_stream = report["vfr_transform"]["output_probe"]["streams"][0]
    assert (output_stream["width"], output_stream["height"]) == (100, 80)
    assert report["vfr_transform"]["speed"] == 2.0


def test_p0_executes_expected_no_speech_and_script_mismatch_cases(
    tmp_path: Path,
) -> None:
    harness = _harness_module()
    for name in ("silent.wav", "omission.wav", "insertion.wav"):
        (tmp_path / name).write_bytes(b"fixture")
    (tmp_path / "script.txt").write_text("required script", encoding="utf-8")

    class ExpectedFailureRunner:
        engine_version = "failure-test-runner"
        device = "test"

        def __init__(self) -> None:
            self.calls: list[str] = []

        def align(self, audio: Path, *, language: str, script: str) -> list[dict]:
            self.calls.append(audio.name)
            if audio.name == "silent.wav":
                raise ValueError("ASR produced no speech segments")
            raise ValueError(
                "ASR transcript does not match the controlled fixture script"
            )

    runner = ExpectedFailureRunner()
    no_speech = {
        "case_id": "no-speech",
        "language": "en",
        "category": "no_speech",
        "required_assets": ["silent.wav", "script.txt"],
        "script_path": "script.txt",
        "voices": [{"id": "silent", "audio": "silent.wav", "expected": "no_speech"}],
    }
    mismatch = {
        "case_id": "script-mismatch",
        "language": "en",
        "category": "script_mismatch",
        "required_assets": ["omission.wav", "insertion.wav", "script.txt"],
        "script_path": "script.txt",
        "voices": [
            {"id": "omission", "audio": "omission.wav", "expected": "script_mismatch"},
            {
                "id": "insertion",
                "audio": "insertion.wav",
                "expected": "script_mismatch",
            },
        ],
    }

    no_speech_report, _ = harness._case_report(
        no_speech, tmp_path, tmp_path / "out", runner
    )
    mismatch_report, _ = harness._case_report(
        mismatch, tmp_path, tmp_path / "out", runner
    )

    assert no_speech_report["status"] == "expected_no_speech"
    assert mismatch_report["status"] == "expected_unresolved"
    assert runner.calls == ["silent.wav", "omission.wav", "insertion.wav"]


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
