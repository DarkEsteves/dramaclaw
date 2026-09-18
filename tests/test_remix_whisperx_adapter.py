"""An explicitly configured local WhisperX engine feeds the remix timeline."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from novelvideo.freezone.remix import alignment


class FakeWhisperX:
    __version__ = "test-version"

    def __init__(self, *, transcript: str = "go go", chars: list[dict] | None = None):
        self.transcript = transcript
        self.chars = chars
        self.calls: list[tuple[str, dict]] = []

    def load_model(self, name, device, **kwargs):
        self.calls.append(("load_model", {"name": name, "device": device, **kwargs}))
        return SimpleNamespace(transcribe=lambda audio, **options: {
            "segments": [{"text": self.transcript, "start": 0.0, "end": 1.0}]
        })

    def load_audio(self, path):
        self.calls.append(("load_audio", {"path": path}))
        return "audio"

    def load_align_model(self, **kwargs):
        self.calls.append(("load_align_model", kwargs))
        return "model", "metadata"

    def align(self, segments, model, metadata, audio, device, **kwargs):
        self.calls.append(("align", {"segments": segments, **kwargs}))
        if self.chars is None:
            chars = [
                {"char": "g", "start": 0.0, "end": 0.1},
                {"char": "o", "start": 0.1, "end": 0.2},
                {"char": " ", "start": 0.2, "end": 0.3},
                {"char": "g", "start": 0.5, "end": 0.6},
                {"char": "o", "start": 0.6, "end": 0.7},
            ]
        else:
            chars = self.chars
        return {"segments": [{"chars": chars}]}


def test_unconfigured_engine_stays_unavailable(monkeypatch):
    monkeypatch.delenv("DRAMACLAW_REMIX_SPEECH_ENGINE", raising=False)
    assert not alignment.speech_engine_ready()
    with pytest.raises(RuntimeError, match="no speech aligner"):
        alignment.configured_speech_aligner()


def test_configured_adapter_aligns_repeated_words_with_exact_spans(tmp_path: Path, monkeypatch):
    media = tmp_path / "voice.wav"
    media.write_bytes(b"audio")
    cache = tmp_path / "models"
    cache.mkdir()
    fake = FakeWhisperX()
    monkeypatch.setitem(sys.modules, "whisperx", fake)
    monkeypatch.setenv("DRAMACLAW_REMIX_SPEECH_ENGINE", "whisperx")
    monkeypatch.setenv("DRAMACLAW_REMIX_MODEL_CACHE", str(cache))

    result = alignment.align_script(media, {"revision": 2, "language": "en", "text": "go go"})

    assert result["status"] == "completed"
    assert [(w["text"], w["start_char"], w["end_char"]) for w in result["words"]] == [
        ("g", 0, 1), ("o", 1, 2), ("g", 3, 4), ("o", 4, 5)
    ]
    assert fake.calls[0][1]["local_files_only"] is True
    assert fake.calls[0][1]["download_root"] == str(cache)
    assert next(options for name, options in fake.calls if name == "load_align_model")["model_cache_only"] is True


def test_configured_adapter_returns_no_speech(tmp_path: Path, monkeypatch):
    media = tmp_path / "silent.wav"
    media.write_bytes(b"audio")
    cache = tmp_path / "models"
    cache.mkdir()
    fake = FakeWhisperX(transcript="")
    monkeypatch.setitem(sys.modules, "whisperx", fake)
    monkeypatch.setenv("DRAMACLAW_REMIX_SPEECH_ENGINE", "whisperx")
    monkeypatch.setenv("DRAMACLAW_REMIX_MODEL_CACHE", str(cache))

    assert alignment.transcribe_media(media, "en")["status"] == "no_speech"
    assert not any(name == "load_align_model" for name, _ in fake.calls)


def test_configured_adapter_transcribes_to_timed_characters(tmp_path: Path, monkeypatch):
    media = tmp_path / "voice.wav"
    media.write_bytes(b"audio")
    cache = tmp_path / "models"
    cache.mkdir()
    monkeypatch.setitem(sys.modules, "whisperx", FakeWhisperX())
    monkeypatch.setenv("DRAMACLAW_REMIX_SPEECH_ENGINE", "whisperx")
    monkeypatch.setenv("DRAMACLAW_REMIX_MODEL_CACHE", str(cache))

    result = alignment.transcribe_media(media, "en")

    assert result["status"] == "completed"
    assert [(word["text"], word["start_us"], word["end_us"]) for word in result["words"]] == [
        ("g", 0, 100_000), ("o", 100_000, 200_000),
        ("g", 500_000, 600_000), ("o", 600_000, 700_000),
    ]


def test_configured_adapter_reports_script_mismatch_before_forced_alignment(tmp_path: Path, monkeypatch):
    media = tmp_path / "voice.wav"
    media.write_bytes(b"audio")
    cache = tmp_path / "models"
    cache.mkdir()
    fake = FakeWhisperX(transcript="different")
    monkeypatch.setitem(sys.modules, "whisperx", fake)
    monkeypatch.setenv("DRAMACLAW_REMIX_SPEECH_ENGINE", "whisperx")
    monkeypatch.setenv("DRAMACLAW_REMIX_MODEL_CACHE", str(cache))

    result = alignment.align_script(media, {"revision": 1, "language": "en", "text": "go go"})

    assert result["status"] == "script_mismatch"
    assert not any(name == "load_align_model" for name, _ in fake.calls)


def test_spoken_text_with_no_timed_characters_is_not_reported_as_silence(tmp_path: Path, monkeypatch):
    media = tmp_path / "voice.wav"
    media.write_bytes(b"audio")
    cache = tmp_path / "models"
    cache.mkdir()
    fake = FakeWhisperX(chars=[
        {"char": "g"}, {"char": "o"}, {"char": " "}, {"char": "g"}, {"char": "o"},
    ])
    monkeypatch.setitem(sys.modules, "whisperx", fake)
    monkeypatch.setenv("DRAMACLAW_REMIX_SPEECH_ENGINE", "whisperx")
    monkeypatch.setenv("DRAMACLAW_REMIX_MODEL_CACHE", str(cache))

    result = alignment.align_script(media, {"revision": 1, "language": "en", "text": "go go"})

    assert result["status"] == "script_mismatch"


def test_engine_requires_existing_local_cache(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DRAMACLAW_REMIX_SPEECH_ENGINE", "whisperx")
    monkeypatch.setenv("DRAMACLAW_REMIX_MODEL_CACHE", str(tmp_path / "missing"))
    assert not alignment.speech_engine_ready()
    with pytest.raises(RuntimeError, match="model cache"):
        alignment.configured_speech_aligner()
