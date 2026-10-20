"""A plan command must preserve the same evidence and revision rules as manual edits."""

from __future__ import annotations

from copy import deepcopy
import base64
import hashlib
from pathlib import Path

import pytest

from novelvideo.freezone.remix.store import RemixArtifactStore
from novelvideo.freezone.remix.production_commands import (
    ProductionCommandConflict,
    apply_production_command,
    validate_production_command,
)


def _plan() -> dict:
    def anchor(edge: str) -> dict:
        return {"kind": "beat", "ref_id": "intro", "edge": edge}
    return {
        "schema_version": 2,
        "goal": "参考片改编",
        "output": {"width": 1920, "height": 1080, "fps_num": 24000, "fps_den": 1001},
        "beats": [{"id": "intro", "purpose": "提问", "duration_frames": 72,
                   "turns": [{"id": "line", "speaker": "嘉宾", "text": "原台词"}]}],
        "resources": [],
        "placements": [{"id": "picture", "kind": "visual", "role": "primary", "track_id": "visual",
                        "start": anchor("start"), "end": anchor("end")}],
        "provenance": {},
    }


def _store(tmp_path: Path, project: str = "alice") -> RemixArtifactStore:
    return RemixArtifactStore(tmp_path / project, project_id=project, created_by="alice")


def _command(ref: dict, *, command_id: str = "edit-one", kind: str = "edit_beat",
             target_id: str = "intro", payload: dict | None = None) -> dict:
    return {"command_id": command_id, "plan_ref": ref, "kind": kind,
            "target_id": target_id, "payload": payload or {"purpose": "开门见山"}}


def test_missing_plan_ref_names_the_missing_field() -> None:
    command = _command({"artifact_id": "saved-plan", "revision": 1})
    command.pop("plan_ref")
    with pytest.raises(ValueError, match="missing required fields: plan_ref"):
        validate_production_command(command)


def test_edit_beat_pace_receipt_contains_current_revision_measurement(tmp_path: Path) -> None:
    store = _store(tmp_path)
    plan = _plan()
    plan['beats'][0]['turns'][0]['text'] = '上班来一杯，健身前来一杯，做饭的时候也得先来一杯。'
    saved = store.create('production_plan.v2', plan, [])
    receipt = apply_production_command(store, _command(
        {'artifact_id': saved['id'], 'revision': 1}, command_id='pace-1',
        payload={'speech_pace': 'fast', 'extra_pause_seconds': 0.5}))
    current = store.read_latest(saved['id'])['payload']
    assert current['beats'][0]['duration_frames'] == 72
    assert current['beats'][0]['speech_pace'] == 'fast'
    assert receipt['plan_ref']['revision'] == 2
    assert receipt['speech_estimates'][0]['entity_id'] == 'line'
    assert receipt['speech_estimates'][0]['estimate']['pace'] == 'fast'
    assert any(item['code'] == 'speech_density' for item in receipt['plan_diagnostics'])


def test_edit_turn_can_plan_unbound_visual_speech_source(tmp_path: Path) -> None:
    store = _store(tmp_path)
    saved = store.create("production_plan.v2", _plan(), [])
    result = apply_production_command(store, _command(
        {"artifact_id": saved["id"], "revision": 1}, command_id="assign-source",
        kind="edit_turn", target_id="line", payload={"speech_placement_id": "picture"}))
    current = store.read_latest(saved["id"])["payload"]
    assert current["beats"][0]["turns"][0]["speech_placement_id"] == "picture"
    assert "speech_timing" in result["invalidated"]


def test_bind_media_materializes_one_take_for_contiguous_planned_turns(tmp_path: Path) -> None:
    from test_production_audio_sources import planned_video_dialogue

    store = _store(tmp_path)
    media = store.project_dir / 'talk.mp4'
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b'audible video fixture')
    plan = planned_video_dialogue()
    plan['resources'] = [{'id': 'talk', 'node_id': 'video-node', 'media_kind': 'video',
                          'media_path': 'talk.mp4', 'media_hash': hashlib.sha256(media.read_bytes()).hexdigest(),
                          'duration_us': 6_000_000, 'has_audio': True}]
    saved = store.create('production_plan.v2', plan, [])
    ref = {'artifact_id': saved['id'], 'revision': 1}
    command = _command(ref, command_id='bind-planned-talk', kind='bind_media',
                       target_id='demo-video', payload={'resource_id': 'talk'})
    first = apply_production_command(store, command)
    current = store.read_latest(saved['id'])['payload']
    assert len(current['speech_takes']) == 1
    assert current['speech_takes'][0]['turn_ids'] == ['ask', 'answer']
    assert current['speech_takes'][0]['origin_placement_id'] == 'demo-video'
    assert [cue['turn_id'] for cue in current['speech_cues']] == ['ask', 'answer']
    assert current['placements'][0]['speech_take_id'] == current['speech_takes'][0]['id']
    assert apply_production_command(store, command) == first
    assert len(store.read_latest(saved['id'])['payload']['speech_takes']) == 1


def test_selecting_already_bound_audible_source_materializes_its_take(tmp_path: Path) -> None:
    store = _store(tmp_path)
    media = store.project_dir / 'talk.mp4'
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b'audible video fixture')
    plan = _plan()
    plan['resources'] = [{'id': 'talk', 'node_id': 'video-node', 'media_kind': 'video',
                          'media_path': 'talk.mp4', 'media_hash': hashlib.sha256(media.read_bytes()).hexdigest(),
                          'duration_us': 4_000_000, 'has_audio': True}]
    plan['placements'][0]['resource_id'] = 'talk'
    saved = store.create('production_plan.v2', plan, [])
    apply_production_command(store, _command(
        {'artifact_id': saved['id'], 'revision': 1}, command_id='select-bound-source',
        kind='edit_turn', target_id='line', payload={'speech_placement_id': 'picture'}))
    current = store.read_latest(saved['id'])['payload']
    assert len(current['speech_takes']) == 1
    assert current['speech_takes'][0]['turn_ids'] == ['line']
    assert current['speech_cues'][0]['turn_id'] == 'line'


