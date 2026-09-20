"""Offline WhisperX implementation of the normalized speech adapter boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any


class WhisperXSpeechAligner:
    """Run local ASR with word timing, or character timing for a supplied script."""

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
        return self._align_words(whisperx, audio, text, language, segments)

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
        return self._force_align_characters(whisperx, audio, script, language, segments)

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

    def _alignment_model(self, whisperx, language: str):
        return whisperx.load_align_model(
            language_code=language, device=self.device,
            model_dir=str(self.model_cache), model_cache_only=True,
        )

    def _align_words(self, whisperx, audio, text: str, language: str, segments: list[dict[str, Any]]) -> dict[str, Any]:
        model, metadata = self._alignment_model(whisperx, language)
        result = whisperx.align(segments, model, metadata, audio, self.device)
        words: list[dict[str, Any]] = []
        cursor = 0
        for segment in result.get("segments", []):
            for raw_word in segment.get("words", []):
                value = raw_word.get("word", raw_word.get("text", ""))
                token = value.strip() if isinstance(value, str) else ""
                if not token or "start" not in raw_word or "end" not in raw_word:
                    continue
                start, end = float(raw_word["start"]), float(raw_word["end"])
                if end <= start:
                    continue
                position = text.find(token, cursor)
                if position < 0:
                    return {
                        "status": "script_mismatch", "words": [],
                        "mismatch": {"reason": "aligned word does not match the transcript"},
                    }
                cursor = position + len(token)
                word: dict[str, Any] = {
                    "text": token, "start_char": position, "end_char": cursor,
                    "start": start, "end": end,
                }
                score = raw_word.get("score")
                if isinstance(score, (int, float)) and 0 <= score <= 1:
                    word["confidence"] = float(score)
                words.append(word)
        if not words:
            return {
                "status": "script_mismatch", "words": [],
                "mismatch": {"reason": "WhisperX found speech but produced no timed words"},
            }
        return {"status": "completed", "transcript": text, "words": words}

    def _force_align_characters(self, whisperx, audio, text: str, language: str, segments: list[dict[str, Any]]) -> dict[str, Any]:
        duration = max((float(row["end"]) for row in segments), default=0.0)
        if duration <= 0:
            return {"status": "no_speech", "words": []}
        model, metadata = self._alignment_model(whisperx, language)
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
        return {"status": "completed", "transcript": text, "words": words}


def _script_check(text: str) -> str:
    return "".join(character for character in text.casefold() if not character.isspace())
