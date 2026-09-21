"""Small, deterministic P0 sample: aligned event to portrait FFmpeg render."""

from __future__ import annotations

import math
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Protocol


class AlignmentRunner(Protocol):
    """An injectable ASR plus forced-alignment adapter for the P0 harness."""

    engine_version: str
    device: str

    def align(self, audio: Path, *, language: str, script: str) -> list[dict[str, Any]]:
        """Return character spans with Unicode code-point ranges and microseconds."""


class WhisperXAlignmentRunner:
    """Lazy WhisperX adapter using only locally available packages and weights."""

    def __init__(
        self,
        *,
        device: str,
        model_name: str,
        compute_type: str,
        batch_size: int,
        model_cache: Path | None = None,
    ) -> None:
        self.device = device
        self.model_name = model_name
        self.compute_type = compute_type
        self.batch_size = batch_size
        self.model_cache = model_cache
        self.engine_version = "whisperx-unloaded"

    def align(self, audio: Path, *, language: str, script: str) -> list[dict[str, Any]]:
        """Transcribe, verify the controlled script, then force-align it to audio.

        Imports and model loading happen here rather than when the command starts.
        Both model loading APIs are asked to use local files only, so this harness
        cannot silently obtain weights during an experiment.
        """
        try:
            import whisperx
        except ImportError as error:  # pragma: no cover - exercised on configured host
            raise RuntimeError("WhisperX is not installed") from error

        self.engine_version = getattr(whisperx, "__version__", "whisperx-unknown")
        cache = str(self.model_cache) if self.model_cache else None
        model = whisperx.load_model(
            self.model_name,
            self.device,
            compute_type=self.compute_type,
            language=language,
            download_root=cache,
            local_files_only=True,
        )
        waveform = whisperx.load_audio(str(audio))
        transcription = model.transcribe(
            waveform, batch_size=self.batch_size, language=language
        )
        transcript = "".join(segment["text"] for segment in transcription["segments"])
        if not transcription["segments"] or not transcript.strip():
            raise ValueError("ASR produced no speech segments")
        if _normalize_for_script_check(transcript) != _normalize_for_script_check(
            script
        ):
            raise ValueError(
                "ASR transcript does not match the controlled fixture script"
            )
        duration_s = max(
            (float(segment["end"]) for segment in transcription["segments"]),
            default=0.0,
        )
        if duration_s <= 0:
            raise ValueError("ASR produced no speech segments")
        align_model, metadata = whisperx.load_align_model(
            language_code=language,
            device=self.device,
            model_dir=cache,
            model_cache_only=True,
        )
        aligned = whisperx.align(
            [{"text": script, "start": 0.0, "end": duration_s}],
            align_model,
            metadata,
            waveform,
            self.device,
            return_char_alignments=True,
        )
        characters = [
            character
            for segment in aligned["segments"]
            for character in segment.get("chars", [])
        ]
        if "".join(character["char"] for character in characters) != script:
            raise ValueError(
                "forced alignment did not preserve the controlled fixture script"
            )
        timeline: list[dict[str, Any]] = []
        for index, character in enumerate(characters):
            if "start" not in character or "end" not in character:
                continue
            start_us = round(float(character["start"]) * 1_000_000)
            end_us = round(float(character["end"]) * 1_000_000)
            if end_us > start_us:
                timeline.append(
                    {
                        "start_char": index,
                        "end_char": index + 1,
                        "start_us": start_us,
                        "end_us": end_us,
                    }
                )
        return timeline


def _normalize_for_script_check(text: str) -> str:
    return "".join(
        character for character in text.casefold() if not character.isspace()
    )


def boundary_p95_ms(
    aligned: list[dict[str, Any]], annotations: list[dict[str, Any]]
) -> float:
    """Calculate P95 endpoint error from supplied manual annotations only."""
    errors_ms = boundary_errors_ms(aligned, annotations)
    return errors_ms[math.ceil(len(errors_ms) * 0.95) - 1]


def boundary_errors_ms(
    aligned: list[dict[str, Any]], annotations: list[dict[str, Any]]
) -> list[float]:
    """Return endpoint errors so the harness can calculate a cross-case P95."""
    errors_ms: list[float] = []
    for annotation in annotations:
        matching = [
            span
            for span in aligned
            if span["start_char"] == annotation["start_char"]
            and span["end_char"] == annotation["end_char"]
        ]
        if len(matching) != 1:
            raise ValueError("manual annotation has no aligned span")
        span = matching[0]
        errors_ms.extend(
            [
                abs(span["start_us"] - annotation["start_us"]) / 1_000,
                abs(span["end_us"] - annotation["end_us"]) / 1_000,
            ]
        )
    if not errors_ms:
        raise ValueError("no manually annotated boundaries")
    errors_ms.sort()
    return errors_ms


def execute_alignment_case(
    *,
    case: dict[str, Any],
    audio: Path,
    script: str,
    annotations: list[dict[str, Any]],
    runner: AlignmentRunner,
    fps: int,
) -> dict[str, Any]:
    """Run one actual candidate alignment and compile its required event."""
    aligned = runner.align(audio, language=case["language"], script=script)
    event_frames = compile_events(case["required_event_selection"], aligned, fps=fps)
    return {
        "engine_version": runner.engine_version,
        "device": runner.device,
        "aligned_words": aligned,
        "boundary_p95_ms": boundary_p95_ms(aligned, annotations),
        "boundary_errors_ms": boundary_errors_ms(aligned, annotations),
        "annotated_boundary_count": len(annotations),
        "event_frames": event_frames,
    }


