"""Real media checks for the versioned remix composition manifest."""

from __future__ import annotations

import json
import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from novelvideo.freezone.remix.render import (
    _ass_document,
    build_manifest,
    render_manifest,
    validate_manifest,
)
from novelvideo.freezone.remix.timing import compile_timing


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def _pixel(
    path: Path, seconds: float, *, x: int = 90, y: int = 100
) -> tuple[int, int, int]:
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            str(seconds),
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            f"crop=2:2:{x}:{y}",
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    )
    return tuple(result.stdout[:3])


def _subtitle_pixels(path: Path, seconds: float) -> bytes:
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(seconds), "-i", str(path),
         "-frames:v", "1", "-vf", "crop=180:120:0:200", "-pix_fmt", "rgb24",
         "-f", "rawvideo", "pipe:1"],
        capture_output=True, check=True,
    )
    return result.stdout


def _manifest() -> dict:
    return {
        "output_spec": {"width": 180, "height": 320, "fps": 30, "duration_frames": 60},
        "media_bindings": [
            {"media_id": "background", "kind": "image", "media_hash": "sha256:" + "0" * 64},
            {"media_id": "product", "kind": "video", "media_hash": "sha256:" + "0" * 64},
            {"media_id": "voice", "kind": "audio", "media_hash": "sha256:" + "0" * 64},
        ],
        "layers": [
            {"media_id": "background", "start_frame": 0, "end_frame": 60, "z_order": 0},
            {"media_id": "product", "start_frame": 15, "end_frame": 45, "z_order": 1},
        ],
        "subtitle_events": [{"text": "Product", "start_frame": 15, "end_frame": 45}],
        "audio_events": [
            {
                "media_id": "voice",
                "role": "narration",
                "start_frame": 0,
                "end_frame": 60,
                "volume": 1.0,
            },
        ],
        "manual_overrides": [],
        "source_versions": [],
    }


def _bind_media_hashes(manifest: dict, paths: dict[str, Path]) -> dict:
    for binding in manifest["media_bindings"]:
        binding["media_hash"] = "sha256:" + hashlib.sha256(paths[binding["media_id"]].read_bytes()).hexdigest()
    return manifest


def test_manifest_rejects_unresolved_media_and_out_of_range_events() -> None:
    manifest = _manifest()
    manifest["layers"][1]["media_id"] = "missing"
    with pytest.raises(ValueError, match="missing"):
        validate_manifest(manifest)

    manifest = _manifest()
    manifest["layers"][1]["end_frame"] = 61
    with pytest.raises(ValueError, match="duration"):
        validate_manifest(manifest)


def test_manifest_requires_a_narration_track() -> None:
    manifest = _manifest()
    manifest["audio_events"] = []
    with pytest.raises(ValueError, match="narration"):
        validate_manifest(manifest)


def test_word_cues_render_one_active_word_at_a_time() -> None:
    event = {"text": "go home", "start_frame": 0, "end_frame": 30, "word_cues": [
        {"start_char": 0, "end_char": 2, "start_frame": 0, "end_frame": 10},
        {"start_char": 3, "end_char": 7, "start_frame": 15, "end_frame": 30},
    ]}
    manifest = _manifest()
    manifest["subtitle_events"] = [event]
    validate_manifest(manifest)

    lines = [line for line in _ass_document([event], width=180, height=320, fps=30).splitlines() if line.startswith("Dialogue:")]
    assert len(lines) == 3
    assert "{\\c&H00CFBD00&\\b1}go{\\r} home" in lines[0]
    assert lines[1].endswith("go home")
    assert "go {\\c&H00CFBD00&\\b1}home{\\r}" in lines[2]


def test_manifest_rejects_word_cues_outside_subtitle_text_or_time() -> None:
    manifest = _manifest()
    manifest["subtitle_events"][0]["word_cues"] = [
        {"start_char": 0, "end_char": 8, "start_frame": 15, "end_frame": 20}
    ]
    with pytest.raises(ValueError, match="word cue"):
        validate_manifest(manifest)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_rendered_subtitle_highlight_changes_with_the_spoken_word(tmp_path: Path) -> None:
    background = tmp_path / "background.png"
    voice = tmp_path / "voice.wav"
    _ffmpeg("-f", "lavfi", "-i", "color=c=black:s=180x320", "-frames:v", "1", str(background))
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(voice))
    manifest = _manifest()
    manifest["media_bindings"] = [row for row in manifest["media_bindings"] if row["media_id"] != "product"]
    manifest["layers"] = [manifest["layers"][0]]
    manifest["subtitle_events"] = [{"text": "go home", "start_frame": 0, "end_frame": 60, "word_cues": [
        {"start_char": 0, "end_char": 2, "start_frame": 0, "end_frame": 20},
        {"start_char": 3, "end_char": 7, "start_frame": 20, "end_frame": 40},
    ]}]
    paths = {"background": background, "voice": voice}
    output = render_manifest(_bind_media_hashes(manifest, paths), media_paths=paths, output_path=tmp_path / "out.mp4")

    assert _subtitle_pixels(output, 0.3) != _subtitle_pixels(output, 0.9)


