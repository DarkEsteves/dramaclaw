"""Native authoring commands produce an editable layered, wordless render."""

import hashlib
import json
import shutil
import subprocess

import pytest

from test_freezone_plugin import _load_plugin_module, _restore_tools_registry_modules as _restore_tools_registry_modules
from test_production_advancement import production as production, current, passed

from novelvideo.freezone.remix.production_commands import apply_production_command
from novelvideo.freezone.remix.production_plan_v2 import compile_production_plan_v2
from novelvideo.freezone.remix.render import render_manifest
from novelvideo.freezone.remix.store import RemixArtifactStore


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")
def test_agent_commands_compile_overlapping_images_without_speech_or_fixed_output(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    paths = {}
    resources = []
    for identity, color in [("base", "blue"), ("overlay", "red")]:
        path = assets / f"{identity}.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"color=c={color}:s=128x72", "-frames:v", "1", str(path)], check=True)
        paths[identity] = path
        resources.append({"id": identity, "node_id": identity + "-node", "media_kind": "image",
                          "media_path": f"assets/{identity}.png", "media_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
                          "duration_us": 0, "has_audio": False})
    plan = {"schema_version": 2, "goal": "无台词横屏复刻", "output": {
        "width": 128, "height": 72, "fps_num": 24, "fps_den": 1,
    }, "beats": [{"id": "scene", "purpose": "产品展示", "duration_frames": 48, "turns": []}],
        "resources": resources, "placements": [{"id": "main", "kind": "visual", "role": "primary", "track_id": "main",
            "start": {"kind": "beat", "ref_id": "scene", "edge": "start"},
            "end": {"kind": "beat", "ref_id": "scene", "edge": "end"}}], "provenance": {}}
    store = RemixArtifactStore(tmp_path, "owner")
    first = store.create("production_plan.v2", plan, [])
    second = store.create("production_plan.v2", plan, [])
    assert first["id"] != second["id"]
    command = {"command_id": "bind-base", "plan_ref": {"artifact_id": first["id"], "revision": 1},
               "kind": "bind_media", "target_id": "main", "payload": {"resource_id": "base"}}
    bound = apply_production_command(store, command)
    assert apply_production_command(store, command) == bound
    placed = apply_production_command(store, {
        "command_id": "add-overlay", "plan_ref": bound["plan_ref"], "kind": "upsert_placement", "target_id": "overlay-shot",
        "payload": {"placement": {"id": "overlay-shot", "kind": "visual", "role": "overlay", "track_id": "overlay",
            "resource_id": "overlay", "x": 0, "y": 0, "width": 64, "height": 72, "z_order": 1,
            "start": {"kind": "program_frame", "frame": 12}, "end": {"kind": "program_frame", "frame": 24}}},
    })
    current = store.read(first["id"], placed["plan_ref"]["revision"])
    assert current["payload"]["speech_takes"] == []
    assert store.read_latest(second["id"])["revision"] == 1
    manifest = compile_production_plan_v2(current["payload"])
    manifest["source_versions"] = [placed["plan_ref"]]
    saved_manifest = store.create("composition_manifest.v2", manifest, [placed["plan_ref"]])
    result = render_manifest(saved_manifest["payload"], media_paths=paths, output_path=tmp_path / "result.mp4")
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height,r_frame_rate,nb_frames", "-of", "json", str(result)],
                           check=True, capture_output=True, text=True)
    stream = json.loads(probe.stdout)["streams"][0]
    assert (stream["width"], stream["height"], stream["r_frame_rate"], int(stream["nb_frames"])) == (128, 72, "24/1", 48)
    def sample(time):
        return subprocess.run(["ffmpeg", "-v", "error", "-ss", str(time), "-i", str(result), "-frames:v", "1",
                               "-vf", "crop=1:1:10:10,format=rgb24", "-f", "rawvideo", "-"],
                              check=True, capture_output=True).stdout
    first_pixel, overlap_pixel = sample(0.1), sample(0.75)
    assert first_pixel[2] > 200 and first_pixel[0] < 40
    assert overlap_pixel[0] > 200 and overlap_pixel[2] < 40


def test_director_tools_resume_same_saved_work_after_real_user_review(production, monkeypatch):
    """The adapter runs native acceptance and reports its next action without node polling."""
    import asyncio
    from novelvideo.freezone.remix.production_advancement import advance_production
    ctx, store, commission, work, _ = production
    plugin = _load_plugin_module()
    monkeypatch.setenv('DRAMACLAW_PROJECT_ID', 'p')
    monkeypatch.setenv('DRAMACLAW_CANVAS_ID', 'c')
    monkeypatch.setenv('DRAMACLAW_AGENT_ID', 'main')
    monkeypatch.delenv('DRAMACLAW_AGENT_PROFILE', raising=False)
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path))
        if method == 'GET':
            return {'ok': True, 'data': current(production)}
        body = kwargs['body']
        result = asyncio.run(advance_production(ctx, canvas_id='c', **body))
        return {'ok': True, 'data': result}

    monkeypatch.setattr(plugin, '_request', request)
    tools = {name: handler for name, _, handler in plugin.TOOLS}
    assert 'freezone_get_production_progress' in tools
    assert 'freezone_advance_production' in tools
    initial = tools['freezone_get_production_progress']({'commission_id': commission['commission_ref']['artifact_id']})
    passed(production)
    observed = tools['freezone_get_production_progress']({'commission_id': commission['commission_ref']['artifact_id']})
    assert observed['projection_revision'] != initial['projection_revision']
    result = tools['freezone_advance_production']({'commission_ref': observed['commission_ref'],
        'expected_projection_revision': observed['projection_revision'], 'command_id': 'agent-adopt'})
    assert result['progress']['materials_accepted'] == 1
    assert result['work_ref']['artifact_id'] == work['artifact_id']
    assert result['next_action']['kind'] == 'bind_materials'
    assert store.read_latest(work['artifact_id'])['revision'] == work['revision'] + 1
    assert len(calls) == 3
    assert all('/director/' in path for _, path in calls)
