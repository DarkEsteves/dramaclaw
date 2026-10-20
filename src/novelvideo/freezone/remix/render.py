"""Validate and render version-one layered remix manifests with FFmpeg."""

from __future__ import annotations

import json
import hashlib
from copy import deepcopy
import math
import os
import shutil
import subprocess
import tempfile
import re
from fractions import Fraction
from pathlib import Path
from typing import Any

from .graphic_effects import GraphicEffect


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
    version = manifest.get("manifest_version", 1)
    if type(version) is not int or version not in {1, 2, 3, 4}:
        raise ValueError("unsupported composition manifest version")
    if version == 4:
        allowed = {
            "manifest": {"manifest_version", "output_spec", "media_bindings", "layers", "subtitle_events", "graphic_events",
                         "audio_events", "manual_overrides", "source_versions"},
            "output_spec": {"width", "height", "fps_num", "fps_den", "duration_frames", "background", "subtitle_safe_area"},
            "media_bindings": {"media_id", "kind", "project_path", "media_hash", "has_audio", "loop", "purpose"},
            "layers": {"placement_id", "row_id", "media_id", "start_frame", "end_frame", "z_order", "fit",
                       "x", "y", "width", "height", "opacity", "source_in_us", "source_out_us", "speed_num",
                       "speed_den", "crop", "fade_in_frames", "fade_out_frames", "zoom"},
            "audio_events": {"placement_id", "row_id", "media_id", "role", "start_frame", "end_frame",
                             "source_in_us", "source_out_us", "speed_num", "speed_den", "volume", "duck_under_narration"},
            "subtitle_events": {"placement_id", "row_id", "text", "start_frame", "end_frame", "word_cues", "timing_basis", "x", "y", "font_size"},
            "graphic_events": {"placement_id", "row_id", "start_frame", "end_frame", "template", "text", "emphasis",
                               "foreground", "background", "accent", "enter", "enter_frames", "exit_frames",
                               "x", "y", "font_size", "sample_frame", "original_span"},
        }
        if extra := set(manifest) - allowed["manifest"]:
            raise ValueError(f"unsupported manifest fields: {sorted(extra)}")
        for name in ("media_bindings", "layers", "audio_events", "subtitle_events", "graphic_events"):
            rows = manifest.get(name, [] if name == "graphic_events" else None)
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError(f"{name} must be a list of objects")
            for row in rows:
                if extra := set(row) - allowed[name]:
                    label = "layer" if name == "layers" else name
                    raise ValueError(f"unsupported {label} fields: {sorted(extra)}")
    spec = manifest.get("output_spec")
    if not isinstance(spec, dict):
        raise ValueError("output_spec is required")
    if version == 4 and (extra := set(spec) - allowed["output_spec"]):
        raise ValueError(f"unsupported output_spec fields: {sorted(extra)}")
    width, height, fps, duration_frames = (
        spec.get("width"),
        spec.get("height"),
        spec.get("fps"),
        spec.get("duration_frames"),
    )
    if version == 4:
        if any(type(value) is not int or value <= 0 for value in (
            width, height, spec.get("fps_num"), spec.get("fps_den"), duration_frames
        )):
            raise ValueError("output geometry, rational fps and duration must be positive integers")
        if width % 2 or height % 2:
            raise ValueError("encoded output dimensions must be even")
        if not isinstance(spec.get("background"), str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", spec["background"]):
            raise ValueError("output background must be a hex color")
        fps = Fraction(spec["fps_num"], spec["fps_den"])
    else:
        if any(type(value) is not int or value <= 0 for value in (width, height, fps, duration_frames)):
            raise ValueError("output geometry and duration must be positive integers")
        if width % 2 or height % 2 or width * 16 != height * 9:
            raise ValueError("output must be even-sized 9:16 portrait video")
        if fps > 60 or duration_frames > 60 * fps:
            raise ValueError("output duration or fps exceeds the remix MVP limit")
    safe_area = spec.get("subtitle_safe_area")
    if version in {2, 3}:
        if (width, height, fps) != (1080, 1920, 30):
            raise ValueError("version 2 output must be 1080x1920 at 30 fps")
    if version in {2, 3} or (version == 4 and safe_area is not None):
        if not isinstance(safe_area, dict) or any(
            type(safe_area.get(key)) is not int or safe_area[key] < 0
            for key in ("left", "right", "top", "bottom")
        ):
            raise ValueError("output subtitle safe area is invalid")
        if (
            safe_area["left"] + safe_area["right"] >= width
            or safe_area["top"] + safe_area["bottom"] >= height
        ):
            raise ValueError("subtitle safe area exceeds the output canvas")

    bindings: dict[str, str] = {}
    binding_purposes: dict[str, str | None] = {}
    binding_has_audio: dict[str, bool] = {}
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
        binding_purposes[media_id] = row.get("purpose")
        binding_has_audio[media_id] = row.get("has_audio") is True

    layers = manifest.get("layers", [])
    if not layers and version != 4:
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
        if version in {2, 3, 4}:
            crop = layer.get("crop")
            if crop is not None:
                if not isinstance(crop, dict) or any(
                    type(crop.get(key)) is not int
                    for key in ("x", "y", "width", "height")
                ):
                    raise ValueError("layer crop must use integer pixels")
                if (
                    crop["x"] < 0
                    or crop["y"] < 0
                    or crop["width"] <= 0
                    or crop["height"] <= 0
                ):
                    raise ValueError("layer crop must be a positive source rectangle")
            span = layer["end_frame"] - layer["start_frame"]
            for key in ("fade_in_frames", "fade_out_frames"):
                value = layer.get(key, 0)
                if type(value) is not int or value < 0 or value > span:
                    raise ValueError("layer fade exceeds its event interval")
            if (
                layer.get("fade_in_frames", 0) + layer.get("fade_out_frames", 0)
                > span
            ):
                raise ValueError("layer fades overlap")
            zoom = layer.get("zoom")
            if zoom is not None:
                if not isinstance(zoom, dict) or any(
                    isinstance(zoom.get(key), bool)
                    or not isinstance(zoom.get(key), (int, float))
                    or not 1 <= zoom[key] <= 4
                    for key in ("start", "end")
                ):
                    raise ValueError(
                        "layer zoom must contain bounded start and end scales"
                    )
    if version in {2, 3}:
        primary_layers = sorted((layer for layer in layers
            if layer.get("z_order") == 0
            and binding_purposes.get(layer.get("media_id")) in {"background", "a-roll"}
            and layer.get("x", 0) == 0 and layer.get("y", 0) == 0
            and layer.get("width", width) == width and layer.get("height", height) == height
            and layer.get("opacity", 1) == 1
            and not layer.get("fade_in_frames", 0) and not layer.get("fade_out_frames", 0)
        ), key=lambda layer: layer["start_frame"])
        covered_until = 0
        for layer in primary_layers:
            if layer["start_frame"] > covered_until:
                break
            covered_until = max(covered_until, layer["end_frame"])
        if covered_until < duration_frames:
            raise ValueError("version 2 requires continuous primary visual coverage")
    for event in manifest.get("subtitle_events", []):
        _interval(event, duration_frames)
        if not isinstance(event.get("text"), str) or not event["text"].strip():
            raise ValueError("subtitle text is required")
        if version == 4:
            x, y, font_size = event.get("x"), event.get("y"), event.get("font_size")
            if (x is None) != (y is None) or (x is not None and (
                type(x) is not int or type(y) is not int or not 0 <= x <= width or not 0 <= y <= height
            )) or (font_size is not None and (type(font_size) is not int or not 1 <= font_size <= height)):
                raise ValueError("subtitle position or font size is invalid")
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
    if version == 4:
        for event in manifest.get("graphic_events", []):
            start, end = _interval(event, duration_frames)
            effect = GraphicEffect.model_validate({key: value for key, value in event.items()
                                                   if key not in {"placement_id", "row_id", "start_frame", "end_frame", "sample_frame", "original_span"}})
            sample_frame = event.get("sample_frame")
            if sample_frame is not None and (type(sample_frame) is not int or sample_frame < 0):
                raise ValueError("graphic sample frame must be non-negative")
            original_span = event.get("original_span")
            if original_span is not None and (sample_frame is None or type(original_span) is not int
                                             or original_span <= 0 or sample_frame >= original_span):
                raise ValueError("graphic preview span is invalid")
            if sample_frame is None and effect.enter_frames + effect.exit_frames > end - start:
                raise ValueError("graphic animation exceeds its interval")
            if effect.x is not None and effect.x > width or effect.y is not None and effect.y > height:
                raise ValueError("graphic position exceeds output canvas")
    audio_events = manifest.get("audio_events", [])
    if version in {1, 2} and not any(row.get("role") == "narration" for row in audio_events):
        raise ValueError("narration audio event is required")
    for event in audio_events:
        media_id = event.get("media_id")
        if bindings.get(media_id) != "audio" and not (version in {3, 4} and bindings.get(media_id) == "video" and binding_has_audio.get(media_id)):
            if version in {3, 4} and bindings.get(media_id) == "video":
                raise ValueError(f"video speech media requires an audio stream: {media_id}")
            raise ValueError(f"missing or invalid audio media binding: {media_id}")
        _interval(event, duration_frames)
        if event.get("role") not in ({"narration", "bgm", "sfx", "speech"} if version in {3, 4} else {"narration", "bgm", "sfx"}):
            raise ValueError("unsupported audio role")
        volume = event.get("volume", 1)
        if (
            isinstance(volume, bool)
            or not isinstance(volume, (int, float))
            or not 0 <= volume <= 2
        ):
            raise ValueError("audio volume must be between 0 and 2")
        if version == 3 and event.get("role") == "speech" and bindings.get(media_id) == "video" and not any(
            layer.get("media_id") == media_id
            and layer.get("start_frame") == event.get("start_frame")
            and layer.get("end_frame") == event.get("end_frame")
            and layer.get("z_order") == 0 for layer in layers
        ):
            raise ValueError("speaking video speech and visual spans must match")
        ducking = event.get("duck_under_narration") if version in {2, 3} else None
        if ducking is not None and (
            event.get("role") != "bgm"
            or isinstance(ducking, bool)
            or not isinstance(ducking, (int, float))
            or not 0 <= ducking <= 1
        ):
            raise ValueError("BGM duck factor must be between 0 and 1")
    if version == 2:
        narration_ranges = sorted(
            (_interval(row, duration_frames) for row in audio_events if row.get("role") == "narration")
        )
        cursor = 0
        for start, end in narration_ranges:
            if start > cursor:
                raise ValueError("version 2 requires continuous narration")
            cursor = max(cursor, end)
        if cursor != duration_frames:
            raise ValueError("version 2 requires continuous narration")
    if version == 4:
        seen_placements: set[str] = set()
        for row in [*layers, *audio_events, *manifest.get("subtitle_events", []), *manifest.get("graphic_events", [])]:
            placement_id = row.get("placement_id")
            if not isinstance(placement_id, str) or not placement_id or placement_id in seen_placements:
                raise ValueError("version 4 events need unique placement_id values")
            seen_placements.add(placement_id)
            if row.get("media_id") is None:
                continue
            source_in, source_out = row.get("source_in_us"), row.get("source_out_us")
            speed_num, speed_den = row.get("speed_num"), row.get("speed_den")
            if any(type(value) is not int for value in (source_in, source_out, speed_num, speed_den)) or (
                source_in < 0 or source_out <= source_in or speed_num <= 0 or speed_den <= 0
            ):
                raise ValueError(f"placement {placement_id} has invalid source time or speed")
            expected = Fraction(row["end_frame"] - row["start_frame"], 1) / fps * 1_000_000 * Fraction(speed_num, speed_den)
            # A real media duration rarely lands exactly on a program frame.
            # The nearest output frame may differ by at most half a source frame.
            tolerance = max(1, Fraction(500_000, 1) / fps * Fraction(speed_num, speed_den))
            if abs(source_out - source_in - expected) > tolerance:
                raise ValueError(f"placement {placement_id} source range does not match output span and speed")
    return manifest


def manifest_renderer(manifest: dict[str, Any]) -> str:
    """Return the stable renderer route without silently upgrading old manifests."""
    version = manifest.get("manifest_version", 1)
    if version == 1:
        return "legacy_v1"
    if version == 2:
        return "layered_v2"
    if version == 3:
        return "layered_v3"
    if version == 4:
        return "layered_v4"
    raise ValueError("unsupported composition manifest version")


def _ass_time(frame: int, fps: int) -> str:
    hundredths = math.floor(frame * 100 / fps)
    return f"{hundredths // 360000}:{hundredths // 6000 % 60:02d}:{hundredths // 100 % 60:02d}.{hundredths % 100:02d}"


def _ass_document(
    events: list[dict[str, Any]],
    *,
    width: int,
    height: int,
    fps: int,
    safe_area: dict[str, int] | None = None,
    graphic_events: list[dict[str, Any]] | None = None,
) -> str:
    margins = safe_area or {
        "left": 20,
        "right": 20,
        "top": 30,
        "bottom": 30,
    }
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {width}\nPlayResY: {height}\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,Arial,{max(12, round(height * 0.052))},&H00FFFFFF,&H000000FF,&H00000000,"
        f"&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,{margins['left']},"
        f"{margins['right']},{margins['bottom']},1\n"
    )
    for index, graphic in enumerate(graphic_events or []):
        template = graphic["template"]
        default_size = round(height * {"question_card": .06, "keyword": .075, "title_bar": .065}[template])
        font_size = graphic.get("font_size") or max(12, default_size)
        primary = graphic["accent"] if template == "keyword" else graphic["foreground"]
        border_style = 1 if template == "keyword" else 3
        outline = 0 if template == "keyword" else max(6, round(height * .02))
        # ASS opaque boxes take their fill from OutlineColour, not BackColour.
        header += (f"Style: Graphic{index},Arial,{font_size},{_ass_color(primary)},"
                   f"{_ass_color(graphic['accent'])},{_ass_color(graphic['background'])},&H00000000,"
                   f"1,0,0,0,100,100,0,0,{border_style},{outline},0,5,0,0,0,1\n")
    header += "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
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
            style = ""
            if event.get("x") is not None:
                style += rf"\an2\pos({event['x']},{event['y']})"
            if event.get("font_size") is not None:
                style += rf"\fs{event['font_size']}"
            if style:
                safe = "{" + style + "}" + safe
            subtitle_layer = 2 if graphic_events else 0
            lines.append(
                f"Dialogue: {subtitle_layer},{_ass_time(start, fps)},{_ass_time(end, fps)},"
                f"Default,,0,0,0,,{safe}"
            )
    for index, graphic in enumerate(graphic_events or []):
        start, end = graphic["start_frame"], graphic["end_frame"]
        template = graphic["template"]
        x = graphic.get("x") if graphic.get("x") is not None else width // 2
        y = graphic.get("y") if graphic.get("y") is not None else round(height * {
            "question_card": .25, "keyword": .50, "title_bar": .12,
        }[template])
        text = graphic["text"]
        emphasis = graphic.get("emphasis")
        if emphasis:
            before, after = text.split(emphasis, 1)
            text = (_ass_escape(before) + "{" + rf"\c{_ass_color(graphic['accent'])}\b1" + "}"
                    + _ass_escape(emphasis) + r"{\rGraphic" + str(index) + "}" + _ass_escape(after))
        else:
            text = _ass_escape(text)
        enter_frames = graphic.get("enter_frames", 0) if graphic.get("enter") != "none" else 0
        exit_frames = graphic.get("exit_frames", 0)
        enter_ms, exit_ms = round(enter_frames * 1000 / fps), round(exit_frames * 1000 / fps)
        tags = rf"\an5\pos({x},{y})"
        sample = graphic.get("sample_frame")
        if sample is None:
            if enter_ms or exit_ms:
                tags += rf"\fad({enter_ms},{exit_ms})"
            if graphic.get("enter") == "pop" and enter_ms:
                tags += rf"\fscx85\fscy85\t(0,{enter_ms},\fscx100\fscy100)"
        else:
            alpha = 1.0
            if enter_frames:
                alpha = min(alpha, sample / enter_frames)
            if exit_frames:
                original_span = graphic.get("original_span", end - start)
                alpha = min(alpha, (original_span - sample) / exit_frames)
            tags += rf"\alpha&H{round(255 * (1 - max(0, min(1, alpha)))):02X}&"
            if graphic.get("enter") == "pop" and enter_frames:
                scale = round(85 + 15 * min(1, sample / enter_frames))
                tags += rf"\fscx{scale}\fscy{scale}"
        lines.append(f"Dialogue: 1,{_ass_time(start, fps)},{_ass_time(end, fps)},"
                     f"Graphic{index},,0,0,0,,{{{tags}}}{text}")
    return header + "\n".join(lines) + "\n"


