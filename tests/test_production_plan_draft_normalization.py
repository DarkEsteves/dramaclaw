"""A model draft may use only unambiguous, authorable shorthand."""

from __future__ import annotations

from copy import deepcopy

import pytest

from novelvideo.freezone.remix.production_plan_v2 import (
    ProductionPlanV2, resolve_production_placement_timing,
)
from test_production_plan import native_plan


def normalize(payload):
    from novelvideo.freezone.remix.production_plan_draft_normalization import (
        normalize_production_plan_draft,
    )
    return normalize_production_plan_draft(payload)


def test_explicit_local_offsets_resolve_without_double_counting_end():
    payload = native_plan()
    row = payload["placements"][0]
    row["start"] = {"kind": "beat", "ref_id": "hook", "offset_frames": 12}
    row["end"] = {"kind": "beat", "ref_id": "hook", "offset_frames": 72}
    original = deepcopy(payload)

    normalized, changes = normalize(payload)
    duration, rows = resolve_production_placement_timing(normalized)
    assert duration == 90
    assert (rows[0]["start_frame"], rows[0]["end_frame"]) == (12, 72)
    assert payload == original
    assert {row["code"] for row in changes} == {"beat_anchor_start_defaulted"}
    assert len(changes) == 2
    assert normalize(normalized) == (normalized, [])


def test_cross_beat_offsets_resolve_from_each_beats_start():
    payload = native_plan()
    payload["beats"].append({"id": "finish", "purpose": "收束", "duration_frames": 60})
    row = payload["placements"][0]
    row["start"] = {"kind": "beat", "ref_id": "hook", "offset_frames": 30}
    row["end"] = {"kind": "beat", "ref_id": "finish", "offset_frames": 60}

    normalized, _ = normalize(payload)
    duration, rows = resolve_production_placement_timing(normalized)
    assert duration == 150
    assert (rows[0]["start_frame"], rows[0]["end_frame"]) == (30, 150)


def test_explicit_end_edge_and_negative_offset_are_preserved():
    payload = native_plan()
    payload["placements"][0]["end"] = {
        "kind": "beat", "ref_id": "hook", "edge": "end", "offset_frames": -12,
    }
    assert normalize(payload) == (payload, [])


@pytest.mark.parametrize("anchor", [
    {"kind": "beat", "ref_id": "hook"},
    {"kind": "beat", "ref_id": "hook", "offset_frames": -1},
    {"kind": "beat", "ref_id": "hook", "offset_frames": 91},
    {"kind": "beat", "ref_id": "hook", "offset_frames": True},
    {"kind": "beat", "ref_id": "missing", "offset_frames": 10},
    {"kind": "turn", "ref_id": "t1", "offset_frames": 10},
])
def test_ambiguous_or_invalid_anchor_is_never_guessed(anchor):
    payload = native_plan()
    payload["placements"][0]["start"] = anchor
    normalized, changes = normalize(payload)
    assert normalized["placements"][0]["start"] == anchor
    assert changes == []


@pytest.mark.parametrize("role", [None, "primary", "bgm"])
def test_subtitle_role_is_derived_from_kind_when_content_is_clear(role):
    payload = native_plan()
    subtitle = {"id": "s1", "kind": "subtitle", "track_id": "subtitles",
                "text": "字幕", "start": payload["placements"][0]["start"],
                "end": payload["placements"][0]["end"]}
    if role is not None:
        subtitle["role"] = role
    payload["placements"].append(subtitle)

    normalized, changes = normalize(payload)
    assert normalized["placements"][1]["role"] == "dialogue"
    assert changes == [{"path": "/placements/1/role", "code": "subtitle_role_defaulted",
                        "placement_id": "s1"}]
    ProductionPlanV2.model_validate(normalized)


@pytest.mark.parametrize("patch", [
    {"role": "unknown"},
    {"text": ""},
    {"resource_id": "invented"},
    {"graphic": {"template": "keyword", "text": "冲突"}},
])
def test_conflicting_or_empty_subtitle_is_not_repaired(patch):
    payload = native_plan()
    row = {"id": "s1", "kind": "subtitle", "role": "primary",
           "track_id": "subtitles", "text": "字幕",
           "start": payload["placements"][0]["start"],
           "end": payload["placements"][0]["end"]}
    row.update(patch)
    payload["placements"].append(row)
    normalized, changes = normalize(payload)
    assert normalized["placements"][1]["role"] == row["role"]
    assert changes == []


def test_malformed_draft_is_left_for_schema_validation_without_preprocessor_crash():
    payload = {"beats": None, "placements": [{"kind": "subtitle", "role": ["primary"],
                                              "text": "字幕"}]}
    assert normalize(payload) == (payload, [])


def test_duplicated_beat_id_cannot_choose_one_anchor_origin():
    payload = native_plan()
    payload["beats"].append({"id": "hook", "purpose": "重复", "duration_frames": 120})
    payload["placements"][0]["start"] = {"kind": "beat", "ref_id": "hook", "offset_frames": 10}
    normalized, changes = normalize(payload)
    assert normalized["placements"][0]["start"] == payload["placements"][0]["start"]
    assert changes == []


def test_estimated_out_of_beat_bounds_return_to_untimed_draft():
    payload = native_plan()
    payload["beats"][0]["turns"] = [
        {"id": "t1", "text": "第一句", "start_frame": 0, "end_frame": 30},
        {"id": "t2", "text": "第二句", "start_frame": 120, "end_frame": 150},
    ]
    original = deepcopy(payload)
    normalized, changes = normalize(payload)
    turns = ProductionPlanV2.model_validate(normalized).beats[0].turns
    assert [turn.text for turn in turns] == ["第一句", "第二句"]
    assert all(turn.start_frame is None and turn.end_frame is None for turn in turns)
    assert all(turn.timing_basis == "estimated" for turn in turns)
    assert [row["code"] for row in changes] == ["estimated_turn_bounds_cleared"] * 2
    assert payload == original
    assert normalize(normalized) == (normalized, [])


@pytest.mark.parametrize("change", [
    lambda p: p["beats"][0].update(timing_mode="fixed"),
    lambda p: p["beats"][0]["turns"][0].update(timing_basis="manual"),
    lambda p: p["placements"][0].update(start={"kind": "program_frame", "frame": 0}),
    lambda p: p["beats"][0]["turns"][1].update(start_frame=160, end_frame=150),
    lambda p: p["beats"][0]["turns"][1].update(start_frame=None),
])
def test_estimated_bounds_with_fixed_or_invalid_evidence_are_not_cleared(change):
    payload = native_plan()
    payload["beats"][0]["turns"] = [
        {"id": "t1", "text": "第一句", "start_frame": 0, "end_frame": 30},
        {"id": "t2", "text": "第二句", "start_frame": 120, "end_frame": 150},
    ]
    change(payload)
    normalized, changes = normalize(payload)
    assert normalized == payload
    assert changes == []
