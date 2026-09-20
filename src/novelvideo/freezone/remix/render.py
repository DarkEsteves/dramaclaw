"""Validate and render version-one layered remix manifests with FFmpeg."""

from __future__ import annotations

import json
import hashlib
import math
import os
import shutil
import subprocess
import tempfile
import re
from pathlib import Path
from typing import Any


def build_manifest(
    timing: dict[str, Any],
    media_bindings: dict[str, dict[str, Any]],
    style: dict[str, Any],
) -> dict[str, Any]:
    """Bind resolved semantic frame events to existing media and subtitle rules."""
    if timing.get("unresolved_required_events"):
        raise ValueError("unresolved required semantic events block composition")
    if timing.get("override_conflicts"):
        raise ValueError("manual timing override conflicts block composition")
    spec = style.get("output_spec")
    if not isinstance(spec, dict) or timing.get("fps") != spec.get("fps"):
        raise ValueError("timing fps must match output specification")
    background_id = style.get("background_media_id")
    narration_id = style.get("narration_media_id")
    duration_frames = spec.get("duration_frames")
    if not isinstance(background_id, str) or not isinstance(narration_id, str):
        raise ValueError("background and narration media are required")
    rules = style.get("event_rules", {})
    layers = [
        {
            "media_id": background_id,
            "start_frame": 0,
            "end_frame": duration_frames,
            "z_order": 0,
        }
    ]
    subtitles: list[dict[str, Any]] = []
    for event in timing.get("events", []):
        event_id = event.get("event_id")
        rule = rules.get(event_id)
        if not isinstance(rule, dict):
            raise ValueError(f"semantic event has no composition rule: {event_id}")
        start, end = event.get("start_frame"), event.get("end_frame")
        if rule.get("media_id"):
            layer = {
                "media_id": rule["media_id"],
                "start_frame": start,
                "end_frame": end,
                "z_order": rule.get("z_order", 1),
            }
            for key in ("x", "y", "width", "height", "opacity", "fit"):
                if key in rule:
                    layer[key] = rule[key]
            layers.append(layer)
        if rule.get("caption"):
            subtitles.append(
                {"text": rule["caption"], "start_frame": start, "end_frame": end}
            )
        if not rule.get("media_id") and not rule.get("caption"):
            raise ValueError(
                f"semantic event has no visual or subtitle output: {event_id}"
            )
    manifest = {
        "output_spec": spec,
        "media_bindings": [
            {"media_id": key, **value} for key, value in media_bindings.items()
        ],
        "layers": layers,
        "subtitle_events": subtitles,
        "audio_events": [
            {
                "media_id": narration_id,
                "role": "narration",
                "start_frame": 0,
                "end_frame": duration_frames,
                "volume": 1.0,
            }
        ],
        "manual_overrides": style.get("manual_overrides", []),
        "source_versions": style.get("source_versions", []),
    }
    return validate_manifest(manifest)


def _interval(row: dict[str, Any], duration_frames: int) -> tuple[int, int]:
    start, end = row.get("start_frame"), row.get("end_frame")
    if (
        type(start) is not int
        or type(end) is not int
        or not 0 <= start < end <= duration_frames
    ):
        raise ValueError("event interval exceeds output duration or is empty")
    return start, end


