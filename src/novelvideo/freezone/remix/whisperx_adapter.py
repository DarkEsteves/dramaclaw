"""Offline WhisperX implementation of the normalized speech adapter boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any


class WhisperXSpeechAligner:
    """Run local ASR then character alignment with already cached model weights."""

    def __init__(
        self, *, model_cache: Path, model_name: str, device: str,
        compute_type: str, batch_size: int,
    ) -> None:
        if batch_size < 1:
            raise ValueError("WhisperX batch size must be positive")
        self.model_cache = model_cache
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.batch_size = batch_size

    def transcribe(self, media_path: Path, language: str) -> dict[str, Any]:
        whisperx, audio, segments = self._recognize(media_path, language)
        text = "".join(str(row.get("text", "")) for row in segments)
        if not text.strip():
            return {"status": "no_speech", "words": []}
        return self._force_align(whisperx, audio, text, language, segments)

    def align(self, media_path: Path, script: str, language: str) -> dict[str, Any]:
        whisperx, audio, segments = self._recognize(media_path, language)
        transcript = "".join(str(row.get("text", "")) for row in segments)
        if not transcript.strip():
            return {"status": "no_speech", "words": []}
        if _script_check(transcript) != _script_check(script):
            return {
                "status": "script_mismatch", "words": [],
                "mismatch": {"reason": "ASR transcript does not match the script"},
            }
        return self._force_align(whisperx, audio, script, language, segments)

    def _recognize(self, media_path: Path, language: str):
        try:
            import whisperx
        except ImportError as error:
            raise RuntimeError("WhisperX is not installed") from error
        model = whisperx.load_model(
            self.model_name, self.device, compute_type=self.compute_type,
            language=language, download_root=str(self.model_cache),
            local_files_only=True,
        )
        audio = whisperx.load_audio(str(media_path))
        result = model.transcribe(audio, batch_size=self.batch_size, language=language)
        segments = result.get("segments", [])
        if not isinstance(segments, list):
            raise ValueError("WhisperX returned invalid transcription segments")
        return whisperx, audio, segments

    def _force_align(self, whisperx, audio, text: str, language: str, segments: list[dict[str, Any]]) -> dict[str, Any]:
        duration = max((float(row["end"]) for row in segments), default=0.0)
        if duration <= 0:
            return {"status": "no_speech", "words": []}
        model, metadata = whisperx.load_align_model(
            language_code=language, device=self.device,
            model_dir=str(self.model_cache), model_cache_only=True,
        )
        result = whisperx.align(
            [{"text": text, "start": 0.0, "end": duration}],
            model, metadata, audio, self.device, return_char_alignments=True,
        )
        chars = [char for segment in result.get("segments", []) for char in segment.get("chars", [])]
        if "".join(str(char.get("char", "")) for char in chars) != text:
            return {
                "status": "script_mismatch", "words": [],
                "mismatch": {"reason": "forced alignment did not preserve the script"},
            }
        words = []
        for index, char in enumerate(chars):
            letter = char["char"]
            if letter.isspace() or "start" not in char or "end" not in char:
                continue
            start, end = float(char["start"]), float(char["end"])
            if end <= start:
                continue
            words.append({
                "text": letter, "start_char": index, "end_char": index + 1,
                "start": start, "end": end,
            })
        if not words:
            return {
                "status": "script_mismatch", "words": [],
                "mismatch": {"reason": "WhisperX found speech but produced no timed characters"},
            }
        return {"status": "completed", "words": words}


def _script_check(text: str) -> str:
    return "".join(character for character in text.casefold() if not character.isspace())
