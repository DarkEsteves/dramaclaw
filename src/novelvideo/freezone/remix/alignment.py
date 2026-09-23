"""Adapter boundary and normalization for speech transcription and alignment."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import threading
import json
import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol


_SPEECH_ENGINE_SLOT = threading.BoundedSemaphore(value=1)


class SpeechAligner(Protocol):
    """A local or service-backed engine whose raw result is normalized here."""

    def transcribe(self, media_path: Path, language: str) -> dict[str, Any]: ...

    def align(self, media_path: Path, script: str, language: str) -> dict[str, Any]: ...


def transcribe_media(
    media_path: Path, language: str, *, aligner: SpeechAligner | None = None
) -> dict[str, Any]:
    """Transcribe local media into a normalized ``word_timeline.v1`` payload.

    ``aligner`` is deliberately injected. This module does not import WhisperX,
    download model weights, or select a hosted provider while handling a request.
    The adapter returns ``status`` (``completed`` or ``no_speech``) and raw
    ``words`` containing ``text`` plus either microsecond times or seconds.
    Unicode code-point spans are derived in stream order.
    """
    _validate_media_and_language(media_path, language)
    with _SPEECH_ENGINE_SLOT:
        raw = _require_aligner(aligner).transcribe(media_path, language)
    return _normalize_result(
        raw,
        media_path=media_path,
        language=language,
        script_revision=None,
        script_text=None,
    )


def align_script(
    media_path: Path,
    script: dict[str, Any],
    *,
    aligner: SpeechAligner | None = None,
) -> dict[str, Any]:
    """Force-align a versioned script and return its normalized word timeline.

    ``script`` requires ``revision`` and either ``dialogue`` or ``text``; its
    optional ``language`` is inferred from the script when absent. Engine
    character spans are checked against the exact script slice. Engines without
    spans are matched only in forward sequence after whitespace, which prevents
    a repeated word from being rebound to the first occurrence. Mismatches are
    returned as a ``script_mismatch`` status rather than fabricated timings.
    """
    revision = script.get("revision")
    if not isinstance(revision, int) or revision < 1:
        raise ValueError("script revision must be a positive integer")
    text = script.get("dialogue", script.get("text"))
    if not isinstance(text, str):
        raise ValueError("script dialogue or text is required")
    language = _script_language(script.get("language"), text)
    _validate_media_and_language(media_path, language)
    spoken, original_positions = _spoken_script_positions(text)
    with _SPEECH_ENGINE_SLOT:
        raw = _require_aligner(aligner).align(media_path, spoken, language)
    result = _normalize_result(
        raw,
        media_path=media_path,
        language=language,
        script_revision=revision,
        script_text=spoken,
    )
    if result["status"] == "completed" and original_positions is not None:
        for word in result["words"]:
            start, end = word["start_char"], word["end_char"]
            word["start_char"] = original_positions[start]
            word["end_char"] = original_positions[end - 1] + 1
        result["transcript"] = text
    if original_positions is not None:
        _map_match_report_to_original_script(result, original_positions)
    return result


_HAN_SCRIPT = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN_SCRIPT = re.compile(r"[A-Za-z]")


def _script_language(value: object, text: str) -> str:
    if isinstance(value, str) and value:
        return value
    if _HAN_SCRIPT.search(text):
        return "zh"
    if _LATIN_SCRIPT.search(text):
        return "en"
    return "auto"


def _map_match_report_to_original_script(
    result: dict[str, Any], original_positions: list[int]
) -> None:
    report = result.get("match_report")
    if not isinstance(report, dict):
        mismatch = result.get("mismatch")
        report = mismatch.get("report") if isinstance(mismatch, dict) else None
    if isinstance(report, dict):
        for key in ("substitutions", "insertions", "omissions"):
            differences = report.get(key)
            if not isinstance(differences, list):
                continue
            for difference in differences:
                span = difference.get("script_range") if isinstance(difference, dict) else None
                if not isinstance(span, dict):
                    continue
                _map_script_span(span, original_positions)
    mismatch = result.get("mismatch")
    untimed = mismatch.get("untimed_script_ranges") if isinstance(mismatch, dict) else None
    if isinstance(untimed, list):
        for span in untimed:
            if isinstance(span, dict):
                _map_script_span(span, original_positions)


def _map_script_span(span: dict[str, Any], original_positions: list[int]) -> None:
    start, end = span.get("start_char"), span.get("end_char")
    if not isinstance(start, int) or not isinstance(end, int):
        return
    if start < end and end <= len(original_positions):
        span["start_char"] = original_positions[start]
        span["end_char"] = original_positions[end - 1] + 1
    elif start == end:
        span["start_char"] = span["end_char"] = (
            original_positions[start]
            if start < len(original_positions)
            else original_positions[-1] + 1
        )


_SPEAKER_PREFIX = re.compile(r"^\s*[^：:\n]{1,24}[：:]\s*")


def _spoken_script_positions(text: str) -> tuple[str, list[int] | None]:
    """Remove role prefixes for alignment while retaining original code-point offsets."""
    lines = text.splitlines(keepends=True)
    nonblank = [line for line in lines if line.strip()]
    if len(nonblank) < 2 or not all(_SPEAKER_PREFIX.match(line) for line in nonblank):
        return text, None
    spoken_chars: list[str] = []
    positions: list[int] = []
    source_offset = 0
    for line in lines:
        content = line.rstrip("\r\n")
        prefix = _SPEAKER_PREFIX.match(content)
        start = prefix.end() if prefix else 0
        for index in range(start, len(content)):
            spoken_chars.append(content[index])
            positions.append(source_offset + index)
        if line.endswith("\n"):
            spoken_chars.append("\n")
            positions.append(source_offset + len(line) - 1)
        source_offset += len(line)
    return "".join(spoken_chars), positions


def _require_aligner(aligner: SpeechAligner | None) -> SpeechAligner:
    if aligner is None:
        return configured_speech_aligner()
    return aligner


def speech_engine_ready() -> bool:
    """Report whether an explicitly enabled local engine can accept jobs."""
    if os.getenv("DRAMACLAW_REMIX_SPEECH_ENGINE") != "whisperx":
        return False
    cache = os.getenv("DRAMACLAW_REMIX_MODEL_CACHE")
    python = os.getenv("DRAMACLAW_REMIX_WHISPERX_PYTHON")
    available = Path(python).is_file() and os.access(python, os.X_OK) if python else importlib.util.find_spec("whisperx")
    return bool(cache and Path(cache).is_dir() and available)


def configured_speech_aligner() -> SpeechAligner:
    """Build the opt-in adapter without downloading models during API startup."""
    if os.getenv("DRAMACLAW_REMIX_SPEECH_ENGINE") != "whisperx":
        raise RuntimeError("no speech aligner is configured")
    cache = os.getenv("DRAMACLAW_REMIX_MODEL_CACHE")
    if not cache or not Path(cache).is_dir():
        raise RuntimeError("WhisperX model cache must be an existing local directory")
    python = os.getenv("DRAMACLAW_REMIX_WHISPERX_PYTHON")
    if python:
        if not Path(python).is_file() or not os.access(python, os.X_OK):
            raise RuntimeError("WhisperX Python executable is unavailable")
        return _ExternalWhisperXAligner(Path(python), Path(cache))
    from novelvideo.freezone.remix.whisperx_adapter import WhisperXSpeechAligner

    return WhisperXSpeechAligner(
        model_cache=Path(cache),
        model_name=os.getenv("DRAMACLAW_REMIX_WHISPER_MODEL", "small"),
        device=os.getenv("DRAMACLAW_REMIX_DEVICE", "cpu"),
        compute_type=os.getenv("DRAMACLAW_REMIX_COMPUTE_TYPE", "int8"),
        batch_size=int(os.getenv("DRAMACLAW_REMIX_BATCH_SIZE", "4")),
    )


class _ExternalWhisperXAligner:
    """Run WhisperX in its own Python environment to preserve project dependencies."""

    def __init__(self, python: Path, model_cache: Path) -> None:
        self.python = python
        self.model_cache = model_cache

    def transcribe(self, media_path: Path, language: str) -> dict[str, Any]:
        return self._run("transcribe", media_path, language)

    def align(self, media_path: Path, script: str, language: str) -> dict[str, Any]:
        return self._run("align", media_path, language, script)

    def _run(self, action: str, media_path: Path, language: str, script: str | None = None) -> dict[str, Any]:
        request = {
            "action": action,
            "media_path": str(media_path),
            "language": language,
            "script": script,
            "model_cache": str(self.model_cache),
            "model_name": os.getenv("DRAMACLAW_REMIX_WHISPER_MODEL", "small"),
            "device": os.getenv("DRAMACLAW_REMIX_DEVICE", "cpu"),
            "compute_type": os.getenv("DRAMACLAW_REMIX_COMPUTE_TYPE", "int8"),
            "batch_size": int(os.getenv("DRAMACLAW_REMIX_BATCH_SIZE", "4")),
        }
        worker = Path(__file__).with_name("whisperx_worker.py")
        completed = subprocess.run(
            [str(self.python), str(worker)], input=json.dumps(request),
            text=True, capture_output=True, timeout=1800, check=False,
        )
        if completed.returncode:
            raise RuntimeError(f"WhisperX worker failed: {completed.stderr[-2000:]}")
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("WhisperX worker returned invalid JSON") from error
        if not isinstance(result, dict):
            raise RuntimeError("WhisperX worker returned invalid result")
        return result


def _validate_media_and_language(media_path: Path, language: object) -> None:
    if not media_path.is_file():
        raise FileNotFoundError(media_path)
    if not isinstance(language, str) or not language:
        raise ValueError("language is required")


def _normalize_result(
    raw: dict[str, Any],
    *,
    media_path: Path,
    language: str,
    script_revision: int | None,
    script_text: str | None,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("speech aligner returned an invalid result")
    status = raw.get("status")
    detected_language = raw.get("language") if language == "auto" else language
    if not isinstance(detected_language, str) or not detected_language:
        detected_language = language
    common = {
        "schema": "word_timeline.v1",
        "media_hash": _sha256(media_path),
        "language": detected_language,
    }
    if script_revision is not None:
        common["script_revision"] = script_revision
    if status == "no_speech":
        return {**common, "status": "no_speech", "transcript": "", "words": [], "confidence_available": False}
    if status == "script_mismatch":
        return {
            **common,
            "status": "script_mismatch",
            "transcript": "",
            "words": [],
            "confidence_available": False,
            "mismatch": raw.get("mismatch", {"reason": "speech aligner reported a script mismatch"}),
        }
    if status != "completed":
        raise ValueError("speech aligner status must be completed, no_speech, or script_mismatch")

    try:
        words = _normalize_words(raw.get("words"), script_text)
    except _ScriptMismatch as error:
        return {
            **common,
            "status": "script_mismatch",
            "transcript": "",
            "words": [],
            "confidence_available": False,
            "mismatch": {"reason": str(error)},
        }
    transcript = script_text if script_text is not None else raw.get("transcript")
    if not isinstance(transcript, str):
        transcript = "".join(word["text"] for word in words)
    normalized = {
        **common,
        "status": "completed",
        "transcript": transcript,
        "words": words,
        "confidence_available": any(word["confidence"] is not None for word in words),
    }
    if isinstance(raw.get("match_report"), dict):
        normalized["match_report"] = deepcopy(raw["match_report"])
    return normalized


def _normalize_words(raw_words: object, script_text: str | None) -> list[dict[str, Any]]:
    if not isinstance(raw_words, list):
        raise ValueError("speech aligner words must be a list")
    words: list[dict[str, Any]] = []
    cursor = 0
    for raw_word in raw_words:
        if not isinstance(raw_word, dict):
            raise ValueError("speech aligner word must be an object")
        text = raw_word.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError("speech aligner word text is required")
        start_char, end_char, cursor = _word_span(raw_word, text, script_text, cursor)
        start_us, end_us = _word_times(raw_word)
        confidence = raw_word.get("confidence")
        if confidence is not None and not isinstance(confidence, (int, float)):
            raise ValueError("speech aligner word confidence must be numeric or absent")
        words.append(
            {
                "text": text,
                "start_char": start_char,
                "end_char": end_char,
                "start_us": start_us,
                "end_us": end_us,
                "confidence": confidence,
                "confidence_status": "available" if confidence is not None else "unavailable",
            }
        )
    return words


def _word_span(
    raw_word: dict[str, Any], text: str, script_text: str | None, cursor: int
) -> tuple[int, int, int]:
    start = raw_word.get("start_char")
    end = raw_word.get("end_char")
    if start is not None or end is not None:
        if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end <= start:
            raise ValueError("speech aligner word character span is invalid")
        if script_text is not None and (end > len(script_text) or script_text[start:end] != text):
            raise _ScriptMismatch(f"aligned token does not match script at character {start}")
        return start, end, max(cursor, end)
    if script_text is None:
        return cursor, cursor + len(text), cursor + len(text)
    while cursor < len(script_text) and script_text[cursor].isspace():
        cursor += 1
    if not script_text.startswith(text, cursor):
        raise _ScriptMismatch(f"aligned token does not match script at character {cursor}")
    return cursor, cursor + len(text), cursor + len(text)


def _word_times(raw_word: dict[str, Any]) -> tuple[int, int]:
    if "start_us" in raw_word or "end_us" in raw_word:
        start, end = raw_word.get("start_us"), raw_word.get("end_us")
        if not isinstance(start, int) or not isinstance(end, int):
            raise ValueError("speech aligner microsecond times must be integers")
    else:
        start, end = _seconds_to_us(raw_word.get("start")), _seconds_to_us(raw_word.get("end"))
    if start < 0 or end <= start:
        raise ValueError("speech aligner word times are invalid")
    return start, end


def _seconds_to_us(value: object) -> int:
    try:
        seconds = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("speech aligner word seconds are invalid") from error
    if not seconds.is_finite():
        raise ValueError("speech aligner word seconds are invalid")
    return int(seconds * 1_000_000)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1_048_576), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


class _ScriptMismatch(ValueError):
    """Internal mismatch signal converted to a result status at the public boundary."""