def test_switching_draft_turn_between_bound_sources_replaces_one_take(tmp_path: Path) -> None:
    store = _store(tmp_path)
    plan = _plan()
    resources = []
    for identity in ('old', 'new'):
        path = store.project_dir / f'{identity}.mp4'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(identity.encode())
        resources.append({'id': identity, 'node_id': f'{identity}-node', 'media_kind': 'video',
                          'media_path': f'{identity}.mp4', 'media_hash': hashlib.sha256(path.read_bytes()).hexdigest(),
                          'duration_us': 4_000_000, 'has_audio': True})
    plan['resources'] = resources
    plan['placements'][0].update(resource_id='old', speech_take_id='old-take')
    plan['placements'].append({**deepcopy(plan['placements'][0]), 'id': 'alternate',
                               'resource_id': 'new', 'speech_take_id': None})
    plan['beats'][0]['turns'][0]['speech_placement_id'] = 'picture'
    plan['speech_takes'] = [{'id': 'old-take', 'beat_id': 'intro', 'resource_id': 'old',
                             'turn_ids': ['line'], 'origin_placement_id': 'picture'}]
    plan['speech_cues'] = [{'id': 'old-cue', 'turn_id': 'line', 'take_id': 'old-take',
                            'text': '原台词', 'timing_status': 'draft'}]
    saved = store.create('production_plan.v2', plan, [])
    apply_production_command(store, _command(
        {'artifact_id': saved['id'], 'revision': 1}, command_id='switch-draft-source',
        kind='edit_turn', target_id='line', payload={'speech_placement_id': 'alternate'}))
    current = store.read_latest(saved['id'])['payload']
    assert len(current['speech_takes']) == 1
    assert current['speech_takes'][0]['origin_placement_id'] == 'alternate'
    assert current['speech_cues'][0]['take_id'] == current['speech_takes'][0]['id']
    assert current['placements'][0].get('speech_take_id') is None


def test_switching_away_from_audio_placement_requires_removing_old_voice(tmp_path: Path) -> None:
    store = _store(tmp_path)
    plan = _plan()
    resources = []
    for identity, kind in (('voice', 'audio'), ('picture', 'video')):
        path = store.project_dir / f'{identity}.dat'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(identity.encode())
        resources.append({'id': identity, 'node_id': f'{identity}-node', 'media_kind': kind,
                          'media_path': f'{identity}.dat', 'media_hash': hashlib.sha256(path.read_bytes()).hexdigest(),
                          'duration_us': 4_000_000, 'has_audio': True})
    plan['resources'] = resources
    plan['placements'][0]['resource_id'] = 'picture'
    plan['placements'].append({'id': 'voice-placement', 'kind': 'audio', 'role': 'speech',
                               'track_id': 'audio:speech', 'resource_id': 'voice',
                               'start': {'kind': 'beat', 'ref_id': 'intro', 'edge': 'start'},
                               'end': {'kind': 'beat', 'ref_id': 'intro', 'edge': 'end'}})
    plan['beats'][0]['turns'][0]['speech_placement_id'] = 'voice-placement'
    plan['speech_takes'] = [{'id': 'old-take', 'beat_id': 'intro', 'resource_id': 'voice',
                             'turn_ids': ['line'], 'origin_placement_id': 'voice-placement'}]
    saved = store.create('production_plan.v2', plan, [])
    with pytest.raises(ValueError, match='dedicated correction flow'):
        apply_production_command(store, _command(
            {'artifact_id': saved['id'], 'revision': 1}, command_id='unsafe-audio-switch',
            kind='edit_turn', target_id='line', payload={'speech_placement_id': 'picture'}))


def test_source_change_needs_explicit_invalidation_of_ready_evidence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    plan = _plan()
    plan["placements"].append({**deepcopy(plan["placements"][0]), "id": "alternate"})
    plan["beats"][0]["turns"][0]["speech_placement_id"] = "picture"
    plan["beats"][0]["turns"][0]["timing_basis"] = "manual"
    plan["beats"][0]["turns"][0]["start_frame"] = 0
    plan["beats"][0]["turns"][0]["end_frame"] = 20
    saved = store.create("production_plan.v2", plan, [])
    ref = {"artifact_id": saved["id"], "revision": 1}
    with pytest.raises(ValueError, match="accepted speech timing"):
        apply_production_command(store, _command(ref, command_id="unsafe-source", kind="edit_turn",
                                 target_id="line", payload={"speech_placement_id": "alternate"}))
    result = apply_production_command(store, _command(ref, command_id="safe-source", kind="edit_turn",
                                      target_id="line", payload={"speech_placement_id": "alternate",
                                                                 "invalidate_speech_timing": True}))
    current = store.read_latest(saved["id"])["payload"]["beats"][0]["turns"][0]
    assert current["speech_placement_id"] == "alternate"
    assert current["timing_basis"] == "estimated"
    assert "start_frame" not in current
    assert "speech_timing" in result["invalidated"]


def test_upsert_source_audio_change_on_unbound_spoken_placement_is_authorable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    plan = _plan()
    plan["beats"][0]["turns"][0]["speech_placement_id"] = "picture"
    saved = store.create("production_plan.v2", plan, [])
    placement = deepcopy(saved["payload"]["placements"][0])
    placement["source_audio"] = True
    apply_production_command(store, _command(
        {"artifact_id": saved["id"], "revision": 1}, command_id="enable-source-audio",
        kind="upsert_placement", target_id="picture", payload={"placement": placement}))
    assert store.read_latest(saved["id"])["payload"]["placements"][0]["source_audio"] is True


def test_upsert_source_audio_change_with_accepted_timing_needs_correction(tmp_path: Path) -> None:
    store = _store(tmp_path)
    plan = _plan()
    turn = plan["beats"][0]["turns"][0]
    turn.update(speech_placement_id="picture", timing_basis="manual", start_frame=0, end_frame=20)
    saved = store.create("production_plan.v2", plan, [])
    placement = deepcopy(saved["payload"]["placements"][0])
    placement["source_audio"] = True
    with pytest.raises(ValueError, match="accepted speech timing"):
        apply_production_command(store, _command(
            {"artifact_id": saved["id"], "revision": 1}, command_id="change-accepted-source-audio",
            kind="upsert_placement", target_id="picture", payload={"placement": placement}))


def test_command_rejects_stale_revision_without_changing_current_plan(tmp_path: Path) -> None:
    store = _store(tmp_path)
    saved = store.create("production_plan.v2", _plan(), [])
    ref = {"artifact_id": saved["id"], "revision": 1}
    result = apply_production_command(store, _command(ref))
    assert result["plan_ref"] == {"artifact_id": saved["id"], "revision": 2}
    with pytest.raises(ProductionCommandConflict) as error:
        apply_production_command(store, _command(ref, command_id="edit-two", payload={"purpose": "新开头"}))
    assert error.value.current_revision == 2
    assert store.read_latest(saved["id"])["payload"]["beats"][0]["purpose"] == "开门见山"