def compile_events(selection: dict, words: list[dict], *, fps: int) -> dict[str, int]:
    """Map a required script character selection to real narration frame bounds."""
    if fps <= 0:
        raise ValueError("fps must be positive")
    start_char, end_char = selection["start_char"], selection["end_char"]
    if start_char >= end_char:
        raise ValueError("selection is empty")
    matched = [
        word
        for word in words
        if word["start_char"] < end_char and word["end_char"] > start_char
    ]
    if not matched:
        raise ValueError(f"unresolved selection: {selection['id']}")
    covered_until = start_char
    for word in sorted(
        matched, key=lambda item: (item["start_char"], item["end_char"])
    ):
        if word["start_us"] < 0 or word["end_us"] <= word["start_us"]:
            raise ValueError("invalid aligned time")
        if word["start_char"] > covered_until:
            raise ValueError(f"unresolved selection: {selection['id']}")
        covered_until = max(covered_until, word["end_char"])
    if covered_until < end_char:
        raise ValueError(f"unresolved selection: {selection['id']}")
    start_us = min(word["start_us"] for word in matched)
    end_us = max(word["end_us"] for word in matched)
    if start_us < 0 or end_us <= start_us:
        raise ValueError("invalid aligned time")
    return {
        "start_frame": start_us * fps // 1_000_000,
        "end_frame": (end_us * fps + 999_999) // 1_000_000,
    }


def evaluate_p0_gate(
    *,
    cases: list[dict[str, Any]],
    asr_executable: bool,
    annotated_boundary_counts: dict[str, int],
    boundary_p95_ms: dict[str, float | None],
) -> dict[str, Any]:
    """Return the evidence-based P0 decision without inventing ASR quality data."""
    blockers: list[str] = []
    if not asr_executable:
        blockers.append("ASR/forced alignment is not executable")
    for language in ("zh", "en"):
        count = annotated_boundary_counts.get(language, 0)
        p95 = boundary_p95_ms.get(language)
        if count < 100:
            blockers.append(
                f"{language} requires 100 manually annotated boundaries (found {count})"
            )
        if p95 is None:
            blockers.append(f"{language} boundary P95 has not been measured")
        elif p95 > 200:
            blockers.append(f"{language} boundary P95 exceeds 200ms ({p95:g}ms)")
    for case in cases:
        case_id = str(case.get("case_id", "unknown"))
        status = case.get("status")
        unresolved = case.get("unresolved_required_events", [])
        if status not in {
            "rendered",
            "validated",
            "expected_no_speech",
            "expected_unresolved",
        }:
            blockers.append(
                f"{case_id} has no conclusive result ({status or 'missing status'})"
            )
        if unresolved and status not in {"expected_no_speech", "expected_unresolved"}:
            blockers.append(f"{case_id} has unresolved required events")
    return {"status": "passed" if not blockers else "blocked", "blockers": blockers}


def _ass_caption(
    caption: str, *, start_frame: int, end_frame: int, fps: int, width: int, height: int
) -> str:
    def stamp(frame: int) -> str:
        hundredths = math.floor(frame * 100 / fps)
        return f"{hundredths // 360000}:{hundredths // 6000 % 60:02d}:{hundredths // 100 % 60:02d}.{hundredths % 100:02d}"

    safe = (
        caption.replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\n", r"\N")
    )
    font_size = max(12, round(height * 0.052))
    return (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {width}\nPlayResY: {height}\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,Arial,{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
        "0,0,0,0,100,100,0,0,1,2,0,2,20,20,30,1\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        f"Dialogue: 0,{stamp(start_frame)},{stamp(end_frame)},Default,,0,0,0,,{safe}\n"
    )


def render_sample(
    *,
    background: Path,
    broll: Path,
    narration: Path,
    caption: str,
    start_frame: int,
    end_frame: int,
    duration_frames: int,
    output: Path,
    width: int = 1080,
    height: int = 1920,
    fps: int = 30,
) -> None:
    """Render one frame-aligned B-roll overlay, ASS subtitle, and independent narration."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required")
    if (
        not (0 <= start_frame < end_frame <= duration_frames)
        or min(width, height, fps) <= 0
    ):
        raise ValueError("invalid output geometry or event frames")
    for source in (background, broll, narration):
        if not source.is_file():
            raise FileNotFoundError(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="remix-p0-") as temporary:
        ass_path = Path(temporary) / "caption.ass"
        ass_path.write_text(
            _ass_caption(
                caption,
                start_frame=start_frame,
                end_frame=end_frame,
                fps=fps,
                width=width,
                height=height,
            ),
            encoding="utf-8-sig",
        )
        start = start_frame / fps
        end = end_frame / fps
        duration = duration_frames / fps
        # tempfile names contain no FFmpeg filter separators; no user path enters the filter string.
        graph = (
            f"[0:v]scale={width}:{height},setsar=1,trim=duration={duration},setpts=PTS-STARTPTS[bg];"
            f"[1:v]scale={width}:{height},setsar=1,setpts=PTS-STARTPTS+{start}/TB[fg];"
            f"[bg][fg]overlay=eof_action=pass:enable='gte(t,{start})*lt(t,{end})',"
            f"ass={ass_path.as_posix()}[v]"
        )
        cmd = [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-loop",
            "1",
            "-framerate",
            str(fps),
            "-i",
            str(background),
            "-i",
            str(broll),
            "-i",
            str(narration),
            "-filter_complex",
            graph,
            "-map",
            "[v]",
            "-map",
            "2:a:0",
            "-frames:v",
            str(duration_frames),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-t",
            str(duration),
            str(output),
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