def _ass_color(hex_color: str) -> str:
    return f"&H00{hex_color[5:7]}{hex_color[3:5]}{hex_color[1:3]}&"


def _ass_escape(text: str) -> str:
    return (
        text.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\\", r"\\")
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


def _render_manifest_ffmpeg(
    manifest: dict[str, Any],
    *,
    media_paths: dict[str, Path],
    output_path: Path,
    layered_v2: bool,
) -> Path:
    """Render an authorized media table using the exact saved composition manifest."""
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("FFmpeg and ffprobe are required")
    spec = manifest["output_spec"]
    width, height, frames = (spec[key] for key in ("width", "height", "duration_frames"))
    version = manifest.get("manifest_version", 1)
    fps_rate = Fraction(spec["fps_num"], spec["fps_den"]) if version == 4 else Fraction(spec["fps"], 1)
    fps_arg = f"{fps_rate.numerator}/{fps_rate.denominator}" if version == 4 else str(spec["fps"])
    fps = float(fps_rate)
    duration = frames / fps
    bindings = manifest["media_bindings"]
    binding_table = {binding["media_id"]: binding for binding in bindings}
    source_seeks = {}
    if version == 4:
        for row in [*manifest["layers"], *manifest["audio_events"]]:
            media_id = row["media_id"]
            if binding_table[media_id]["kind"] != "image" and not binding_table[media_id].get("loop"):
                source_seeks[media_id] = min(source_seeks.get(media_id, row["source_in_us"]), row["source_in_us"])
    digests: dict[Path, str] = {}
    media_indices: dict[str, int] = {}
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c={'0x' + spec['background'][1:] if version == 4 else 'black'}:s={width}x{height}:r={fps_arg}:d={duration}",
    ]
    for index, binding in enumerate(bindings, start=1):
        media_id = binding["media_id"]
        path = media_paths.get(media_id)
        if path is None or not Path(path).is_file():
            raise FileNotFoundError(f"bound media is unavailable: {media_id}")
        canonical_path = Path(path).resolve()
        if canonical_path not in digests:
            digest = hashlib.sha256()
            with canonical_path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1_048_576), b""):
                    digest.update(chunk)
            digests[canonical_path] = f"sha256:{digest.hexdigest()}"
        if digests[canonical_path] != binding["media_hash"]:
            raise ValueError(f"bound media changed since manifest publication: {media_id}")
        media_indices[media_id] = index
        if binding["kind"] == "image":
            command.extend(["-loop", "1", "-framerate", fps_arg])
        elif binding.get("loop"):
            command.extend(["-stream_loop", "-1"])
        if source_seeks.get(media_id):
            command.extend(["-ss", str(source_seeks[media_id] / 1_000_000)])
        command.extend(["-i", str(path)])

    durations: dict[Path, float] = {}
    for row in [*manifest["layers"], *manifest["audio_events"]]:
        media_id = row["media_id"]
        kind = binding_table[media_id]["kind"]
        if kind == "image":
            continue
        required = (row["source_out_us"] / 1_000_000 if version == 4 else
                    (row["end_frame"] - row["start_frame"]) / fps)
        binding = binding_table[media_id]
        if not binding.get("loop"):
            path = Path(media_paths[media_id]).resolve()
            if path not in durations:
                durations[path] = _duration_seconds(path)
            if durations[path] + 0.5 / fps < required:
                raise ValueError(f"bound media {media_id} is shorter than its event")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".remix-render-", dir=output_path.parent
    ) as temporary:
        temp_dir = Path(temporary)
        ass_path = temp_dir / "captions.ass"
        ass_path.write_text(
            _ass_document(
                manifest.get("subtitle_events", []),
                width=width,
                height=height,
                fps=fps,
                safe_area=spec.get("subtitle_safe_area") if layered_v2 else None,
                graphic_events=manifest.get("graphic_events", []),
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
            image_filters: list[str] = []
            crop = layer.get("crop") if layered_v2 else None
            if crop:
                image_filters.append(
                    f"crop={crop['width']}:{crop['height']}:{crop['x']}:{crop['y']}"
                )
            if fit == "cover":
                image_filters.extend(
                    [
                        f"scale={layer_width}:{layer_height}:force_original_aspect_ratio=increase",
                        f"crop={layer_width}:{layer_height}",
                    ]
                )
            elif fit == "contain":
                image_filters.extend(
                    [
                        f"scale={layer_width}:{layer_height}:force_original_aspect_ratio=decrease",
                        "format=rgba",
                        f"pad={layer_width}:{layer_height}:(ow-iw)/2:(oh-ih)/2:color=black@0",
                    ]
                )
            else:
                image_filters.append(f"scale={layer_width}:{layer_height}")
            zoom = layer.get("zoom") if layered_v2 else None
            if zoom:
                layer_frames = end - start
                progress = f"n/{max(1, layer_frames - 1)}"
                scale = f"({zoom['start']}+({zoom['end']}-{zoom['start']})*{progress})"
                image_filters.extend(
                    [
                        f"scale=w='trunc({layer_width}*{scale}/2)*2':"
                        f"h='trunc({layer_height}*{scale}/2)*2':eval=frame",
                        f"crop={layer_width}:{layer_height}:(iw-{layer_width})/2:(ih-{layer_height})/2",
                    ]
                )
            image_filters.append("format=rgba")
            layer_duration = (end - start) / fps
            fade_in = layer.get("fade_in_frames", 0) if layered_v2 else 0
            fade_out = layer.get("fade_out_frames", 0) if layered_v2 else 0
            if fade_in:
                image_filters.append(f"fade=t=in:st=0:d={fade_in / fps}:alpha=1")
            if fade_out:
                image_filters.append(
                    f"fade=t=out:st={(end - start - fade_out) / fps}:"
                    f"d={fade_out / fps}:alpha=1"
                )
            if version == 4 and binding_table[layer["media_id"]]["kind"] == "video":
                source_start = (layer["source_in_us"] - source_seeks.get(layer["media_id"], 0)) / 1_000_000
                source_duration = (layer["source_out_us"] - layer["source_in_us"]) / 1_000_000
                image_filters.insert(0, f"trim=start={source_start}:duration={source_duration},setpts=PTS-STARTPTS")
            image_filter = ",".join(image_filters)
            speed_filter = (f"setpts=(PTS-STARTPTS)*{layer['speed_den']}/{layer['speed_num']},"
                            if version == 4 else "")
            filters.append(
                f"[{source}:v]{image_filter},setsar=1,"
                f"{speed_filter}trim=duration={layer_duration},setpts=PTS-STARTPTS+{start / fps}/TB,"
                f"colorchannelmixer=aa={layer.get('opacity', 1)}[{segment}]"
            )
            filters.append(
                f"[{base}][{segment}]overlay=x={layer.get('x', 0)}:y={layer.get('y', 0)}:eof_action=pass:"
                f"enable='gte(t,{start / fps})*lt(t,{end / fps})'[{next_base}]"
            )
            base = next_base
        # Run FFmpeg from the private staging directory so the filter graph never
        # has to parse a user-controlled filesystem path. Media paths remain
        # separate argv entries and the fixed ASS filename is filter-safe.
        filters.append(f"[{base}]ass=filename='captions.ass'[video]")

        audio_labels = []
        for i, event in enumerate(manifest["audio_events"]):
            source = media_indices[event["media_id"]]
            start, end = event["start_frame"], event["end_frame"]
            label = f"audio{i}"
            delay_ms = round(start * 1000 / fps)
            volume = event.get("volume", 1)
            if layered_v2 and event.get("role") == "bgm":
                volume *= event.get("duck_under_narration", 1)
            if version == 4:
                source_start = (event["source_in_us"] - source_seeks.get(event["media_id"], 0)) / 1_000_000
                source_duration = (event["source_out_us"] - event["source_in_us"]) / 1_000_000
                speed = event["speed_num"] / event["speed_den"]
                tempos: list[float] = []
                while speed > 2:
                    tempos.append(2.0)
                    speed /= 2
                while speed < 0.5:
                    tempos.append(0.5)
                    speed /= 0.5
                tempos.append(speed)
                tempo_filters = "".join(f",atempo={tempo}" for tempo in tempos)
                filters.append(
                    f"[{source}:a]atrim=start={source_start}:duration={source_duration},"
                    f"asetpts=PTS-STARTPTS{tempo_filters},atrim=duration={(end - start) / fps},"
                    f"volume={volume},adelay={delay_ms}|{delay_ms}[{label}]"
                )
            else:
                filters.append(
                    f"[{source}:a]atrim=duration={(end - start) / fps},asetpts=PTS-STARTPTS,"
                    f"volume={volume},adelay={delay_ms}|{delay_ms}[{label}]"
                )
            audio_labels.append(label)
        if len(audio_labels) > 1:
            filters.append(
                "".join(f"[{label}]" for label in audio_labels)
                + f"amix=inputs={len(audio_labels)}:duration=longest[audio]"
            )
        elif audio_labels:
            filters.append(f"[{audio_labels[0]}]anull[audio]")
        else:
            filters.append(
                f"anullsrc=r=48000:cl=stereo,atrim=duration={duration},"
                "asetpts=PTS-STARTPTS[audio]"
            )

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
                "-preset",
                "veryfast",
                "-r",
                fps_arg,
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(staged_output),
            ]
        )
        subprocess.run(
            command, check=True, capture_output=True, text=True, cwd=temp_dir
        )
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