def test_goal_edit_is_bounded_and_keeps_other_plan_entities(tmp_path):
    store = _store(tmp_path)
    saved = store.create('production_plan.v2', _plan(), [])
    result = apply_production_command(store, _command({'artifact_id':saved['id'],'revision':1}, kind='set_goal', target_id='plan', payload={'goal':'产品换成茶'}))
    changed = store.read(saved['id'],result['plan_ref']['revision'])['payload']
    assert changed['goal'] == '产品换成茶'
    assert changed['beats'] == saved['payload']['beats']
    with pytest.raises(ValueError, match='nonempty'):
        apply_production_command(store, _command(result['plan_ref'], command_id='bad-goal', kind='set_goal', target_id='plan', payload={'goal':''}))


def test_native_take_edit_changes_playback_without_replacing_the_plan(tmp_path):
    store = _store(tmp_path)
    media = store.project_dir / 'speech.wav'
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b'example audio')
    plan = _plan()
    plan['resources'] = [{'id': 'voice', 'node_id': 'voice-node', 'media_kind': 'audio',
                          'media_path': 'speech.wav', 'media_hash': hashlib.sha256(media.read_bytes()).hexdigest(),
                          'duration_us': 4_000_000, 'has_audio': True}]
    plan['placements'].append({'id': 'voice-placement', 'kind': 'audio', 'role': 'speech', 'track_id': 'audio:speech',
                               'resource_id': 'voice', 'start': {'kind': 'beat', 'ref_id': 'intro', 'edge': 'start'},
                               'end': {'kind': 'beat', 'ref_id': 'intro', 'edge': 'end'}})
    plan['speech_takes'] = [{'id': 'take', 'beat_id': 'intro', 'resource_id': 'voice',
                             'turn_ids': ['line'], 'origin_placement_id': 'voice-placement'}]
    saved = store.create('production_plan.v2', plan, [])
    result = apply_production_command(store, _command(
        {'artifact_id': saved['id'], 'revision': 1}, command_id='move-take', kind='edit_speech_take',
        target_id='take', payload={'lead_in_frames': 8, 'volume': 0.6}))
    current = store.read_latest(saved['id'])['payload']
    assert result['changed_ids'] == ['take']
    assert current['speech_takes'][0]['lead_in_frames'] == 8
    assert current['speech_takes'][0]['volume'] == 0.6
    assert current['beats'] == saved['payload']['beats']
    with pytest.raises(ValueError, match='unexpected fields'):
        validate_production_command(_command(result['plan_ref'], kind='edit_speech_take', target_id='take',
            payload={'turn_ids': ['another']}))


def test_repeating_command_after_restart_returns_same_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    saved = store.create("production_plan.v2", _plan(), [])
    command = _command({"artifact_id": saved["id"], "revision": 1})
    first = apply_production_command(store, command)
    restarted = _store(tmp_path)
    assert apply_production_command(restarted, command) == first
    assert restarted.read_latest(saved["id"])["revision"] == 2
    with pytest.raises(ProductionCommandConflict, match="different request"):
        apply_production_command(restarted, _command(command["plan_ref"], payload={"purpose": "different"}))


def test_command_cannot_bind_media_outside_authorized_project(tmp_path: Path) -> None:
    owner = _store(tmp_path)
    peer = _store(tmp_path, "peer")
    peer_file = peer.project_dir / "assets" / "peer.png"
    peer_file.parent.mkdir(parents=True)
    peer_file.write_bytes(b"peer image")
    plan = _plan()
    plan["resources"] = [{"id": "peer-media", "node_id": "peer-node", "media_kind": "image",
                          "media_path": "assets/peer.png", "media_hash": hashlib.sha256(peer_file.read_bytes()).hexdigest(),
                          "duration_us": 0}]
    saved = owner.create("production_plan.v2", plan, [])
    command = _command({"artifact_id": saved["id"], "revision": 1}, command_id="bind-peer",
                       kind="bind_media", target_id="picture", payload={"resource_id": "peer-media"})
    with pytest.raises(ValueError, match="project media"):
        apply_production_command(owner, command)
    assert owner.read_latest(saved["id"])["revision"] == 1


def test_visual_media_replacement_preserves_spoken_evidence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    image = store.project_dir / "assets" / "new.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"replacement")
    plan = _plan()
    plan["resources"] = [{"id": "new-image", "node_id": "new-image-node", "media_kind": "image",
                          "media_path": "assets/new.png", "media_hash": hashlib.sha256(image.read_bytes()).hexdigest(),
                          "duration_us": 0}]
    saved = store.create("production_plan.v2", plan, [])
    ref = {"artifact_id": saved["id"], "revision": 1}
    result = apply_production_command(store, _command(ref, command_id="bind-image", kind="bind_media",
                                                          target_id="picture", payload={"resource_id": "new-image"}))
    assert result["invalidated"] == ["projection", "render"]
    assert store.read_latest(saved["id"])["payload"]["placements"][0]["resource_id"] == "new-image"


def test_rejects_unbounded_patch_and_keeps_original_plan(tmp_path: Path) -> None:
    store = _store(tmp_path)
    saved = store.create("production_plan.v2", _plan(), [])
    ref = {"artifact_id": saved["id"], "revision": 1}
    with pytest.raises(ValueError):
        apply_production_command(store, _command(ref, payload={"unexpected": "write all"}))
    assert store.read_latest(saved["id"])["payload"] == deepcopy(saved["payload"])


def test_speech_source_replacement_requires_bind_command_and_invalidates_take(tmp_path: Path) -> None:
    store = _store(tmp_path)
    plan = _plan()
    media_dir = store.project_dir / "assets"
    media_dir.mkdir(parents=True)
    resources = []
    for resource_id in ("old", "new"):
        path = media_dir / f"{resource_id}.mp4"
        path.write_bytes(resource_id.encode())
        resources.append({"id": resource_id, "node_id": f"node-{resource_id}", "media_kind": "video",
                          "media_path": f"assets/{resource_id}.mp4", "media_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
                          "duration_us": 4_000_000, "has_audio": True})
    plan["resources"] = resources
    plan["placements"][0]["resource_id"] = "old"
    plan["beats"][0]["turns"][0]["speech_placement_id"] = "picture"
    plan["speech_takes"] = [{"id": "take", "beat_id": "intro", "resource_id": "old",
                             "turn_ids": ["line"], "origin_placement_id": "picture"}]
    saved = store.create("production_plan.v2", plan, [])
    ref = {"artifact_id": saved["id"], "revision": 1}
    replacement = {**saved["payload"]["placements"][0], "resource_id": "new"}
    with pytest.raises(ValueError, match="bind_media"):
        apply_production_command(store, _command(ref, command_id="replace-direct", kind="upsert_placement",
                                                      target_id="picture", payload={"placement": replacement}))
    assert store.read_latest(saved["id"])["revision"] == 1
    result = apply_production_command(store, _command(ref, command_id="replace-bound", kind="bind_media",
                                                          target_id="picture", payload={"resource_id": "new"}))
    assert "speech_timing" in result["invalidated"]
    assert result["blockers"] == ["speech_timing_requires_recalibration"]
    current = store.read_latest(saved["id"])["payload"]
    assert current["speech_takes"][0]["resource_id"] == "new"
    assert current["placements"][0]["resource_id"] == "new"