def validate_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Reject unsupported or incomplete media and time bindings before spawning FFmpeg."""
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object")
    spec = manifest.get("output_spec")
    if not isinstance(spec, dict):
        raise ValueError("output_spec is required")
    width, height, fps, duration_frames = (
        spec.get("width"),
        spec.get("height"),
        spec.get("fps"),
        spec.get("duration_frames"),
    )
    if any(
        type(value) is not int or value <= 0
        for value in (width, height, fps, duration_frames)
    ):
        raise ValueError("output geometry and duration must be positive integers")
    if width % 2 or height % 2 or width * 16 != height * 9:
        raise ValueError("output must be even-sized 9:16 portrait video")
    if fps > 60 or duration_frames > 60 * fps:
        raise ValueError("output duration or fps exceeds the remix MVP limit")

    bindings: dict[str, str] = {}
    for row in manifest.get("media_bindings", []):
        media_id, kind = row.get("media_id"), row.get("kind")
        if not isinstance(media_id, str) or not media_id or media_id in bindings:
            raise ValueError("media bindings require unique nonempty IDs")
        if kind not in {"image", "video", "audio"}:
            raise ValueError(f"unsupported media kind for {media_id}")
        if "loop" in row and (kind not in {"video", "audio"} or type(row["loop"]) is not bool):
            raise ValueError(f"loop is only supported for video or audio media: {media_id}")
        if not isinstance(row.get("media_hash"), str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", row["media_hash"]):
            raise ValueError(f"media_hash is required for {media_id}")
        bindings[media_id] = kind

    layers = manifest.get("layers", [])
    if not layers:
        raise ValueError("at least one visual layer is required")
    for layer in layers:
        media_id = layer.get("media_id")
        if bindings.get(media_id) not in {"image", "video"}:
            raise ValueError(f"missing or invalid visual media binding: {media_id}")
        _interval(layer, duration_frames)
        if type(layer.get("z_order")) is not int:
            raise ValueError("layer z_order must be an integer")
        x, y = layer.get("x", 0), layer.get("y", 0)
        layer_width, layer_height = (
            layer.get("width", width),
            layer.get("height", height),
        )
        if any(type(value) is not int for value in (x, y, layer_width, layer_height)):
            raise ValueError("layer geometry must use integer pixels")
        if (
            x < 0
            or y < 0
            or layer_width <= 0
            or layer_height <= 0
            or x + layer_width > width
            or y + layer_height > height
        ):
            raise ValueError("layer geometry exceeds output canvas")
        opacity = layer.get("opacity", 1)
        if (
            isinstance(opacity, bool)
            or not isinstance(opacity, (int, float))
            or not 0 <= opacity <= 1
        ):
            raise ValueError("layer opacity must be between 0 and 1")
        if layer.get("fit", "stretch") not in {"stretch", "cover", "contain"}:
            raise ValueError("unsupported layer fit mode")
    for event in manifest.get("subtitle_events", []):
        _interval(event, duration_frames)
        if not isinstance(event.get("text"), str) or not event["text"].strip():
            raise ValueError("subtitle text is required")
        cues = event.get("word_cues", [])
        if not isinstance(cues, list):
            raise ValueError("subtitle word cues must be a list")
        previous_frame, previous_char = event["start_frame"], 0
        for cue in cues:
            if not isinstance(cue, dict):
                raise ValueError("subtitle word cue must be an object")
            start_char, end_char = cue.get("start_char"), cue.get("end_char")
            start_frame, end_frame = cue.get("start_frame"), cue.get("end_frame")
            if (
                any(type(value) is not int for value in (start_char, end_char, start_frame, end_frame))
                or not previous_char <= start_char < end_char <= len(event["text"])
                or not previous_frame <= start_frame < end_frame <= event["end_frame"]
            ):
                raise ValueError("subtitle word cue exceeds its text or time interval")
            previous_frame, previous_char = end_frame, end_char
    audio_events = manifest.get("audio_events", [])
    if not any(row.get("role") == "narration" for row in audio_events):
        raise ValueError("narration audio event is required")
    for event in audio_events:
        media_id = event.get("media_id")
        if bindings.get(media_id) != "audio":
            raise ValueError(f"missing or invalid audio media binding: {media_id}")
        _interval(event, duration_frames)
        if event.get("role") not in {"narration", "bgm", "sfx"}:
            raise ValueError("unsupported audio role")
        volume = event.get("volume", 1)
        if (
            isinstance(volume, bool)
            or not isinstance(volume, (int, float))
            or not 0 <= volume <= 2
        ):
            raise ValueError("audio volume must be between 0 and 2")
    return manifest


def _ass_time(frame: int, fps: int) -> str:
    hundredths = math.floor(frame * 100 / fps)
    return f"{hundredths // 360000}:{hundredths // 6000 % 60:02d}:{hundredths // 100 % 60:02d}.{hundredths % 100:02d}"


def _ass_document(
    events: list[dict[str, Any]], *, width: int, height: int, fps: int
) -> str:
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {width}\nPlayResY: {height}\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,Arial,{max(12, round(height * 0.052))},&H00FFFFFF,&H000000FF,&H00000000,"
        "&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,20,20,30,1\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = []
    for event in events:
        cues = event.get("word_cues", [])
        boundaries = sorted({event["start_frame"], event["end_frame"], *(frame for cue in cues for frame in (cue["start_frame"], cue["end_frame"]))})
        for start, end in zip(boundaries, boundaries[1:]):
            active = next((cue for cue in cues if cue["start_frame"] <= start < cue["end_frame"]), None)
            if active is None:
                safe = _ass_escape(event["text"])
            else:
                left, right = active["start_char"], active["end_char"]
                safe = (
                    _ass_escape(event["text"][:left])
                    + r"{\c&H00CFBD00&\b1}" + _ass_escape(event["text"][left:right])
                    + r"{\r}" + _ass_escape(event["text"][right:])
                )
            lines.append(
                f"Dialogue: 0,{_ass_time(start, fps)},{_ass_time(end, fps)},"
                f"Default,,0,0,0,,{safe}"
            )
    return header + "\n".join(lines) + "\n"


def _ass_escape(text: str) -> str:
    return (
        text.replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\n", r"\N")
    )


def _duration_seconds(path: Path) -> float:
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
    duration = float(json.loads(result.stdout)["format"]["duration"])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("media has invalid duration")
    return duration


def render_manifest(
    manifest: dict[str, Any], *, media_paths: dict[str, Path], output_path: Path
) -> Path:
    """Render an authorized media table using the exact saved composition manifest."""
    validate_manifest(manifest)
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("FFmpeg and ffprobe are required")
    spec = manifest["output_spec"]
    width, height, fps, frames = (
        spec[key] for key in ("width", "height", "fps", "duration_frames")
    )
    duration = frames / fps
    bindings = manifest["media_bindings"]
    media_indices: dict[str, int] = {}
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s={width}x{height}:r={fps}:d={duration}",
    ]
    for index, binding in enumerate(bindings, start=1):
        media_id = binding["media_id"]
        path = media_paths.get(media_id)
        if path is None or not Path(path).is_file():
            raise FileNotFoundError(f"bound media is unavailable: {media_id}")
        digest = hashlib.sha256()
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1_048_576), b""):
                digest.update(chunk)
        if f"sha256:{digest.hexdigest()}" != binding["media_hash"]:
            raise ValueError(f"bound media changed since manifest publication: {media_id}")
        media_indices[media_id] = index
        if binding["kind"] == "image":
            command.extend(["-loop", "1", "-framerate", str(fps)])
        elif binding.get("loop"):
            command.extend(["-stream_loop", "-1"])
        command.extend(["-i", str(path)])

    for row in [*manifest["layers"], *manifest["audio_events"]]:
        media_id = row["media_id"]
        kind = next(item["kind"] for item in bindings if item["media_id"] == media_id)
        if kind == "image":
            continue
        required = (row["end_frame"] - row["start_frame"]) / fps
        binding = next(item for item in bindings if item["media_id"] == media_id)
        if not binding.get("loop") and _duration_seconds(Path(media_paths[media_id])) + 0.5 / fps < required:
            raise ValueError(f"bound media {media_id} is shorter than its event")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".remix-render-", dir=output_path.parent
    ) as temporary:
        temp_dir = Path(temporary)
        ass_path = temp_dir / "captions.ass"
        ass_path.write_text(
            _ass_document(
                manifest.get("subtitle_events", []), width=width, height=height, fps=fps
            ),
            encoding="utf-8-sig",
        )
        filters = ["[0:v]setpts=PTS-STARTPTS[base0]"]
        base = "base0"
        for i, layer in enumerate(
            sorted(manifest["layers"], key=lambda row: row["z_order"]), start=1
        ):
            start, end = layer["start_frame"], layer["end_frame"]
            source = media_indices[layer["media_id"]]
            segment = f"layer{i}"
            next_base = f"base{i}"
            layer_width = layer.get("width", width)
            layer_height = layer.get("height", height)
            fit = layer.get("fit", "stretch")
            if fit == "cover":
                image_filter = (
                    f"scale={layer_width}:{layer_height}:force_original_aspect_ratio=increase,"
                    f"crop={layer_width}:{layer_height}"
                )
            elif fit == "contain":
                image_filter = (
                    f"scale={layer_width}:{layer_height}:force_original_aspect_ratio=decrease,"
                    f"format=rgba,pad={layer_width}:{layer_height}:(ow-iw)/2:(oh-ih)/2:color=black@0"
                )
            else:
                image_filter = f"scale={layer_width}:{layer_height}"
            filters.append(
                f"[{source}:v]{image_filter},setsar=1,"
                f"trim=duration={(end - start) / fps},setpts=PTS-STARTPTS+{start / fps}/TB,"
                f"format=rgba,colorchannelmixer=aa={layer.get('opacity', 1)}[{segment}]"
            )
            filters.append(
                f"[{base}][{segment}]overlay=x={layer.get('x', 0)}:y={layer.get('y', 0)}:eof_action=pass:"
                f"enable='gte(t,{start / fps})*lt(t,{end / fps})'[{next_base}]"
            )
            base = next_base
        filters.append(f"[{base}]ass={ass_path.as_posix()}[video]")

        audio_labels = []
        for i, event in enumerate(manifest["audio_events"]):
            source = media_indices[event["media_id"]]
            start, end = event["start_frame"], event["end_frame"]
            label = f"audio{i}"
            delay_ms = round(start * 1000 / fps)
            filters.append(
                f"[{source}:a]atrim=duration={(end - start) / fps},asetpts=PTS-STARTPTS,"
                f"volume={event.get('volume', 1)},adelay={delay_ms}|{delay_ms}[{label}]"
            )
            audio_labels.append(label)
        if len(audio_labels) > 1:
            filters.append(
                "".join(f"[{label}]" for label in audio_labels)
                + f"amix=inputs={len(audio_labels)}:duration=longest[audio]"
            )
        elif audio_labels:
            filters.append(f"[{audio_labels[0]}]anull[audio]")

        staged_output = temp_dir / "output.mp4"
        command.extend(
            [
                "-filter_complex",
                ";".join(filters),
                "-map",
                "[video]",
                "-map",
                "[audio]",
                "-frames:v",
                str(frames),
                "-t",
                str(duration),
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(staged_output),
            ]
        )
        subprocess.run(command, check=True, capture_output=True, text=True)
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(staged_output),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        os.replace(staged_output, output_path)
    return output_path
