"""A reference draft stays editable without paid retries or premature media binding."""

from __future__ import annotations

import json

import pytest

from novelvideo.freezone.remix import production_plan
from novelvideo.freezone.remix.production_commands import apply_production_command, preflight_production_command
from novelvideo.freezone.remix.production_plan_diagnostics import measure_plan_speech
from novelvideo.freezone.remix.store import RemixArtifactStore
from test_production_plan import native_plan


@pytest.mark.asyncio
async def test_reference_evidence_draft_estimate_and_confirmed_edit(monkeypatch, tmp_path):
    calls = []
    model_plan = native_plan()
    beat = model_plan["beats"][0]
    beat.pop("duration_frames")
    beat.update(speech_pace="normal", extra_pause_seconds=0.5,
                turns=[{"id": "line", "speaker": "受访者", "text": "看看你的手臂。",
                        "speech_placement_id": "hook-picture"}])
    model_plan["placements"][0].update(
        description="听到反问后切采访者；结尾受访者看向对方手臂，留出反应。",
        source_audio=True)

    async def fake_model(**kwargs):
        calls.append(kwargs)
        return json.dumps(model_plan, ensure_ascii=False)

    monkeypatch.setattr(production_plan, "generate_recipe_text", fake_model)
    blueprint = {"events": [
        {"event_id": "opening", "start_us": 0, "end_us": 2_000_000,
         "spoken_range": {"text": "你喝什么？"}, "model": {"visual_observation": "采访者举起杯子"}},
        {"event_id": "reaction", "start_us": 8_000_000, "end_us": 10_000_000,
         "model": {"visual_observation": "受访者看向采访者"}},
        {"event_id": "ending", "start_us": 16_000_000, "end_us": 18_000_000,
         "spoken_range": {"text": "看看你的手臂。"},
         "model": {"visual_observation": "受访者看向手臂"}},
    ]}
    assumptions = []
    draft = await production_plan.generate_production_plan_draft(
        topic="保持原片问答与反转，产品换黑咖啡", blueprint=blueprint,
        username="alice", requirements={}, timing_assumptions=assumptions)
    assert len(calls) == 1
    assert [row["event_id"] for row in calls[0]["structured_upstream"]["source_evidence"]["events"]] == [
        "opening", "reaction", "ending"]
    assert assumptions[0]["duration_basis"] == "local_speech_estimate"
    assert draft["placements"][0]["description"] == model_plan["placements"][0]["description"]
    estimate = next(row for row in measure_plan_speech(
        draft, local_timing_beat_ids={"hook"}) if row["entity_kind"] == "beat")
    assert estimate["estimate"]["duration_basis"] == "local_speech_estimate"
    assert estimate["estimate"]["padding_seconds"] == 0.5

    store = RemixArtifactStore(tmp_path / "alice", project_id="alice", created_by="alice")
    saved = store.create("production_plan.v2", draft, [])
    ref = {"artifact_id": saved["id"], "revision": 1}
    command = {"command_id": "change-rhythm", "plan_ref": ref, "kind": "edit_beat",
               "target_id": "hook", "payload": {"speech_pace": "fast", "extra_pause_seconds": 1}}
    before = store.read_latest(saved["id"])["payload"]
    assert preflight_production_command(store, command)["ok"]
    assert store.read_latest(saved["id"])["payload"] == before
    receipt = apply_production_command(store, command)
    assert receipt["plan_ref"]["revision"] == 2
    updated = store.read_latest(saved["id"])["payload"]
    assert updated["beats"][0]["duration_frames"] == before["beats"][0]["duration_frames"]
    assert updated["beats"][0]["speech_pace"] == "fast"
    assert len(calls) == 1

    unbound = {"command_id": "bind-unmade-video", "plan_ref": receipt["plan_ref"],
               "kind": "bind_speech_source", "target_id": "hook",
               "payload": {"placement_id": "hook-picture", "turn_ids": ["line"]}}
    issue = preflight_production_command(store, unbound)["issues"][0]
    assert issue["code"] == "speech_source_media_required"
    assert store.read_latest(saved["id"])["revision"] == 2