def test_rebinding_shared_speech_take_updates_every_visual_window_atomically(tmp_path: Path) -> None:
    store = _store(tmp_path)
    plan = _spoken_plan(store)
    plan["placements"].append({
        "id": "reaction-window", "kind": "visual", "role": "overlay", "track_id": "overlay",
        "resource_id": "old", "speech_take_id": "take",
        "start": {"kind": "beat", "ref_id": "intro", "edge": "start"},
        "end": {"kind": "beat", "ref_id": "intro", "edge": "end"},
    })
    saved = store.create("production_plan.v2", plan, [])
    result = apply_production_command(store, _command({"artifact_id": saved["id"], "revision": 1},
        command_id="shared-rebind", kind="bind_media", target_id="picture", payload={"resource_id": "new"}))
    current = store.read_latest(saved["id"])["payload"]
    assert result["invalidated"].count("speech_timing") == 1
    assert current["speech_takes"][0]["resource_id"] == "new"
    assert {row["resource_id"] for row in current["placements"] if row.get("speech_take_id") == "take"} == {"new"}


def test_bind_media_can_register_verified_project_resource(tmp_path: Path) -> None:
    store = _store(tmp_path)
    image = store.project_dir / "assets" / "generated.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
    ))
    resource = {"id": "generated", "node_id": "image-node", "media_kind": "image",
                "media_path": "assets/generated.png", "media_hash": hashlib.sha256(image.read_bytes()).hexdigest(),
                "duration_us": 0, "has_audio": False}
    saved = store.create("production_plan.v2", _plan(), [])
    ref = {"artifact_id": saved["id"], "revision": 1}
    command = _command(ref, command_id="bind-new", kind="bind_media", target_id="picture",
                       payload={"resource_id": "generated", "resource": resource})
    result = apply_production_command(store, command)
    assert result["plan_ref"]["revision"] == 2
    current = store.read_latest(saved["id"])["payload"]
    assert current["resources"] == [resource]
    assert current["placements"][0]["resource_id"] == "generated"


def test_register_resource_rejects_spoofed_metadata_and_keeps_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    image = store.project_dir / "assets" / "generated.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
    ))
    saved = store.create("production_plan.v2", _plan(), [])
    resource = {"id": "generated", "node_id": "image-node", "media_kind": "video",
                "media_path": "assets/generated.png", "media_hash": hashlib.sha256(image.read_bytes()).hexdigest(),
                "duration_us": 5_000_000, "has_audio": True}
    command = _command({"artifact_id": saved["id"], "revision": 1}, command_id="spoofed",
                       kind="bind_media", target_id="picture",
                       payload={"resource_id": "generated", "resource": resource})
    with pytest.raises(ValueError, match="metadata"):
        apply_production_command(store, command)
    assert store.read_latest(saved["id"])["revision"] == 1


def _spoken_plan(store: RemixArtifactStore) -> dict:
    plan = _plan()
    folder = store.project_dir / "assets"
    folder.mkdir(parents=True, exist_ok=True)
    for media_id in ("old", "new"):
        (folder / f"{media_id}.mp4").write_bytes(media_id.encode())
    plan["resources"] = [
        {"id": media_id, "node_id": f"node-{media_id}", "media_kind": "video",
         "media_path": f"assets/{media_id}.mp4", "media_hash": hashlib.sha256(media_id.encode()).hexdigest(),
         "duration_us": 4_000_000, "has_audio": True}
        for media_id in ("old", "new")
    ]
    plan["placements"][0]["resource_id"] = "old"
    plan["beats"][0]["turns"][0]["speech_placement_id"] = "picture"
    plan["speech_takes"] = [{"id": "take", "beat_id": "intro", "resource_id": "old",
                             "turn_ids": ["line"], "origin_placement_id": "picture"}]
    return plan


def _accepted_spoken_plan(store: RemixArtifactStore) -> tuple[dict, dict]:
    plan = _spoken_plan(store)
    original = store.create("production_plan.v2", plan, [])
    source_ref = {"artifact_id": original["id"], "revision": 1}
    evidence = store.create("speech_cue_evidence.v1", {
        "take_id": "take", "media_hash": plan["resources"][0]["media_hash"], "basis": "manual",
        "source_plan_ref": source_ref,
        "cues": [{"id": "cue", "turn_id": "line", "text": "原台词", "start_us": 500_000, "end_us": 1_000_000}],
    }, [source_ref])
    evidence_ref = {"artifact_id": evidence["id"], "revision": 1}
    plan["speech_cues"] = [{"id": "cue", "turn_id": "line", "take_id": "take", "text": "原台词",
                            "source_start_us": 500_000, "source_end_us": 1_000_000,
                            "timing_source": "manual", "timing_status": "ready", "word_ids": [],
                            "evidence_ref": evidence_ref}]
    accepted = store.revise(original["id"], 1, plan, [evidence_ref])
    return accepted, plan


def test_editing_accepted_dialogue_refuses_to_discard_calibration(tmp_path: Path) -> None:
    store = _store(tmp_path)
    accepted, _ = _accepted_spoken_plan(store)
    ref = {"artifact_id": accepted["id"], "revision": accepted["revision"]}
    with pytest.raises(ValueError, match="accepted speech"):
        apply_production_command(store, _command(ref, command_id="rewrite-accepted", kind="edit_turn",
                                                  target_id="line", payload={"text": "新台词"}))
    assert store.read_latest(accepted["id"])["revision"] == 2