def _render_manifest_v1(
    manifest: dict[str, Any], *, media_paths: dict[str, Path], output_path: Path
) -> Path:
    return _render_manifest_ffmpeg(
        manifest,
        media_paths=media_paths,
        output_path=output_path,
        layered_v2=False,
    )


def _render_manifest_v2(
    manifest: dict[str, Any], *, media_paths: dict[str, Path], output_path: Path
) -> Path:
    return _render_manifest_ffmpeg(
        manifest,
        media_paths=media_paths,
        output_path=output_path,
        layered_v2=True,
    )


def render_manifest(
    manifest: dict[str, Any], *, media_paths: dict[str, Path], output_path: Path
) -> Path:
    """Route immutable manifest versions to their compatible composer."""
    validate_manifest(manifest)
    if manifest_renderer(manifest) == "legacy_v1":
        return _render_manifest_v1(
            manifest, media_paths=media_paths, output_path=output_path
        )
    return _render_manifest_v2(
        manifest, media_paths=media_paths, output_path=output_path
    )


def _preview_snapshot_v4(manifest: dict[str, Any], frame: int) -> dict[str, Any]:
    """Compose only active visual inputs at the requested program instant."""
    snapshot = deepcopy(manifest)
    snapshot["output_spec"]["duration_frames"] = 1
    spec = snapshot["output_spec"]
    fps = Fraction(spec["fps_num"], spec["fps_den"])
    snapshot["audio_events"] = []
    active = set()
    layers = []
    for original in manifest["layers"]:
        if not original["start_frame"] <= frame < original["end_frame"]:
            continue
        row = deepcopy(original)
        offset = frame - row["start_frame"]
        span = row["end_frame"] - row["start_frame"]
        speed = Fraction(row["speed_num"], row["speed_den"])
        row["source_in_us"] += round(Fraction(offset, 1) / fps * 1_000_000 * speed)
        row["source_out_us"] = row["source_in_us"] + round(1_000_000 / fps * speed)
        opacity = row.get("opacity", 1)
        if row.get("fade_in_frames"):
            opacity *= min(1, offset / row["fade_in_frames"])
        if row.get("fade_out_frames"):
            opacity *= min(1, (span - offset) / row["fade_out_frames"])
        row["opacity"] = opacity
        row.pop("fade_in_frames", None)
        row.pop("fade_out_frames", None)
        if zoom := row.get("zoom"):
            scale = zoom["start"] + (zoom["end"] - zoom["start"]) * offset / max(1, span - 1)
            row["zoom"] = {"start": scale, "end": scale}
        row.update(start_frame=0, end_frame=1)
        layers.append(row)
        active.add(row["media_id"])
    snapshot["layers"] = layers
    snapshot["subtitle_events"] = [
        {**event, "start_frame": 0, "end_frame": 1,
         "word_cues": [{**cue, "start_frame": 0, "end_frame": 1}
                       for cue in event.get("word_cues", [])
                       if cue["start_frame"] <= frame < cue["end_frame"]][:1]}
        for event in manifest["subtitle_events"] if event["start_frame"] <= frame < event["end_frame"]
    ]
    snapshot["graphic_events"] = [
        {**event, "start_frame": 0, "end_frame": 1, "sample_frame": frame - event["start_frame"],
         "original_span": event["end_frame"] - event["start_frame"]}
        for event in manifest.get("graphic_events", []) if event["start_frame"] <= frame < event["end_frame"]
    ]
    snapshot["media_bindings"] = [binding for binding in snapshot["media_bindings"] if binding["media_id"] in active]
    return snapshot


def render_preview_frame(
    manifest: dict[str, Any], *, media_paths: dict[str, Path], frame: int, output_path: Path
) -> Path:
    """Extract a frame from an exact manifest render used by export."""
    validate_manifest(manifest)
    duration = manifest["output_spec"]["duration_frames"]
    if type(frame) is not int or not 0 <= frame < duration:
        raise ValueError("preview frame is outside the manifest duration")
    if manifest.get("manifest_version") == 4:
        manifest = _preview_snapshot_v4(manifest, frame)
        frame = 0
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="remix-preview-", dir=output_path.parent) as folder:
        rendered = render_manifest(manifest, media_paths=media_paths, output_path=Path(folder) / "exact-manifest.mp4")
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(rendered), "-vf", f"select=eq(n\\,{frame})", "-vsync", "0", "-frames:v", "1", str(output_path)],
            check=True,
        )
    return output_path
