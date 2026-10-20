"""Script ranges are editable before media and retain their source identity."""

from hashlib import sha256

import pytest

from novelvideo.freezone.remix.authored_text_timing import (
    locate_text_boundary,
    make_text_anchor,
    materialize_text_anchors,
    rebase_text_anchor,
)
from novelvideo.freezone.remix.production_commands import apply_production_command
from novelvideo.freezone.remix.production_plan_v2 import ProductionPlanV2, compile_production_plan_v2, project_production_plan_v2
from novelvideo.freezone.remix.store import RemixArtifactStore


def _plan() -> dict:
    return {"schema_version": 2, "goal": "按内容安排镜头",
            "output": {"width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1},
            "beats": [{"id": "intro", "purpose": "开场", "duration_frames": 90,
                       "turns": [{"id": "line", "speaker": "甲", "text": "甲🙂乙"}]}],
            "resources": [], "placements": [], "provenance": {}}


def _selection() -> dict:
    return {"id": "one-word", "label": "表情后出现", "start": {
                "kind": "text", "ref_id": "line", "char_offset": 2, "affinity": "after"},
            "end": {"kind": "text", "ref_id": "line", "char_offset": 3,
                    "affinity": "before"}}


def test_text_anchor_uses_unicode_code_points_without_a_time_or_media() -> None:
    anchor = make_text_anchor("甲🙂乙", "line", 2)
    assert anchor == {"kind": "text", "ref_id": "line", "char_offset": 2,
                      "text_hash": sha256("甲🙂乙".encode("utf-8")).hexdigest(),
                      "affinity": "after", "offset_frames": 0}
    assert "frame" not in anchor and "edge" not in anchor
    with pytest.raises(ValueError, match="char_offset"):
        make_text_anchor("甲🙂乙", "line", 4)


def test_saved_plan_materializes_text_hash_and_stays_valid_without_media(tmp_path) -> None:
    plan = _plan()
    plan["selections"] = [_selection()]
    normalized = materialize_text_anchors(plan)
    expected = sha256("甲🙂乙".encode("utf-8")).hexdigest()
    assert normalized["selections"][0]["start"]["text_hash"] == expected
    assert normalized["selections"][0]["end"]["text_hash"] == expected
    assert "text_hash" not in plan["selections"][0]["start"]
    store = RemixArtifactStore(tmp_path / "alice", project_id="alice", created_by="alice")
    saved = store.create("production_plan.v2", normalized, [])
    assert ProductionPlanV2.model_validate(saved["payload"]).selections[0].start.char_offset == 2


def test_initial_artifact_create_stamps_text_hash_from_author_script(tmp_path) -> None:
    plan = _plan()
    plan["selections"] = [_selection()]
    store = RemixArtifactStore(tmp_path / "alice", project_id="alice", created_by="alice")
    saved = store.create("production_plan.v2", plan, [])
    assert saved["payload"]["selections"][0]["start"]["text_hash"] == sha256("甲🙂乙".encode()).hexdigest()


def test_selection_command_reuses_durable_receipt_and_rejects_wrong_hash(tmp_path) -> None:
    store = RemixArtifactStore(tmp_path / "alice", project_id="alice", created_by="alice")
    saved = store.create("production_plan.v2", _plan(), [])
    command = {"command_id": "selection-one", "plan_ref": {"artifact_id": saved["id"], "revision": 1},
               "kind": "upsert_selection", "target_id": "one-word",
               "payload": {"selection": _selection()}}
    first = apply_production_command(store, command)
    assert apply_production_command(store, command) == first
    assert store.read_latest(saved["id"])["payload"]["selections"][0]["start"]["text_hash"] == sha256("甲🙂乙".encode()).hexdigest()
    bad = _selection()
    bad["start"]["text_hash"] = "0" * 64
    with pytest.raises(ValueError, match="text_hash"):
        apply_production_command(store, {**command, "command_id": "selection-bad",
            "plan_ref": first["plan_ref"], "payload": {"selection": bad}})
    assert store.read_latest(saved["id"])["revision"] == 2


def test_placement_command_stamps_exact_author_text_and_rejects_stale_hash(tmp_path) -> None:
    store = RemixArtifactStore(tmp_path / "alice", project_id="alice", created_by="alice")
    saved = store.create("production_plan.v2", _plan(), [])
    placement = {"id": "picture", "kind": "visual", "role": "overlay", "track_id": "visual:overlay",
                 "start": {"kind": "text", "ref_id": "line", "char_offset": 0, "affinity": "after"},
                 "end": {"kind": "text", "ref_id": "line", "char_offset": 3, "affinity": "before"}}
    ref = {"artifact_id": saved["id"], "revision": 1}
    applied = apply_production_command(store, {"command_id": "picture-first", "plan_ref": ref,
        "kind": "upsert_placement", "target_id": "picture", "payload": {"placement": placement}})
    stored = store.read_latest(saved["id"])["payload"]["placements"][0]
    expected = sha256("甲🙂乙".encode()).hexdigest()
    assert stored["start"]["text_hash"] == expected
    assert stored["end"]["text_hash"] == expected
    bad = {**placement, "end": {**placement["end"], "text_hash": "0" * 64}}
    with pytest.raises(ValueError, match="text_hash"):
        apply_production_command(store, {"command_id": "picture-stale", "plan_ref": applied["plan_ref"],
            "kind": "upsert_placement", "target_id": "picture", "payload": {"placement": bad}})


def test_text_boundary_uses_real_neighbor_times_on_each_side_of_pause() -> None:
    words = [
        {"id": "w1", "text": "甲", "start_char": 0, "end_char": 1,
         "start_us": 100_000, "end_us": 300_000},
        {"id": "w2", "text": "乙", "start_char": 1, "end_char": 2,
         "start_us": 600_000, "end_us": 900_000},
    ]
    before = locate_text_boundary(text="甲乙", char_offset=1, affinity="before", words=words)
    after = locate_text_boundary(text="甲乙", char_offset=1, affinity="after", words=words)
    assert before == {"source_us": 300_000, "quality": "measured",
                      "source_word_ids": ["w1"], "issue_code": None}
    assert after == {"source_us": 600_000, "quality": "measured",
                     "source_word_ids": ["w2"], "issue_code": None}


def test_sentence_cue_does_not_invent_word_timing() -> None:
    start = locate_text_boundary(text="每天一杯", char_offset=0, affinity="after", words=[],
                                 cue_start_us=200_000, cue_end_us=1_000_000)
    middle = locate_text_boundary(text="每天一杯", char_offset=2, affinity="after", words=[],
                                  cue_start_us=200_000, cue_end_us=1_000_000)
    assert start["source_us"] == 200_000 and start["quality"] == "manual"
    assert middle["source_us"] is None and middle["issue_code"] == "missing_word_evidence"


def test_srt_sentence_edges_project_without_word_timing(tmp_path) -> None:
    plan = _plan()
    plan["beats"][0]["turns"][0]["text"] = "每天一杯"
    plan["resources"] = [{"id": "media", "node_id": "n", "media_kind": "audio",
                          "media_path": "voice.wav", "media_hash": "a" * 64,
                          "duration_us": 2_000_000, "has_audio": True}]
    plan["placements"] = [{"id": "voice", "kind": "audio", "role": "speech",
                           "track_id": "speech", "resource_id": "media",
                           "start": {"kind": "beat", "ref_id": "intro", "edge": "start"},
                           "end": {"kind": "beat", "ref_id": "intro", "edge": "end"}}]
    plan["speech_takes"] = [{"id": "take", "beat_id": "intro", "resource_id": "media",
                             "origin_placement_id": "voice", "turn_ids": ["line"]}]
    plan["speech_cues"] = [{"id": "cue", "turn_id": "line", "take_id": "take",
                            "text": "每天一杯", "source_start_us": 200_000,
                            "source_end_us": 1_000_000, "timing_source": "srt_import",
                            "timing_status": "ready", "evidence_ref": {"artifact_id": "srt", "revision": 1}}]
    plan["selections"] = [{"id": "whole", "label": "全句",
                           "start": make_text_anchor("每天一杯", "line", 0),
                           "end": make_text_anchor("每天一杯", "line", 4, affinity="before")},
                          {"id": "inside", "label": "句中",
                           "start": make_text_anchor("每天一杯", "line", 1),
                           "end": make_text_anchor("每天一杯", "line", 3)}]
    rows = project_production_plan_v2(plan, project_dir=tmp_path)["selections"]
    assert rows[0]["location_status"] == "resolved"
    assert (rows[0]["start_frame"], rows[0]["end_frame"]) == (6, 30)
    assert rows[0]["timing_quality"] == "measured"
    assert rows[1]["location_status"] == "unresolved"
    plan["selections"] = []
    plan["placements"].append({"id": "caption", "kind": "subtitle", "role": "dialogue",
                               "track_id": "subtitles", "text": "每天一杯",
                               "start": make_text_anchor("每天一杯", "line", 0),
                               "end": make_text_anchor("每天一杯", "line", 4, affinity="before")})
    manifest = compile_production_plan_v2(plan)
    assert [(event["start_frame"], event["end_frame"]) for event in manifest["subtitle_events"]] == [(6, 30)]


def test_inside_word_is_only_estimated_and_missing_neighbor_is_unresolved() -> None:
    word = {"id": "w", "text": "coffee", "start_char": 0, "end_char": 6,
            "start_us": 100_000, "end_us": 700_000}
    inside = locate_text_boundary(text="coffee", char_offset=3, affinity="after", words=[word])
    assert inside["source_us"] == 400_000 and inside["quality"] == "estimated"
    missing = locate_text_boundary(text="coffee now", char_offset=7, affinity="after", words=[word])
    assert missing["source_us"] is None and missing["issue_code"] == "missing_word_evidence"


def test_text_selection_follows_adopted_speech_evidence_and_preserves_pause(tmp_path) -> None:
    plan = _plan()
    plan["beats"][0]["turns"][0]["text"] = "甲乙"
    plan["beats"][0]["turns"][0]["speech_placement_id"] = "voice"
    plan["beats"][0]["turns"][0]["speech_timing"] = {
        "placement_id": "voice", "media_hash": "a" * 64, "text": "甲乙",
        "text_hash": sha256("甲乙".encode()).hexdigest(),
        "evidence_ref": {"artifact_id": "speech", "revision": 1},
        "words": [
            {"id": "w1", "text": "甲", "start_char": 0, "end_char": 1,
             "start_us": 100_000, "end_us": 300_000},
            {"id": "w2", "text": "乙", "start_char": 1, "end_char": 2,
             "start_us": 600_000, "end_us": 900_000},
        ],
    }
    plan["resources"] = [{"id": "media", "node_id": "n", "media_kind": "audio",
                          "media_path": "voice.wav", "media_hash": "a" * 64,
                          "duration_us": 1_000_000, "has_audio": True}]
    plan["placements"] = [{"id": "voice", "kind": "audio", "role": "speech",
                           "track_id": "speech", "resource_id": "media",
                           "start": {"kind": "beat", "ref_id": "intro", "edge": "start"},
                           "end": {"kind": "beat", "ref_id": "intro", "edge": "end"}}]
    plan["selections"] = [{"id": "pause", "label": "停顿",
                           "start": make_text_anchor("甲乙", "line", 1, affinity="before"),
                           "end": make_text_anchor("甲乙", "line", 1, affinity="after")}]
    row = project_production_plan_v2(plan, project_dir=tmp_path)["selections"][0]
    assert (row["start_frame"], row["end_frame"]) == (9, 18)
    assert row["timing_quality"] == "measured"


def test_text_edit_rebases_only_provably_unchanged_boundary() -> None:
    anchor = make_text_anchor("甲乙丙", "line", 1)
    before = rebase_text_anchor(anchor, old_text="甲乙丙", new_text="新甲乙丙")
    assert before["status"] == "preserved"
    assert before["anchor"]["char_offset"] == 2
    assert before["anchor"]["text_hash"] == sha256("新甲乙丙".encode()).hexdigest()
    changed = rebase_text_anchor(anchor, old_text="甲乙丙", new_text="甲丁丙")
    assert changed["status"] == "needs_rebind"
    assert changed["anchor"] == anchor


def test_public_turn_edit_preserves_stable_text_selection_and_flags_changed_one(tmp_path) -> None:
    store = RemixArtifactStore(tmp_path / "alice", project_id="alice", created_by="alice")
    plan = _plan()
    plan["beats"][0]["turns"][0]["text"] = "甲乙丙"
    plan["selections"] = [{"id": "stable", "label": "乙",
                           "start": make_text_anchor("甲乙丙", "line", 1),
                           "end": make_text_anchor("甲乙丙", "line", 2)}]
    saved = store.create("production_plan.v2", plan, [])
    first = apply_production_command(store, {
        "command_id": "insert-prefix", "plan_ref": {"artifact_id": saved["id"], "revision": 1},
        "kind": "edit_turn", "target_id": "line", "payload": {"text": "新甲乙丙"},
    })
    stable = store.read_latest(saved["id"])["payload"]["selections"][0]
    assert stable["start"]["char_offset"] == 2
    assert stable["end"]["char_offset"] == 3
    assert stable["start"]["text_hash"] == sha256("新甲乙丙".encode()).hexdigest()
    second = apply_production_command(store, {
        "command_id": "change-target", "plan_ref": first["plan_ref"],
        "kind": "edit_turn", "target_id": "line", "payload": {"text": "新甲丁丙"},
    })
    stale = store.read_latest(saved["id"])["payload"]["selections"][0]
    assert stale["start"]["text_hash"] == sha256("新甲乙丙".encode()).hexdigest()
    assert "text_anchor_requires_rebind" in second["blockers"]