def test_explicit_dialogue_invalidation_keeps_cue_and_evidence_history(tmp_path: Path) -> None:
    store = _store(tmp_path)
    accepted, _ = _accepted_spoken_plan(store)
    ref = {"artifact_id": accepted["id"], "revision": accepted["revision"]}
    previous = store.read_latest(accepted["id"])["payload"]["speech_cues"][0]
    result = apply_production_command(store, _command(ref, command_id="explicit-rewrite", kind="edit_turn",
        target_id="line", payload={"text": "修改后的台词", "invalidate_speech_timing": True}))
    assert result["blockers"] == ["speech_timing_requires_recalibration"]
    current = store.read_latest(accepted["id"])["payload"]
    assert current["beats"][0]["turns"][0]["text"] == "修改后的台词"
    assert current["speech_cues"][0]["id"] == previous["id"]
    assert current["speech_cues"][0]["timing_status"] == "stale"
    assert current["speech_cues"][0]["evidence_ref"] == previous["evidence_ref"]


def test_explicit_dialogue_invalidation_still_rejects_word_anchors(tmp_path: Path) -> None:
    store = _store(tmp_path)
    plan = _spoken_plan(store)
    plan["beats"][0]["turns"][0].update({
        "start_frame": 0, "end_frame": 72, "timing_basis": "aligned",
        "words": [{"id": "word-one", "text": "原", "start_frame": 0, "end_frame": 18}],
    })
    plan["selections"] = [{"id": "on-word", "label": "关键词", "start": {"kind": "word", "ref_id": "word-one", "edge": "start"},
                            "end": {"kind": "word", "ref_id": "word-one", "edge": "end"}}]
    saved = store.create("production_plan.v2", plan, [])
    with pytest.raises(ValueError, match="word anchors"):
        apply_production_command(store, _command({"artifact_id": saved["id"], "revision": 1},
            command_id="rewrite-anchored-explicit", kind="edit_turn", target_id="line",
            payload={"text": "新台词", "invalidate_speech_timing": True}))
    assert store.read_latest(saved["id"])["revision"] == 1


def test_binding_new_voice_refuses_to_discard_accepted_cues(tmp_path: Path) -> None:
    store = _store(tmp_path)
    accepted, _ = _accepted_spoken_plan(store)
    ref = {"artifact_id": accepted["id"], "revision": accepted["revision"]}
    with pytest.raises(ValueError, match="accepted speech"):
        apply_production_command(store, _command(ref, command_id="replace-accepted-source", kind="bind_media",
                                                  target_id="picture", payload={"resource_id": "new"}))
    assert store.read_latest(accepted["id"])["revision"] == 2


def test_explicit_voice_rebind_marks_old_cue_stale_without_erasing_evidence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    accepted, _ = _accepted_spoken_plan(store)
    previous = store.read_latest(accepted["id"])["payload"]["speech_cues"][0]
    result = apply_production_command(store, _command({"artifact_id": accepted["id"], "revision": accepted["revision"]},
        command_id="explicit-voice-rebind", kind="bind_media", target_id="picture",
        payload={"resource_id": "new", "invalidate_speech_timing": True}))
    assert result["blockers"] == ["speech_timing_requires_recalibration"]
    current = store.read_latest(accepted["id"])["payload"]
    assert current["speech_takes"][0]["resource_id"] == "new"
    assert current["speech_cues"][0]["timing_status"] == "stale"
    assert current["speech_cues"][0]["evidence_ref"] == previous["evidence_ref"]


def test_editing_turn_with_word_anchors_reports_explicit_blocker(tmp_path: Path) -> None:
    store = _store(tmp_path)
    plan = _spoken_plan(store)
    plan["beats"][0]["turns"][0].update({
        "start_frame": 0, "end_frame": 72, "timing_basis": "aligned",
        "words": [{"id": "word-one", "text": "原", "start_frame": 0, "end_frame": 18}],
    })
    plan["speech_cues"] = [{"id": "cue", "turn_id": "line", "take_id": "take", "text": "原台词",
                            "timing_source": "asr", "timing_status": "review", "word_ids": ["word-one"]}]
    plan["selections"] = [{"id": "on-word", "label": "关键词", "start": {"kind": "word", "ref_id": "word-one", "edge": "start"},
                            "end": {"kind": "word", "ref_id": "word-one", "edge": "end"}}]
    saved = store.create("production_plan.v2", plan, [])
    with pytest.raises(ValueError, match="word anchors"):
        apply_production_command(store, _command({"artifact_id": saved["id"], "revision": 1},
                                                  command_id="rewrite-anchored", kind="edit_turn", target_id="line",
                                                  payload={"text": "新台词"}))
    assert store.read_latest(saved["id"])["revision"] == 1


def test_upsert_cannot_change_speech_source_playback_without_take_edit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    plan = _spoken_plan(store)
    saved = store.create("production_plan.v2", plan, [])
    ref = {"artifact_id": saved["id"], "revision": 1}
    old = saved["payload"]["placements"][0]
    for index, replacement in enumerate(({**old, "source_in_us": 400_000},
                                         {**old, "speed_num": 2},
                                         {**old, "start": {"kind": "beat", "ref_id": "intro", "edge": "start", "offset_frames": 3}})):
        with pytest.raises(ValueError, match="speech placement"):
            apply_production_command(store, _command(ref, command_id=f"source-change-{index}",
                                                      kind="upsert_placement", target_id="picture",
                                                      payload={"placement": replacement}))
    assert store.read_latest(saved["id"])["revision"] == 1


def test_uncalibrated_spoken_text_edit_keeps_a_recalibration_blocker(tmp_path: Path) -> None:
    store = _store(tmp_path)
    plan = _spoken_plan(store)
    saved = store.create("production_plan.v2", plan, [])
    result = apply_production_command(store, _command({"artifact_id": saved["id"], "revision": 1},
                                                      command_id="rewrite-draft", kind="edit_turn", target_id="line",
                                                      payload={"text": "修改后的台词"}))
    assert result["blockers"] == ["speech_timing_requires_recalibration"]


