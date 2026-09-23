"""Real media checks for the versioned remix composition manifest."""

from __future__ import annotations

import json
import hashlib
import math
import shutil
import subprocess
from array import array
from copy import deepcopy
from pathlib import Path

import pytest

from novelvideo.freezone.remix.render import (
    _ass_document,
    build_manifest,
    manifest_renderer,
    render_manifest,
    render_preview_frame,
    validate_manifest,
)
from novelvideo.freezone.remix.contracts import validate_payload
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


def _hd_subtitle_pixels(path: Path, seconds: float) -> bytes:
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(seconds), "-i", str(path),
         "-frames:v", "1", "-vf", "crop=900:300:90:1440", "-pix_fmt", "rgb24",
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


def test_v2_manifest_requires_vertical_hd_safe_area_and_continuous_narration() -> None:
    manifest = _manifest()
    manifest["manifest_version"] = 2
    manifest["media_bindings"][0]["purpose"] = "background"
    manifest["output_spec"].update({
        "width": 1080,
        "height": 1920,
        "subtitle_safe_area": {"left": 90, "right": 90, "top": 120, "bottom": 180},
    })
    validate_manifest(manifest)

    broken = json.loads(json.dumps(manifest))
    broken["audio_events"][0]["start_frame"] = 1
    with pytest.raises(ValueError, match="continuous narration"):
        validate_manifest(broken)

    broken = json.loads(json.dumps(manifest))
    broken["output_spec"]["width"] = 720
    broken["output_spec"]["height"] = 1280
    with pytest.raises(ValueError, match="1080x1920"):
        validate_manifest(broken)

    broken = json.loads(json.dumps(manifest))
    broken["output_spec"]["duration_frames"] = 30 * 60 + 1
    broken["layers"][0]["end_frame"] = 30 * 60 + 1
    broken["audio_events"][0]["end_frame"] = 30 * 60 + 1
    with pytest.raises(ValueError, match="MVP limit"):
        validate_manifest(broken)


def test_v2_accepts_contiguous_generated_primary_shots_without_uploaded_background() -> None:
    manifest = _manifest()
    manifest["manifest_version"] = 2
    manifest["output_spec"].update({
        "width": 1080, "height": 1920,
        "subtitle_safe_area": {"left": 90, "right": 90, "top": 120, "bottom": 180},
    })
    manifest["media_bindings"][0]["purpose"] = "a-roll"
    manifest["media_bindings"][1]["purpose"] = "a-roll"
    manifest["layers"] = [
        {"media_id": "background", "start_frame": 0, "end_frame": 30, "z_order": 0, "fit": "cover"},
        {"media_id": "product", "start_frame": 30, "end_frame": 60, "z_order": 0, "fit": "cover"},
    ]
    validate_manifest(manifest)

    manifest["layers"][1]["start_frame"] = 31
    with pytest.raises(ValueError, match="continuous primary visual coverage"):
        validate_manifest(manifest)