def test_manifest_requires_exact_media_content_hash() -> None:
    manifest = _manifest()
    manifest["media_bindings"][0].pop("media_hash", None)
    with pytest.raises(ValueError, match="media_hash"):
        validate_manifest(manifest)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_render_rejects_media_changed_after_manifest_binding(tmp_path: Path) -> None:
    paths = {name: tmp_path / name for name in ("background", "product", "voice")}
    for path in paths.values():
        path.write_bytes(b"original")
    manifest = _bind_media_hashes(_manifest(), paths)
    paths["background"].write_bytes(b"replacement")

    with pytest.raises(ValueError, match="changed since manifest publication"):
        render_manifest(manifest, media_paths=paths, output_path=tmp_path / "out.mp4")


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_render_rejects_video_shorter_than_its_event(tmp_path: Path) -> None:
    background = tmp_path / "background.png"
    product = tmp_path / "short.mp4"
    voice = tmp_path / "voice.wav"
    _ffmpeg(
        "-f", "lavfi", "-i", "color=c=blue:s=180x320", "-frames:v", "1", str(background)
    )
    _ffmpeg(
        "-f", "lavfi", "-i", "color=c=red:s=180x320:r=30:d=0.5", "-an", str(product)
    )
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(voice))

    with pytest.raises(ValueError, match="shorter"):
        render_manifest(
            _bind_media_hashes(_manifest(), {"background": background, "product": product, "voice": voice}),
            media_paths={"background": background, "product": product, "voice": voice},
            output_path=tmp_path / "out.mp4",
        )


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_render_can_loop_reference_video_under_longer_narration(tmp_path: Path) -> None:
    reference = tmp_path / "reference.mp4"
    voice = tmp_path / "voice.wav"
    bgm = tmp_path / "bgm.wav"
    _ffmpeg("-f", "lavfi", "-i", "color=c=blue:s=180x320:r=30:d=1", "-an", str(reference))
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(voice))
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=220:duration=0.5", str(bgm))
    manifest = {
        "output_spec": {"width": 180, "height": 320, "fps": 30, "duration_frames": 60},
        "media_bindings": [
            {"media_id": "reference", "kind": "video", "loop": True, "media_hash": "sha256:" + hashlib.sha256(reference.read_bytes()).hexdigest()},
            {"media_id": "voice", "kind": "audio", "media_hash": "sha256:" + hashlib.sha256(voice.read_bytes()).hexdigest()},
            {"media_id": "bgm", "kind": "audio", "loop": True, "media_hash": "sha256:" + hashlib.sha256(bgm.read_bytes()).hexdigest()},
        ],
        "layers": [{"media_id": "reference", "start_frame": 0, "end_frame": 60, "z_order": 0}],
        "subtitle_events": [],
        "audio_events": [{"media_id": "voice", "role": "narration", "start_frame": 0, "end_frame": 60}, {"media_id": "bgm", "role": "bgm", "start_frame": 0, "end_frame": 60, "volume": 0.3}],
        "manual_overrides": [], "source_versions": [],
    }
    output = render_manifest(manifest, media_paths={"reference": reference, "voice": voice, "bgm": bgm}, output_path=tmp_path / "out.mp4")
    r, g, b = _pixel(output, 1.5)
    assert b > r + 50 and b > g + 50


def test_build_manifest_binds_timing_events_to_media_and_caption() -> None:
    timing = {
        "fps": 30,
        "events": [{"event_id": "product", "start_frame": 15, "end_frame": 45}],
        "unresolved_required_events": [],
        "override_conflicts": [],
    }
    bindings = {
        "background": {"kind": "image", "media_hash": "sha256:" + "0" * 64},
        "product": {"kind": "video", "media_hash": "sha256:" + "0" * 64},
        "voice": {"kind": "audio", "media_hash": "sha256:" + "0" * 64},
    }
    style = {
        "output_spec": {"width": 180, "height": 320, "fps": 30, "duration_frames": 60},
        "background_media_id": "background",
        "narration_media_id": "voice",
        "event_rules": {
            "product": {"media_id": "product", "caption": "Product", "z_order": 1}
        },
    }

    result = build_manifest(timing, bindings, style)

    assert result["layers"][1] == {
        "media_id": "product",
        "start_frame": 15,
        "end_frame": 45,
        "z_order": 1,
    }
    assert result["subtitle_events"] == [
        {"text": "Product", "start_frame": 15, "end_frame": 45}
    ]
    assert result["audio_events"][0]["role"] == "narration"