def test_bind_speech_source_groups_two_ordered_turns_on_one_video_take(tmp_path: Path) -> None:
    store = _store(tmp_path)
    plan = _plan()
    plan["beats"][0]["turns"].append({"id": "reply", "speaker": "主持人", "text": "下一题"})
    media = store.project_dir / "assets" / "conversation.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"two speakers in one source")
    plan["resources"] = [{"id": "conversation", "node_id": "source", "media_kind": "video",
                          "media_path": "assets/conversation.mp4", "media_hash": hashlib.sha256(media.read_bytes()).hexdigest(),
                          "duration_us": 4_000_000, "has_audio": True}]
    plan["placements"][0]["resource_id"] = "conversation"
    saved = store.create("production_plan.v2", plan, [])
    result = apply_production_command(store, _command({"artifact_id": saved["id"], "revision": 1},
        command_id="bind-two-voices", kind="bind_speech_source", target_id="intro",
        payload={"placement_id": "picture", "turn_ids": ["line", "reply"]}))
    current = store.read_latest(saved["id"])["payload"]
    assert result["blockers"] == ["speech_timing_requires_calibration"]
    assert current["speech_takes"][0] == {"id": "take:intro:picture", "beat_id": "intro",
        "resource_id": "conversation", "turn_ids": ["line", "reply"],
        "origin_placement_id": "picture", "source_in_us": 0,
        "speed_num": 1, "speed_den": 1, "lead_in_frames": 0, "volume": 1.0}
    assert [cue["turn_id"] for cue in current["speech_cues"]] == ["line", "reply"]
    assert all(cue["timing_status"] == "draft" for cue in current["speech_cues"])
    assert {turn["speech_placement_id"] for turn in current["beats"][0]["turns"]} == {"picture"}
    assert current["placements"][0]["speech_take_id"] == "take:intro:picture"


@pytest.mark.parametrize("target_bound", [True, False])
def test_reassigns_one_draft_audio_take_to_video_for_both_turns(tmp_path: Path, target_bound: bool) -> None:
    store = _store(tmp_path)
    plan = _plan()
    plan["beats"][0]["turns"].append({"id": "reply", "speaker": "主持人", "text": "下一题"})
    for identity in ("voice", "video"):
        path = store.project_dir / f"{identity}.dat"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(identity.encode())
        plan["resources"].append({"id": identity, "node_id": f"{identity}-node",
                                  "media_kind": "audio" if identity == "voice" else "video",
                                  "media_path": f"{identity}.dat", "media_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
                                  "duration_us": 4_000_000, "has_audio": True})
    if target_bound:
        plan["placements"][0]["resource_id"] = "video"
    plan["placements"].append({"id": "voice-placement", "kind": "audio", "role": "speech",
                               "track_id": "audio:speech", "start": {"kind": "beat", "ref_id": "intro", "edge": "start"},
                               "end": {"kind": "beat", "ref_id": "intro", "edge": "end"}})
    for turn in plan["beats"][0]["turns"]:
        turn["speech_placement_id"] = "voice-placement"
    saved = store.create("production_plan.v2", plan, [])
    first = apply_production_command(store, _command({"artifact_id": saved["id"], "revision": 1},
        command_id="bind-draft-voice", kind="bind_media", target_id="voice-placement", payload={"resource_id": "voice"}))
    draft = store.read_latest(saved["id"])["payload"]
    assert first["plan_ref"]["revision"] == 2
    assert draft["speech_takes"][0]["turn_ids"] == ["line", "reply"]
    assert all(cue["timing_status"] == "draft" for cue in draft["speech_cues"])
    result = apply_production_command(store, _command({"artifact_id": saved["id"], "revision": 2},
        command_id="move-draft-voice-to-video", kind="reassign_speech_source", target_id="intro",
        payload={"placement_id": "picture", "turn_ids": ["line", "reply"]}))
    current = store.read_latest(saved["id"])["payload"]
    assert result["plan_ref"]["revision"] == 3
    assert [turn["speech_placement_id"] for turn in current["beats"][0]["turns"]] == ["picture", "picture"]
    assert len(current["speech_takes"]) == (1 if target_bound else 0)
    assert len(current["speech_cues"]) == (2 if target_bound else 0)
    if target_bound:
        assert current["speech_takes"][0]["origin_placement_id"] == "picture"
        assert current["speech_takes"][0]["resource_id"] == "video"
        assert [cue["turn_id"] for cue in current["speech_cues"]] == ["line", "reply"]
        assert all(cue["take_id"] == current["speech_takes"][0]["id"] and cue["timing_status"] == "draft"
                   for cue in current["speech_cues"])
        assert current["placements"][0]["speech_take_id"] == current["speech_takes"][0]["id"]
    else:
        assert current["placements"][0].get("speech_take_id") is None
    assert current["placements"][1].get("speech_take_id") is None


def test_reassign_draft_take_rejects_word_anchored_evidence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    plan = _plan()
    plan["beats"][0]["turns"].append({"id": "reply", "speaker": "主持人", "text": "下一题"})
    for turn in plan["beats"][0]["turns"]:
        turn["speech_placement_id"] = "picture"
    plan["speech_takes"] = [{"id": "take", "beat_id": "intro", "resource_id": "video",
                             "turn_ids": ["line", "reply"], "origin_placement_id": "picture"}]
    plan["speech_cues"] = [{"id": f"cue:{turn['id']}", "turn_id": turn["id"], "take_id": "take",
                            "text": turn["text"], "timing_source": "asr", "timing_status": "draft", "word_ids": []}
                           for turn in plan["beats"][0]["turns"]]
    plan["beats"][0]["turns"][0].update({"start_frame": 0, "end_frame": 20, "timing_basis": "aligned",
        "words": [{"id": "word-one", "text": "原", "start_frame": 0, "end_frame": 10}]})
    plan["selections"] = [{"id": "on-word", "label": "关键词", "start": {"kind": "word", "ref_id": "word-one", "edge": "start"},
                           "end": {"kind": "word", "ref_id": "word-one", "edge": "end"}}]
    path = store.project_dir / "video.dat"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"video")
    plan["resources"] = [{"id": "video", "node_id": "video-node", "media_kind": "video", "media_path": "video.dat",
                          "media_hash": hashlib.sha256(path.read_bytes()).hexdigest(), "duration_us": 4_000_000, "has_audio": True}]
    plan["placements"][0].update(resource_id="video", speech_take_id="take")
    saved = store.create("production_plan.v2", plan, [])
    with pytest.raises(ValueError, match="word anchors"):
        apply_production_command(store, _command({"artifact_id": saved["id"], "revision": 1},
            command_id="unsafe-reassign", kind="reassign_speech_source", target_id="intro",
            payload={"placement_id": None, "turn_ids": ["line", "reply"]}))
    assert store.read_latest(saved["id"])["revision"] == 1