def test_v3_accepts_sequential_speaking_videos_and_rejects_silent_speech() -> None:
    manifest = {
        "manifest_version": 3,
        "output_spec": {"width": 1080, "height": 1920, "fps": 30, "duration_frames": 60,
                        "subtitle_safe_area": {"left": 90, "right": 90, "top": 120, "bottom": 180}},
        "media_bindings": [
            {"media_id": "first", "kind": "video", "purpose": "a-roll", "has_audio": True, "media_hash": "sha256:" + "a" * 64},
            {"media_id": "second", "kind": "video", "purpose": "a-roll", "has_audio": True, "media_hash": "sha256:" + "b" * 64},
        ],
        "layers": [
            {"media_id": "first", "start_frame": 0, "end_frame": 30, "z_order": 0, "fit": "cover"},
            {"media_id": "second", "start_frame": 30, "end_frame": 60, "z_order": 0, "fit": "cover"},
        ],
        "audio_events": [
            {"media_id": "first", "role": "speech", "start_frame": 0, "end_frame": 30, "volume": 1},
            {"media_id": "second", "role": "speech", "start_frame": 30, "end_frame": 60, "volume": 1},
        ],
        "subtitle_events": [], "manual_overrides": [], "source_versions": [],
    }
    assert validate_manifest(manifest) == manifest
    assert manifest_renderer(manifest) == "layered_v3"
    silent = deepcopy(manifest)
    silent["media_bindings"][1]["has_audio"] = False
    with pytest.raises(ValueError, match="audio stream"):
        validate_manifest(silent)
    drift = deepcopy(manifest)
    drift["audio_events"][1]["start_frame"] = 31
    with pytest.raises(ValueError, match="speech and visual spans"):
        validate_manifest(drift)


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")
def test_v3_renders_each_speaking_videos_own_sound(tmp_path: Path) -> None:
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    for color, frequency, target in (("blue", 440, first), ("red", 880, second)):
        _ffmpeg("-f", "lavfi", "-i", f"color=c={color}:s=180x320:r=30:d=1",
                "-f", "lavfi", "-i", f"sine=frequency={frequency}:duration=1",
                "-shortest", "-c:v", "libx264", "-c:a", "aac", str(target))
    manifest = {
        "manifest_version": 3,
        "output_spec": {"width": 1080, "height": 1920, "fps": 30, "duration_frames": 60,
                        "subtitle_safe_area": {"left": 90, "right": 90, "top": 120, "bottom": 180}},
        "media_bindings": [
            {"media_id": "first", "kind": "video", "purpose": "a-roll", "has_audio": True, "media_hash": "sha256:" + "0" * 64},
            {"media_id": "second", "kind": "video", "purpose": "a-roll", "has_audio": True, "media_hash": "sha256:" + "0" * 64},
        ],
        "layers": [
            {"media_id": "first", "start_frame": 0, "end_frame": 30, "z_order": 0, "fit": "cover"},
            {"media_id": "second", "start_frame": 30, "end_frame": 60, "z_order": 0, "fit": "cover"},
        ],
        "audio_events": [
            {"media_id": "first", "role": "speech", "start_frame": 0, "end_frame": 30, "volume": 1},
            {"media_id": "second", "role": "speech", "start_frame": 30, "end_frame": 60, "volume": 1},
        ], "subtitle_events": [], "manual_overrides": [], "source_versions": [],
    }
    output = render_manifest(_bind_media_hashes(manifest, {"first": first, "second": second}),
                             media_paths={"first": first, "second": second}, output_path=tmp_path / "out.mp4")
    assert _pixel(output, 0.25, x=540, y=960)[2] > 100
    assert _pixel(output, 1.25, x=540, y=960)[0] > 100
    counts = []
    for start in (0.2, 1.2):
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(start), "-t", "0.2", "-i", str(output),
                              "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "pipe:1"],
                             check=True, capture_output=True).stdout
        samples = array("h")
        samples.frombytes(raw)
        counts.append(sum(left <= 0 < right for left, right in zip(samples, samples[1:])))
    assert counts[0] > 50
    assert counts[1] > counts[0] * 1.7


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="FFmpeg required",
)
def test_v2_renders_generated_primary_shots_without_uploaded_background(tmp_path: Path) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    voice = tmp_path / "voice.wav"
    _ffmpeg("-f", "lavfi", "-i", "color=c=blue:s=180x320", "-frames:v", "1", str(first))
    _ffmpeg("-f", "lavfi", "-i", "color=c=red:s=180x320", "-frames:v", "1", str(second))
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(voice))
    paths = {"first": first, "second": second, "voice": voice}
    manifest = {
        "manifest_version": 2,
        "output_spec": {"width": 1080, "height": 1920, "fps": 30, "duration_frames": 60,
                        "subtitle_safe_area": {"left": 90, "right": 90, "top": 120, "bottom": 180}},
        "media_bindings": [
            {"media_id": "first", "kind": "image", "purpose": "a-roll", "media_hash": "sha256:" + "0" * 64},
            {"media_id": "second", "kind": "image", "purpose": "a-roll", "media_hash": "sha256:" + "0" * 64},
            {"media_id": "voice", "kind": "audio", "media_hash": "sha256:" + "0" * 64},
        ],
        "layers": [
            {"media_id": "first", "start_frame": 0, "end_frame": 30, "z_order": 0, "fit": "cover"},
            {"media_id": "second", "start_frame": 30, "end_frame": 60, "z_order": 0, "fit": "cover"},
        ],
        "subtitle_events": [],
        "audio_events": [{"media_id": "voice", "role": "narration", "start_frame": 0, "end_frame": 60, "volume": 1}],
        "manual_overrides": [], "source_versions": [],
    }
    output = render_manifest(
        _bind_media_hashes(manifest, paths), media_paths=paths,
        output_path=tmp_path / "out.mp4",
    )
    assert _pixel(output, 0.25, x=540, y=960)[2] > 100
    assert _pixel(output, 1.25, x=540, y=960)[0] > 100