def test_build_manifest_blocks_unresolved_required_events() -> None:
    with pytest.raises(ValueError, match="unresolved required"):
        build_manifest(
            {
                "fps": 30,
                "events": [],
                "unresolved_required_events": [{"event_id": "product"}],
            },
            {},
            {
                "output_spec": {
                    "width": 180,
                    "height": 320,
                    "fps": 30,
                    "duration_frames": 60,
                }
            },
        )


def test_timing_compiler_output_builds_a_renderable_manifest() -> None:
    timing = compile_timing(
        script={
            "revision": 1,
            "media_hash": "sha256:voice",
            "text": "喝下咖啡",
            "selections": [
                {
                    "selection_id": "product",
                    "start_char": 2,
                    "end_char": 4,
                    "script_revision": 1,
                }
            ],
            "events": [
                {"event_id": "product", "selection_id": "product", "required": True}
            ],
        },
        words={
            "script_revision": 1,
            "media_hash": "sha256:voice",
            "words": [
                {
                    "text": "咖啡",
                    "start_char": 2,
                    "end_char": 4,
                    "start_us": 500_000,
                    "end_us": 1_500_000,
                }
            ],
        },
        edit_map=[
            {"source_start_us": 0, "source_end_us": 2_000_000, "timeline_start_us": 0}
        ],
        overrides=[],
        fps=30,
    )
    manifest = build_manifest(
        timing,
        {
            "background": {"kind": "image", "media_hash": "sha256:" + "0" * 64},
            "product": {"kind": "video", "media_hash": "sha256:" + "0" * 64},
            "voice": {"kind": "audio", "media_hash": "sha256:" + "0" * 64},
        },
        {
            "output_spec": {
                "width": 180,
                "height": 320,
                "fps": 30,
                "duration_frames": 60,
            },
            "background_media_id": "background",
            "narration_media_id": "voice",
            "event_rules": {
                "product": {"media_id": "product", "caption": "咖啡", "z_order": 1}
            },
        },
    )
    assert manifest["layers"][1]["start_frame"] == 15
    assert manifest["layers"][1]["end_frame"] == 45


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required"
)
def test_render_manifest_layers_video_and_keeps_voice(tmp_path: Path) -> None:
    background = tmp_path / "background.png"
    product = tmp_path / "product.mp4"
    voice = tmp_path / "voice.wav"
    output = tmp_path / "out.mp4"
    _ffmpeg(
        "-f", "lavfi", "-i", "color=c=blue:s=180x320", "-frames:v", "1", str(background)
    )
    _ffmpeg("-f", "lavfi", "-i", "color=c=red:s=180x320:r=30:d=1", "-an", str(product))
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(voice))

    render_manifest(
        _bind_media_hashes(_manifest(), {"background": background, "product": product, "voice": voice}),
        media_paths={"background": background, "product": product, "voice": voice},
        output_path=output,
    )

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)],
        capture_output=True,
        text=True,
        check=True,
    )
    streams = json.loads(probe.stdout)["streams"]
    video = next(stream for stream in streams if stream["codec_type"] == "video")
    assert (video["width"], video["height"], video["r_frame_rate"]) == (
        180,
        320,
        "30/1",
    )
    assert any(stream["codec_type"] == "audio" for stream in streams)
    assert _pixel(output, 0.25)[2] > _pixel(output, 0.25)[0] * 2
    assert _pixel(output, 0.75)[0] > _pixel(output, 0.75)[2] * 2
    assert _pixel(output, 1.75)[2] > _pixel(output, 1.75)[0] * 2


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_render_manifest_respects_position_size_and_opacity(tmp_path: Path) -> None:
    background = tmp_path / "background.png"
    product = tmp_path / "product.mp4"
    voice = tmp_path / "voice.wav"
    _ffmpeg(
        "-f", "lavfi", "-i", "color=c=blue:s=180x320", "-frames:v", "1", str(background)
    )
    _ffmpeg("-f", "lavfi", "-i", "color=c=red:s=180x320:r=30:d=1", "-an", str(product))
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(voice))
    manifest = _manifest()
    _bind_media_hashes(manifest, {"background": background, "product": product, "voice": voice})
    manifest["layers"][1].update(
        {"x": 45, "y": 80, "width": 90, "height": 160, "opacity": 0.5}
    )
    output = tmp_path / "positioned.mp4"

    render_manifest(
        manifest,
        media_paths={"background": background, "product": product, "voice": voice},
        output_path=output,
    )

    outside = _pixel(output, 0.75, x=10, y=10)
    inside = _pixel(output, 0.75, x=90, y=100)
    assert outside[2] > outside[0] * 2
    assert inside[0] > 45 and inside[2] > 45
