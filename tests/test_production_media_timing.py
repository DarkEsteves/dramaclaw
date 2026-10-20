"""A silent passage can follow its selected media without fabricated speech."""

from copy import deepcopy

import pytest

from novelvideo.freezone.remix.production_plan_v2 import ProductionPlanV2, compile_production_plan_v2
from novelvideo.freezone.remix.production_timing import project_speech_mainline


def _silent_plan() -> dict:
    return {
        "schema_version": 2,
        "goal": "无台词动作接固定画面",
        "output": {"width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1},
        "beats": [
            {"id": "action", "purpose": "走进房间", "duration_frames": 150,
             "duration_source_placement_id": "action-video", "turns": []},
            {"id": "title", "purpose": "片尾文字", "duration_frames": 60,
             "timing_mode": "fixed", "turns": []},
        ],
        "resources": [
            {"id": "clip", "node_id": "clip-node", "media_kind": "video",
             "media_path": "media/action.mp4", "media_hash": "a" * 64,
             "duration_us": 7_200_000, "has_audio": False},
            {"id": "still", "node_id": "still-node", "media_kind": "image",
             "media_path": "media/still.png", "media_hash": "b" * 64,
             "duration_us": 0},
        ],
        "placements": [
            {"id": "action-video", "kind": "visual", "role": "primary", "track_id": "main",
             "resource_id": "clip", "start": {"kind": "beat", "ref_id": "action", "edge": "start"},
             "end": {"kind": "beat", "ref_id": "action", "edge": "end"}},
            {"id": "title-still", "kind": "visual", "role": "primary", "track_id": "main",
             "resource_id": "still", "start": {"kind": "beat", "ref_id": "title", "edge": "start"},
             "end": {"kind": "beat", "ref_id": "title", "edge": "end"}},
        ],
        "provenance": {},
    }


def test_silent_media_source_sets_beat_duration_and_shifts_later_content() -> None:
    plan = _silent_plan()
    projected = project_speech_mainline(ProductionPlanV2.model_validate(plan))
    assert projected["beat_ranges"] == {"action": (0, 216), "title": (216, 276)}
    manifest = compile_production_plan_v2(plan)
    assert manifest["output_spec"]["duration_frames"] == 276
    assert [(row["start_frame"], row["end_frame"]) for row in manifest["layers"]] == [
        (0, 216), (216, 276),
    ]


def test_source_trim_and_speed_drive_only_selected_silent_beat() -> None:
    plan = _silent_plan()
    plan["placements"][0].update(source_in_us=1_000_000, source_out_us=5_000_000,
                                  speed_num=2, speed_den=1)
    projected = project_speech_mainline(ProductionPlanV2.model_validate(plan))
    assert projected["beat_ranges"] == {"action": (0, 60), "title": (60, 120)}
    previous = deepcopy(plan)
    previous["beats"][0].pop("duration_source_placement_id")
    assert project_speech_mainline(ProductionPlanV2.model_validate(previous))["beat_ranges"] == {
        "action": (0, 150), "title": (150, 210),
    }


@pytest.mark.parametrize("change", [
    ("beats", 0, {"timing_mode": "fixed"}),
    ("beats", 0, {"duration_source_placement_id": "title-still"}),
    ("placements", 0, {"loop": True}),
])
def test_invalid_silent_media_clock_is_rejected(change) -> None:
    plan = _silent_plan()
    field, index, values = change
    plan[field][index].update(values)
    with pytest.raises(ValueError, match="duration source"):
        ProductionPlanV2.model_validate(plan)
