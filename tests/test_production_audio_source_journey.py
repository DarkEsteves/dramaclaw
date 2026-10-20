"""A planned sound source survives material preparation, binding, and export."""

from __future__ import annotations

from copy import deepcopy
from array import array
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess

import pytest

from novelvideo.freezone.remix.material_work import material_workflow_intent, prepare_material_work
from novelvideo.freezone.remix.production_commands import apply_production_command
from novelvideo.freezone.remix.production_plan_v2 import compile_production_plan_v2
from novelvideo.freezone.remix.render import render_manifest
from novelvideo.freezone.remix.store import RemixArtifactStore


FIXTURE = Path(__file__).parent / "fixtures" / "production-audio-source-plan.json"


def _draft() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _sound_plan() -> dict:
    plan = _draft()
    plan["placements"].extend([
        {"id": "cover", "kind": "visual", "role": "overlay", "track_id": "overlay",
         "description": "展示手部操作", "source_audio": False, "z_order": 1,
         "start": {"kind": "program_frame", "frame": 10},
         "end": {"kind": "program_frame", "frame": 35}},
        {"id": "music", "kind": "audio", "role": "bgm", "track_id": "music",
         "description": "轻声配乐", "volume": 0.15,
         "start": {"kind": "beat", "ref_id": "demo", "edge": "start"},
         "end": {"kind": "beat", "ref_id": "demo", "edge": "end"}},
    ])
    return plan


def _work(store: RemixArtifactStore, plan_ref: dict) -> dict:
    requirements = [
        {"id": "conversation", "placement_ids": ["demo-video"], "turn_ids": ["ask", "answer"],
         "description": "连续问答", "media_kind": "video", "final_step_id": "talk"},
        {"id": "hands", "placement_ids": ["cover"], "description": "手部操作",
         "media_kind": "video", "final_step_id": "cover"},
        {"id": "score", "placement_ids": ["music"], "description": "配乐",
         "media_kind": "audio", "final_step_id": "score"},
    ]
    steps = [
        {"id": "talk", "label": "问答视频", "media_kind": "video", "source": "generate",
         "recipe_id": "general-video", "prompt": "两人连续问答", "dialogue_turn_ids": ["ask", "answer"]},
        {"id": "cover", "label": "手部视频", "media_kind": "video", "source": "generate",
         "recipe_id": "general-video", "prompt": "手部操作"},
        {"id": "score", "label": "配乐", "media_kind": "audio", "source": "generate",
         "recipe_id": "general-audio", "prompt": "轻声配乐"},
    ]
    result = prepare_material_work(store, plan_ref, requirements, steps, "audio-journey-work")
    return store.read(result["work_ref"]["artifact_id"], result["work_ref"]["revision"])["payload"]


def _bind(store: RemixArtifactStore, ref: dict, placement: str, media: Path, kind: str) -> dict:
    resource = {
        "id": placement + "-asset", "node_id": placement + "-node", "media_kind": kind,
        "media_path": media.relative_to(store.project_dir).as_posix(),
        "media_hash": hashlib.sha256(media.read_bytes()).hexdigest(),
        "duration_us": 2_000_000, "has_audio": True,
    }
    result = apply_production_command(store, {
        "command_id": "bind-" + placement, "plan_ref": ref, "kind": "bind_media",
        "target_id": placement, "payload": {"resource_id": resource["id"], "resource": resource},
    })
    return result["plan_ref"]


def _video(path: Path, color: str, frequency: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=64x64:d=2:r=25",
        "-f", "lavfi", "-i", f"sine=frequency={frequency}:duration=2:sample_rate=48000",
        "-shortest", "-c:v", "mpeg4", "-c:a", "aac", str(path),
    ], check=True)


