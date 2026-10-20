"""Business preflight simulates plan edits without publishing an artifact."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from novelvideo.freezone.remix.production_commands import (
    ProductionCommandConflict, apply_production_command, preflight_production_command,
)
from novelvideo.freezone.remix.store import RemixArtifactStore
from test_production_commands import _command, _plan


def _store(tmp_path: Path) -> RemixArtifactStore:
    return RemixArtifactStore(tmp_path / "alice", project_id="alice", created_by="alice")


def _files(store: RemixArtifactStore) -> dict[str, bytes]:
    return {str(path.relative_to(store.root)): path.read_bytes()
            for path in store.root.rglob("*") if path.is_file()}


def test_unbound_speech_source_fails_before_confirmation_without_writes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    saved = store.create("production_plan.v2", _plan(), [])
    ref = {"artifact_id": saved["id"], "revision": 1}
    command = _command(ref, command_id="preflight-unbound", kind="batch", target_id="intro",
                       payload={"commands": [
                           {"kind": "edit_beat", "target_id": "intro", "payload": {"purpose": "反问"}},
                           {"kind": "bind_speech_source", "target_id": "intro",
                            "payload": {"placement_id": "picture", "turn_ids": ["line"]}},
                       ]})
    before = _files(store)
    result = preflight_production_command(store, command)
    assert not result["ok"]
    assert result["issues"] == [{
        "code": "speech_source_media_required", "path": "/payload/commands/1/payload/placement_id",
        "entity_id": "picture",
        "message": "画面 picture 尚未绑定实际视频或音频。草稿关联台词请设置 speech_placement_id；素材生成并绑定后再校准语音。",
    }]
    assert _files(store) == before
    assert store.read_latest(saved["id"])["revision"] == 1


def test_planned_unbound_turn_reference_passes_and_still_needs_execution(tmp_path: Path) -> None:
    store = _store(tmp_path)
    saved = store.create("production_plan.v2", _plan(), [])
    ref = {"artifact_id": saved["id"], "revision": 1}
    command = _command(ref, command_id="preflight-draft-speech", kind="edit_turn",
                       target_id="line", payload={"speech_placement_id": "picture"})
    before = _files(store)
    result = preflight_production_command(store, command)
    assert result["ok"] and result["plan_ref"] == ref
    assert result["speech_estimates"] and isinstance(result["plan_diagnostics"], list)
    assert _files(store) == before
    assert store.read_latest(saved["id"])["revision"] == 1
    applied = apply_production_command(store, command)
    assert applied["plan_ref"]["revision"] == 2


def test_batch_can_bind_real_audible_media_before_speech_source(tmp_path: Path) -> None:
    store = _store(tmp_path)
    media = store.project_dir / "talk.mp4"
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"audible video fixture")
    plan = _plan()
    plan["resources"] = [{"id": "talk", "node_id": "video-node", "media_kind": "video",
                          "media_path": "talk.mp4", "media_hash": hashlib.sha256(media.read_bytes()).hexdigest(),
                          "duration_us": 4_000_000, "has_audio": True}]
    saved = store.create("production_plan.v2", plan, [])
    ref = {"artifact_id": saved["id"], "revision": 1}
    command = _command(ref, command_id="preflight-bound", kind="batch", target_id="intro",
                       payload={"commands": [
                           {"kind": "bind_media", "target_id": "picture", "payload": {"resource_id": "talk"}},
                           {"kind": "bind_speech_source", "target_id": "intro",
                            "payload": {"placement_id": "picture", "turn_ids": ["line"]}},
                       ]})
    before = _files(store)
    result = preflight_production_command(store, command)
    assert result["ok"] and not result["issues"]
    assert _files(store) == before
    assert store.read_latest(saved["id"])["revision"] == 1


def test_preflight_pass_does_not_bypass_version_check_on_execution(tmp_path: Path) -> None:
    store = _store(tmp_path)
    saved = store.create("production_plan.v2", _plan(), [])
    ref = {"artifact_id": saved["id"], "revision": 1}
    command = _command(ref, command_id="preflight-stale", payload={"purpose": "修正"})
    assert preflight_production_command(store, command)["ok"]
    apply_production_command(store, _command(ref, command_id="other-edit", payload={"purpose": "别人先改"}))
    with pytest.raises(ProductionCommandConflict):
        apply_production_command(store, command)


def test_preflight_accepts_exact_successful_replay_without_writing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    saved = store.create("production_plan.v2", _plan(), [])
    ref = {"artifact_id": saved["id"], "revision": 1}
    command = _command(ref, command_id="replay-preflight", payload={"purpose": "已保存的修改"})
    receipt = apply_production_command(store, command)
    before = _files(store)
    checked = preflight_production_command(store, command)
    assert checked["ok"] and checked["replayed"] is True
    assert checked["plan_ref"] == ref
    assert checked["speech_estimates"] == receipt["speech_estimates"]
    assert _files(store) == before


def test_preflight_accepts_exact_successful_batch_replay(tmp_path: Path) -> None:
    store = _store(tmp_path)
    saved = store.create("production_plan.v2", _plan(), [])
    ref = {"artifact_id": saved["id"], "revision": 1}
    command = _command(ref, command_id="replay-batch", kind="batch", target_id="intro",
                       payload={"commands": [
                           {"kind": "edit_beat", "target_id": "intro", "payload": {"purpose": "反转"}},
                           {"kind": "edit_turn", "target_id": "line", "payload": {"text": "黑咖啡"}},
                       ]})
    apply_production_command(store, command)
    before = _files(store)
    assert preflight_production_command(store, command)["replayed"] is True
    assert _files(store) == before