def test_manifest_versions_route_without_removing_the_legacy_composer() -> None:
    assert manifest_renderer(_manifest()) == "legacy_v1"
    current = _manifest()
    current["manifest_version"] = 2
    assert manifest_renderer(current) == "layered_v2"
    with pytest.raises(ValueError, match="manifest_version"):
        validate_payload("composition_manifest.v1", {**_manifest(), "manifest_version": None})


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_v1_ignores_v2_extension_fields_and_keeps_legacy_rendering(tmp_path: Path) -> None:
    background = tmp_path / "background.png"
    product = tmp_path / "product.mp4"
    voice = tmp_path / "voice.wav"
    _ffmpeg("-f", "lavfi", "-i", "color=c=blue:s=180x320", "-frames:v", "1", str(background))
    _ffmpeg("-f", "lavfi", "-i", "color=c=red:s=180x320:r=30:d=1", "-an", str(product))
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(voice))
    manifest = _manifest()
    manifest["output_spec"]["subtitle_safe_area"] = {
        "left": -1, "right": -1, "top": -1, "bottom": -1,
    }
    manifest["layers"][1].update({
        "crop": {"x": -1, "y": -1, "width": -1, "height": -1},
        "fade_in_frames": -1,
        "fade_out_frames": 999,
        "zoom": {"start": 0, "end": 99},
    })
    manifest["audio_events"][0]["duck_under_narration"] = 99
    paths = {"background": background, "product": product, "voice": voice}

    validate_manifest(manifest)
    output = render_manifest(
        _bind_media_hashes(manifest, paths), media_paths=paths,
        output_path=tmp_path / "legacy.mp4",
    )

    assert _pixel(output, 0.25)[2] > _pixel(output, 0.25)[0] * 2
    assert _pixel(output, 0.75)[0] > _pixel(output, 0.75)[2] * 2
    assert _pixel(output, 1.75)[2] > _pixel(output, 1.75)[0] * 2


def test_v2_validates_crop_motion_fades_and_deterministic_bgm_ducking() -> None:
    manifest = _manifest()
    manifest["manifest_version"] = 2
    manifest["media_bindings"][0]["purpose"] = "background"
    manifest["output_spec"].update({
        "width": 1080, "height": 1920,
        "subtitle_safe_area": {"left": 90, "right": 90, "top": 120, "bottom": 180},
    })
    manifest["layers"][1].update({
        "crop": {"x": 0, "y": 0, "width": 160, "height": 280},
        "fade_in_frames": 4,
        "fade_out_frames": 4,
        "zoom": {"start": 1.0, "end": 1.05},
    })
    manifest["media_bindings"].append({"media_id": "bgm", "kind": "audio", "loop": True, "media_hash": "sha256:" + "0" * 64})
    manifest["audio_events"].append({
        "media_id": "bgm", "role": "bgm", "start_frame": 0, "end_frame": 60,
        "volume": 0.3, "duck_under_narration": 0.35,
    })
    validate_manifest(manifest)

    manifest["audio_events"][-1]["duck_under_narration"] = 1.2
    with pytest.raises(ValueError, match="duck"):
        validate_manifest(manifest)