def _tone_level(path: Path, at_second: float, frequency: int) -> float:
    raw = subprocess.run([
        "ffmpeg", "-v", "error", "-ss", str(at_second), "-i", str(path),
        "-t", "0.2", "-vn", "-ac", "1", "-ar", "48000", "-f", "f32le", "pipe:1",
    ], check=True, capture_output=True).stdout
    samples = array("f")
    samples.frombytes(raw)
    return 2 * abs(sum(value * complex(
        math.cos(2 * math.pi * frequency * i / 48000),
        math.sin(2 * math.pi * frequency * i / 48000),
    ) for i, value in enumerate(samples))) / len(samples)


@pytest.mark.asyncio
async def test_draft_accepts_shared_video_speech_source_with_one_model_request(monkeypatch) -> None:
    from novelvideo.freezone.remix import production_plan

    calls = []

    async def generate(**kwargs):
        calls.append(kwargs)
        return FIXTURE.read_text(encoding="utf-8")

    monkeypatch.setattr(production_plan, "generate_recipe_text", generate)
    draft = await production_plan.generate_production_plan_draft(
        topic="两人演示一件物品", blueprint=None, username="alice")
    assert len(calls) == 1
    assert [turn["speech_placement_id"] for turn in draft["beats"][0]["turns"]] == ["demo-video", "demo-video"]
    assert draft["placements"][0]["source_audio"] is True
    assert not any(placement["kind"] == "audio" for placement in draft["placements"])


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")
def test_planned_video_owns_both_turns_with_one_material_and_one_take(tmp_path: Path) -> None:
    store = RemixArtifactStore(tmp_path / "project", project_id="project", created_by="alice")
    saved = store.create("production_plan.v2", _sound_plan(), [])
    ref = {"artifact_id": saved["id"], "revision": 1}
    work = _work(store, ref)
    intent = material_workflow_intent(work, store=store)
    assert len(work["steps"]) == 3
    assert len([step for step in work["steps"] if step["media_kind"] == "audio"]) == 1
    assert next(item for item in intent["items"] if item["id"] == "talk")["requires_generated_audio"] is True
    cover_intent = material_workflow_intent(work, store=store, selected_step_ids=["cover"])
    assert cover_intent["items"][0]["id"] == "cover"
    assert "requires_generated_audio" not in cover_intent["items"][0]

    media = store.project_dir / "talk.mp4"
    _video(media, "blue", 440)
    first = _bind(store, ref, "demo-video", media, "video")
    current = store.read_latest(saved["id"])["payload"]
    assert first["revision"] == 2
    assert [take["turn_ids"] for take in current["speech_takes"]] == [["ask", "answer"]]
    assert [cue["turn_id"] for cue in current["speech_cues"]] == ["ask", "answer"]
    assert {placement["resource_id"] for placement in current["placements"] if placement["id"] == "demo-video"} == {"demo-video-asset"}


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")
def test_export_keeps_one_speech_source_under_visual_cover_and_optional_cover_sound(tmp_path: Path) -> None:
    store = RemixArtifactStore(tmp_path / "project", project_id="project", created_by="alice")
    saved = store.create("production_plan.v2", _sound_plan(), [])
    ref = {"artifact_id": saved["id"], "revision": 1}
    _work(store, ref)
    store.project_dir.mkdir(parents=True, exist_ok=True)
    for name, color, frequency in (("demo-video", "blue", 440), ("cover", "red", 880)):
        _video(store.project_dir / f"{name}.mp4", color, frequency)
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=220:duration=2:sample_rate=48000",
        str(store.project_dir / "music.wav"),
    ], check=True)
    media = {"demo-video": store.project_dir / "demo-video.mp4",
             "cover": store.project_dir / "cover.mp4", "music": store.project_dir / "music.wav"}
    for placement, kind in (("demo-video", "video"), ("cover", "video"), ("music", "audio")):
        ref = _bind(store, ref, placement, media[placement], kind)
    stored = store.read_latest(saved["id"])
    plan = deepcopy(stored["payload"])
    assert len(plan["speech_takes"]) == 1
    assert len(plan["speech_cues"]) == 2
    source_ref = {"artifact_id": saved["id"], "revision": stored["revision"]}
    evidence = store.create("speech_cue_evidence.v1", {
        "take_id": plan["speech_takes"][0]["id"],
        "media_hash": next(row for row in plan["resources"] if row["id"] == "demo-video-asset")["media_hash"],
        "basis": "manual", "source_plan_ref": source_ref,
        "cues": [
            {"id": cue["id"], "turn_id": cue["turn_id"], "text": cue["text"],
             "start_us": start, "end_us": end}
            for cue, start, end in zip(plan["speech_cues"], (100_000, 1_100_000), (900_000, 1_900_000))
        ],
    }, [source_ref])
    evidence_ref = {"artifact_id": evidence["id"], "revision": evidence["revision"]}
    for cue, start, end in zip(plan["speech_cues"], (100_000, 1_100_000), (900_000, 1_900_000)):
        cue.update(timing_status="ready", timing_source="manual",
                   source_start_us=start, source_end_us=end, evidence_ref=evidence_ref)
    accepted = store.revise(saved["id"], stored["revision"], plan, [*stored["upstream_refs"], evidence_ref])
    plan = accepted["payload"]
    manifest = compile_production_plan_v2(plan)
    assert [event["media_id"] for event in manifest["audio_events"]].count("demo-video-asset") == 1
    assert [event["media_id"] for event in manifest["audio_events"]].count("music-asset") == 1
    assert not any(event["media_id"] == "cover-asset" for event in manifest["audio_events"])
    paths = {placement + "-asset": path for placement, path in media.items()}
    output = render_manifest(manifest, media_paths=paths, output_path=tmp_path / "silent-cover.mp4")
    assert output.is_file()
    source_voice = _tone_level(media["demo-video"], 0.75, 440)
    # This mix has two audio inputs (speech and score); a copied speech stream
    # would materially increase the 440 Hz contribution even after amix normalization.
    normal_voice_level = _tone_level(output, 0.75, 440)
    assert 0.3 < normal_voice_level / source_voice < 0.65
    duplicated_manifest = deepcopy(manifest)
    extra_voice = deepcopy(next(row for row in manifest["audio_events"]
                                if row["media_id"] == "demo-video-asset"))
    extra_voice["placement_id"] += ":duplicate-control"
    if "row_id" in extra_voice:
        extra_voice["row_id"] += ":duplicate-control"
    duplicated_manifest["audio_events"].append(extra_voice)
    duplicated_output = render_manifest(duplicated_manifest, media_paths=paths,
                                        output_path=tmp_path / "duplicated-voice-control.mp4")
    assert normal_voice_level < 0.85 * _tone_level(duplicated_output, 0.75, 440)
    assert min(_tone_level(output, point, 440) for point in (0.2, 0.75, 1.7)) > source_voice / 5
    assert _tone_level(output, 0.75, 220) > 0.005

    audible = deepcopy(plan)
    next(row for row in audible["placements"] if row["id"] == "cover")["source_audio"] = True
    audible_manifest = compile_production_plan_v2(audible)
    assert [event["media_id"] for event in audible_manifest["audio_events"]].count("demo-video-asset") == 1
    assert [event["media_id"] for event in audible_manifest["audio_events"]].count("cover-asset") == 1
    audible_output = render_manifest(audible_manifest, media_paths=paths, output_path=tmp_path / "audible-cover.mp4")
    assert audible_output.is_file()
    # Adding the cover sound makes three inputs; source voice is still one.
    assert 0.2 < _tone_level(audible_output, 0.75, 440) / source_voice < 0.48
    assert min(_tone_level(audible_output, point, 440) for point in (0.2, 0.75, 1.7)) > source_voice / 5
    assert _tone_level(audible_output, 0.75, 880) > 6 * _tone_level(output, 0.75, 880)