def test_reassign_shared_take_rejects_accepted_speech_evidence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    plan = _spoken_plan(store)
    plan["beats"][0]["turns"].append({"id": "reply", "speaker": "主持人", "text": "下一题",
                                      "speech_placement_id": "picture"})
    plan["speech_takes"][0]["turn_ids"] = ["line", "reply"]
    original = store.create("production_plan.v2", plan, [])
    source_ref = {"artifact_id": original["id"], "revision": 1}
    evidence = store.create("speech_cue_evidence.v1", {
        "take_id": "take", "media_hash": plan["resources"][0]["media_hash"], "basis": "manual",
        "source_plan_ref": source_ref,
        "cues": [{"id": "cue", "turn_id": "line", "text": "原台词", "start_us": 500_000, "end_us": 1_000_000}],
    }, [source_ref])
    evidence_ref = {"artifact_id": evidence["id"], "revision": 1}
    plan["speech_cues"] = [
        {"id": "cue", "turn_id": "line", "take_id": "take", "text": "原台词",
         "source_start_us": 500_000, "source_end_us": 1_000_000,
         "timing_source": "manual", "timing_status": "ready", "word_ids": [],
         "evidence_ref": evidence_ref},
        {"id": "cue:reply", "turn_id": "reply", "take_id": "take", "text": "下一题",
         "timing_source": "asr", "timing_status": "draft", "word_ids": []},
    ]
    accepted = store.revise(original["id"], 1, plan, [evidence_ref])
    with pytest.raises(ValueError, match="accepted speech"):
        apply_production_command(store, _command({"artifact_id": accepted["id"], "revision": 2},
            command_id="unsafe-reassign-accepted", kind="reassign_speech_source", target_id="intro",
            payload={"placement_id": None, "turn_ids": ["line", "reply"]}))
    assert store.read_latest(accepted["id"])["revision"] == 2


def test_bind_speech_source_rejects_noncontiguous_and_reuses_uncalibrated_take(tmp_path: Path) -> None:
    store = _store(tmp_path)
    plan = _spoken_plan(store)
    plan["beats"][0]["turns"].extend([
        {"id": "middle", "speaker": "甲", "text": "中间"},
        {"id": "last", "speaker": "乙", "text": "结束"},
    ])
    saved = store.create("production_plan.v2", plan, [])
    ref = {"artifact_id": saved["id"], "revision": 1}
    with pytest.raises(ValueError, match="contiguous"):
        apply_production_command(store, _command(ref, command_id="skip-middle", kind="bind_speech_source",
            target_id="intro", payload={"placement_id": "picture", "turn_ids": ["line", "last"]}))
    result = apply_production_command(store, _command(ref, command_id="extend-existing", kind="bind_speech_source",
        target_id="intro", payload={"placement_id": "picture", "turn_ids": ["line", "middle"]}))
    assert result["plan_ref"]["revision"] == 2
    current = store.read_latest(saved["id"])["payload"]
    assert current["speech_takes"][0]["id"] == "take"
    assert current["speech_takes"][0]["turn_ids"] == ["line", "middle"]


def test_bind_speech_source_rejects_accepted_calibration(tmp_path: Path) -> None:
    store = _store(tmp_path)
    accepted, _ = _accepted_spoken_plan(store)
    with pytest.raises(ValueError, match="accepted speech"):
        apply_production_command(store, _command({"artifact_id": accepted["id"], "revision": accepted["revision"]},
            command_id="regroup-calibrated", kind="bind_speech_source", target_id="intro",
            payload={"placement_id": "picture", "turn_ids": ["line"]}))
    assert store.read_latest(accepted["id"])["revision"] == accepted["revision"]


def test_remove_speech_source_requires_explicit_unbinding(tmp_path: Path) -> None:
    store = _store(tmp_path)
    saved = store.create("production_plan.v2", _spoken_plan(store), [])
    with pytest.raises(ValueError, match="speech placement cannot be removed"):
        apply_production_command(store, _command({"artifact_id": saved["id"], "revision": 1},
                                                  command_id="remove-spoken-source", kind="remove_entity",
                                                  target_id="picture", payload={"entity_kind": "placement"}))
    assert store.read_latest(saved["id"])["revision"] == 1


def test_atomic_batch_inserts_and_reorders_without_partial_write(tmp_path: Path) -> None:
    from novelvideo.freezone.remix import production_commands as commands
    assert hasattr(commands, "apply_production_commands")
    store = _store(tmp_path)
    saved = store.create("production_plan.v2", _plan(), [])
    ref = {"artifact_id": saved["id"], "revision": 1}
    batch = [{"kind": "insert_beat", "target_id": "ending", "payload": {
        "beat": {"id": "ending", "purpose": "收束", "duration_frames": 48}, "index": 1}},
        {"kind": "insert_turn", "target_id": "reply", "payload": {
            "beat_id": "ending", "turn": {"id": "reply", "text": "回答"}, "index": 0}},
        {"kind": "reorder_beats", "target_id": "beats", "payload": {"ids": ["ending", "intro"]}}]
    result = commands.apply_production_commands(store, ref, batch, "batch-one")
    assert result["plan_ref"]["revision"] == 2
    assert [beat["id"] for beat in store.read_latest(saved["id"])["payload"]["beats"]] == ["ending", "intro"]
    assert commands.apply_production_commands(_store(tmp_path), ref, batch, "batch-one") == result
    ref2 = result["plan_ref"]
    with pytest.raises(ValueError):
        commands.apply_production_commands(store, ref2, [
            {"kind": "edit_turn", "target_id": "reply", "payload": {"text": "改"}},
            {"kind": "remove_entity", "target_id": "intro", "payload": {"entity_kind": "beat"}},
        ], "batch-fail")
    assert store.read_latest(saved["id"])["revision"] == 2
    assert store.read_latest(saved["id"])["payload"]["beats"][0]["turns"][0]["text"] == "回答"


def test_remove_turn_rejects_semantic_anchor_and_reorder_requires_permutation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    plan = _plan()
    plan["placements"][0]["start"] = {"kind": "turn", "ref_id": "line", "edge": "start"}
    saved = store.create("production_plan.v2", plan, [])
    ref = {"artifact_id": saved["id"], "revision": 1}
    with pytest.raises(ValueError, match="reference conflict"):
        apply_production_command(store, _command(ref, kind="remove_entity", target_id="line", payload={"entity_kind": "turn"}))
    with pytest.raises(ValueError, match="permutation"):
        apply_production_command(store, _command(ref, kind="reorder_turns", payload={"ids": []}))


@pytest.mark.parametrize("kind,payload,unexpected,accepted", [
    ("edit_beat", {"direction": "new story"}, "direction", "duration_frames, duration_source_placement_id, extra_pause_seconds, purpose, speech_pace, timing_mode"),
    ("edit_turn", {"timing_status": "estimated"}, "timing_status", "invalidate_speech_timing, speaker, speech_placement_id, text"),
    ("insert_turn", {"speaker": "host", "text": "hello"}, "speaker, text", "beat_id, index, turn"),
    ("batch", {"beats": [], "turns": []}, "beats, turns", "commands"),
])
def test_payload_rejection_identifies_kind_and_valid_keys(kind, payload, unexpected, accepted):
    from novelvideo.freezone.remix.production_commands import validate_production_command
    with pytest.raises(ValueError) as error:
        validate_production_command(_command({"artifact_id": "plan-1", "revision": 1}, kind=kind, payload=payload))
    message = str(error.value)
    assert kind in message
    assert f"unexpected keys: {unexpected}" in message
    assert f"accepted payload keys: {accepted}" in message