def test_ass_escapes_markup_and_honours_safe_area() -> None:
    text = '她说, "你好" {产品}\\下一行\r\n收尾\r完成'
    document = _ass_document(
        [{"text": text, "start_frame": 0, "end_frame": 30}],
        width=1080, height=1920, fps=30,
        safe_area={"left": 90, "right": 90, "top": 120, "bottom": 180},
    )
    assert "Alignment, MarginL, MarginR, MarginV" in document
    assert "90,90,180,1" in document
    assert r'她说, "你好" \{产品\}\\下一行\N收尾\N完成' in document
    assert "\r" not in document


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="FFmpeg required",
)
def test_v2_fixed_media_gate_renders_vertical_hd_broll_voice_and_subtitles(tmp_path: Path) -> None:
    media_dir = tmp_path / "带 空格,quotes'"
    media_dir.mkdir()
    background = media_dir / "背景.png"
    broll = media_dir / "B roll.mp4"
    voice = media_dir / "旁白.wav"
    bgm = media_dir / "音乐.wav"
    _ffmpeg("-f", "lavfi", "-i", "color=c=blue:s=1080x1920", "-frames:v", "1", str(background))
    _ffmpeg("-f", "lavfi", "-i", "color=c=red:s=540x960:r=30:d=2", "-an", str(broll))
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=15", str(voice))
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=220:duration=1", str(bgm))
    paths = {"background": background, "broll": broll, "voice": voice, "bgm": bgm}
    manifest = {
        "manifest_version": 2,
        "output_spec": {
            "width": 1080, "height": 1920, "fps": 30, "duration_frames": 450,
            "subtitle_safe_area": {"left": 90, "right": 90, "top": 120, "bottom": 180},
        },
        "media_bindings": [
            {"media_id": "background", "kind": "image", "purpose": "background", "media_hash": "sha256:" + "0" * 64},
            {"media_id": "broll", "kind": "video", "media_hash": "sha256:" + "0" * 64},
            {"media_id": "voice", "kind": "audio", "media_hash": "sha256:" + "0" * 64},
            {"media_id": "bgm", "kind": "audio", "loop": True, "media_hash": "sha256:" + "0" * 64},
        ],
        "layers": [
            {"media_id": "background", "start_frame": 0, "end_frame": 450, "z_order": 0, "fit": "cover"},
            {"media_id": "broll", "start_frame": 30, "end_frame": 90, "z_order": 1, "fit": "cover", "fade_in_frames": 4, "fade_out_frames": 4, "zoom": {"start": 1, "end": 1.03}},
        ],
        "subtitle_events": [{"text": '中文, "caption" {safe}', "start_frame": 150, "end_frame": 300}],
        "audio_events": [
            {"media_id": "voice", "role": "narration", "start_frame": 0, "end_frame": 450, "volume": 1},
            {"media_id": "bgm", "role": "bgm", "start_frame": 0, "end_frame": 450, "volume": 0.3, "duck_under_narration": 0.35},
        ],
        "manual_overrides": [], "source_versions": [],
    }
    output = render_manifest(
        _bind_media_hashes(manifest, paths), media_paths=paths,
        output_path=media_dir / "render output.mp4",
    )
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)],
        check=True, capture_output=True, text=True,
    )
    streams = json.loads(probe.stdout)["streams"]
    video = next(stream for stream in streams if stream["codec_type"] == "video")
    assert (video["width"], video["height"], video["r_frame_rate"]) == (1080, 1920, "30/1")
    assert any(stream["codec_type"] == "audio" for stream in streams)
    assert _pixel(output, 0.5, x=540, y=960)[2] > 100
    assert _pixel(output, 2.0, x=540, y=960)[0] > 100
    assert _pixel(output, 12.0, x=540, y=960)[2] > 100
    assert _hd_subtitle_pixels(output, 6.0) != _hd_subtitle_pixels(output, 12.0)


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


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")
def test_render_manifest_layers_still_image_and_keeps_voice(tmp_path: Path) -> None:
    background = tmp_path / "background.png"
    product = tmp_path / "product.png"
    voice = tmp_path / "voice.wav"
    _ffmpeg("-f", "lavfi", "-i", "color=c=blue:s=180x320", "-frames:v", "1", str(background))
    _ffmpeg("-f", "lavfi", "-i", "color=c=red:s=180x320", "-frames:v", "1", str(product))
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(voice))
    manifest = _manifest()
    manifest["media_bindings"][1]["kind"] = "image"
    paths = {"background": background, "product": product, "voice": voice}
    output = render_manifest(_bind_media_hashes(manifest, paths), media_paths=paths, output_path=tmp_path / "still.mp4")
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)], capture_output=True, text=True, check=True)
    assert any(stream["codec_type"] == "audio" for stream in json.loads(probe.stdout)["streams"])
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


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_preview_frames_match_exact_manifest_before_inside_and_after_event_boundaries(tmp_path: Path) -> None:
    background = tmp_path / "background.png"
    product = tmp_path / "product.mp4"
    voice = tmp_path / "voice.wav"
    _ffmpeg("-f", "lavfi", "-i", "color=c=blue:s=180x320", "-frames:v", "1", str(background))
    _ffmpeg("-f", "lavfi", "-i", "color=c=red:s=180x320:r=30:d=1", "-an", str(product))
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(voice))
    paths = {"background": background, "product": product, "voice": voice}
    manifest = _bind_media_hashes(_manifest(), paths)
    manifest["layers"][1].update({"x": 0, "y": 0, "width": 180, "height": 160})
    export = render_manifest(manifest, media_paths=paths, output_path=tmp_path / "export.mp4")
    for frame in (14, 15, 44, 45):
        preview = render_preview_frame(manifest, media_paths=paths, frame=frame, output_path=tmp_path / f"preview-{frame}.png")
        exported_frame = tmp_path / f"exported-{frame}.png"
        _ffmpeg("-i", str(export), "-vf", f"select=eq(n\\,{frame})", "-vsync", "0", "-frames:v", "1", str(exported_frame))
        assert hashlib.sha256(preview.read_bytes()).digest() == hashlib.sha256(exported_frame.read_bytes()).digest()
    assert _pixel(export, 14 / 30)[2] > _pixel(export, 14 / 30)[0] * 2
    assert _pixel(export, 15 / 30)[0] > _pixel(export, 15 / 30)[2] * 2
    assert _pixel(export, 44 / 30)[0] > _pixel(export, 44 / 30)[2] * 2
    assert _pixel(export, 45 / 30)[2] > _pixel(export, 45 / 30)[0] * 2
    assert _subtitle_pixels(export, 15 / 30) != _subtitle_pixels(export, 14 / 30)
    assert _subtitle_pixels(export, 44 / 30) != _subtitle_pixels(export, 45 / 30)