def test_batch_child_rejection_identifies_missing_target_and_exact_keys():
    from novelvideo.freezone.remix.production_commands import validate_production_command
    with pytest.raises(ValueError) as error:
        validate_production_command(_command({"artifact_id": "plan-1", "revision": 1}, kind="batch", target_id="plan", payload={
            "commands": [{"kind": "edit_turn", "payload": {"text": "hello"}}],
        }))
    message = str(error.value)
    assert "batch child 0" in message
    assert "missing keys: target_id" in message
    assert "exact child keys: kind, payload, target_id" in message


def test_documented_coordinated_batch_and_nested_insertion_apply(tmp_path):
    from novelvideo.freezone.remix.production_commands import validate_production_command
    store = _store(tmp_path)
    saved = store.create("production_plan.v2", _plan(), [])
    command = _command({"artifact_id": saved["id"], "revision": saved["revision"]}, kind="batch", target_id="plan", payload={
        "commands": [
            {"kind": "edit_beat", "target_id": "intro", "payload": {"purpose": "Opening question", "duration_frames": 96, "timing_mode": "auto"}},
            {"kind": "edit_turn", "target_id": "line", "payload": {"speaker": "Host", "text": "Why now?"}},
            {"kind": "insert_turn", "target_id": "reply", "payload": {"beat_id": "intro", "turn": {"id": "reply", "speaker": "Guest", "text": "Here is why."}, "index": 1}},
        ],
    })
    assert validate_production_command(command) == command
    result = apply_production_command(store, command)
    assert result["plan_ref"]["revision"] == saved["revision"] + 1
    plan = store.read_latest(saved["id"])["payload"]
    assert plan["beats"][0]["purpose"] == "Opening question"
    assert plan["beats"][0]["duration_frames"] == 96
    assert [(turn["id"], turn["speaker"], turn["text"]) for turn in plan["beats"][0]["turns"]] == [
        ("line", "Host", "Why now?"), ("reply", "Guest", "Here is why."),
    ]

@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("payload,diagnostic", [
    ({"turn": {"beat_id": "intro", "id": "reply", "speaker": "Guest", "text": "Reply", "timing_status": "estimated"}}, "missing required payload keys: beat_id, index"),
    ({"beat_id": "intro", "index": 1, "turn": {"id": "reply", "text": "Reply", "beat_id": "intro", "timing_status": "estimated"}}, "insert_turn payload.turn"),
    ({"beat_id": "intro", "index": True, "turn": {"id": "reply", "text": "Reply"}}, "index must be a nonnegative integer"),
    ({"beat_id": "", "index": 1, "turn": {"id": "reply", "text": "Reply"}}, "beat_id must be a nonempty string"),
])
def test_insert_turn_shape_rejected_before_apply(payload, diagnostic, batch):
    from novelvideo.freezone.remix.production_commands import validate_production_command
    child = {"kind": "insert_turn", "target_id": "reply", "payload": payload}
    command = _command({"artifact_id": "plan-1", "revision": 1}, **child)
    if batch:
        command = _command(command["plan_ref"], kind="batch", target_id="plan", payload={"commands": [child]})
    with pytest.raises(ValueError, match=diagnostic):
        validate_production_command(command)


def test_insert_turn_preserves_native_optional_fields_and_defaults(tmp_path):
    import hashlib
    from novelvideo.freezone.remix.production_commands import validate_production_command
    from novelvideo.freezone.remix.production_plan_v2 import TurnV2
    turn = {"id": "reply", "speaker": "Guest", "text": "Reply", "start_frame": 0, "end_frame": 12,
            "timing_basis": "aligned", "words": [{"id": "reply-word", "text": "Reply", "start_frame": 0, "end_frame": 12}],
            "speech_placement_id": "speech", "speech_timing": {
                "placement_id": "speech", "media_hash": "a" * 64, "text": "Reply",
                "text_hash": hashlib.sha256(b"Reply").hexdigest(), "source_in_us": 0, "source_out_us": 500000,
                "evidence_ref": {"artifact_id": "evidence", "revision": 1},
                "words": [{"id": "source-word", "text": "Reply", "start_us": 0, "end_us": 500000, "start_char": 0, "end_char": 5}]}}
    command = _command({"artifact_id": "plan-1", "revision": 1}, kind="insert_turn", target_id="reply",
                       payload={"beat_id": "intro", "index": 1, "turn": turn})
    assert TurnV2.model_validate(turn)
    assert validate_production_command(command) == command
    # A real insert with native defaults must remain effective, not just validate.
    store = _store(tmp_path)
    saved = store.create("production_plan.v2", _plan(), [])
    command["plan_ref"] = {"artifact_id": saved["id"], "revision": 1}
    command["payload"]["turn"] = {"id": "reply", "text": "Reply", "start_frame": 0, "end_frame": 12, "timing_basis": "manual", "words": []}
    apply_production_command(store, command)
    inserted = store.read_latest(saved["id"])["payload"]["beats"][0]["turns"][1]
    assert inserted["speaker"] == ""
    assert inserted["timing_basis"] == "manual"
    assert inserted["end_frame"] == 12


def test_edit_turn_on_saved_outline_keeps_stage_and_ids(tmp_path: Path) -> None:
    from novelvideo.freezone.remix.production_plan_draft_stages import outline_to_plan, outline_from_plan
    plan = _plan()
    plan['placements'] = []
    outline = {'output': plan['output'], 'beats': plan['beats']}
    store = _store(tmp_path)
    saved = store.create('production_plan.v2', outline_to_plan('结构', outline), [])
    apply_production_command(store, _command(
        {'artifact_id': saved['id'], 'revision': 1}, kind='edit_turn', target_id='line',
        payload={'text': '用户修改后的台词'}))
    current = store.read_latest(saved['id'])['payload']
    assert current['authoring_stage'] == 'outline'
    reloaded = outline_from_plan(current)
    assert reloaded['beats'][0]['turns'][0]['id'] == 'line'
    assert reloaded['beats'][0]['turns'][0]['text'] == '用户修改后的台词'