def _audio_rms(path: Path) -> float:
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", "8000", "-f", "f32le", "pipe:1"],
        check=True, capture_output=True,
    )
    samples = array("f")
    samples.frombytes(result.stdout)
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples))


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_exact_manifest_narration_role_and_volume_are_measurable_in_export(tmp_path: Path) -> None:
    background = tmp_path / "background.png"
    voice = tmp_path / "voice.wav"
    _ffmpeg("-f", "lavfi", "-i", "color=c=blue:s=180x320", "-frames:v", "1", str(background))
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(voice))
    manifest = _manifest()
    manifest["media_bindings"] = [manifest["media_bindings"][0], manifest["media_bindings"][2]]
    manifest["layers"] = [manifest["layers"][0]]
    manifest["subtitle_events"] = []
    paths = {"background": background, "voice": voice}
    _bind_media_hashes(manifest, paths)
    loud = render_manifest(manifest, media_paths=paths, output_path=tmp_path / "loud.mp4")
    quiet_manifest = deepcopy(manifest)
    quiet_manifest["audio_events"][0].update({"role": "narration", "volume": 0.2})
    quiet = render_manifest(quiet_manifest, media_paths=paths, output_path=tmp_path / "quiet.mp4")

    ratio = _audio_rms(quiet) / _audio_rms(loud)
    assert 0.15 < ratio < 0.25
