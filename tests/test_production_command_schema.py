"""Native and isolated Hermes tools must accept the same command shapes."""

from __future__ import annotations

import json
import importlib.util
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from novelvideo.freezone.remix.production_commands import validate_production_command

def production_command_json_schema():
    module_name = "novelvideo.freezone.remix.production_command_schema"
    assert importlib.util.find_spec(module_name) is not None, "native production command schema is missing"
    from novelvideo.freezone.remix.production_command_schema import production_command_json_schema as build
    return build()


REF = {"artifact_id": "plan-1", "revision": 1}
ANCHOR = {"kind": "beat", "ref_id": "intro", "edge": "start"}
END = {"kind": "beat", "ref_id": "intro", "edge": "end"}


def command(kind, payload, target_id="intro"):
    return {"command_id": "edit-1", "plan_ref": REF, "kind": kind,
            "target_id": target_id, "payload": payload}


CASES = [
    ("set_goal", {"goal": "Tell a story"}),
    ("insert_beat", {"beat": {"id": "new", "purpose": "Opening", "duration_frames": 24}, "index": 0}),
    ("insert_turn", {"beat_id": "intro", "turn": {"id": "new", "text": "Hello"}, "index": 0}),
    ("reorder_beats", {"ids": ["intro"]}),
    ("reorder_turns", {"ids": ["turn-1"]}),
    ("edit_beat", {"purpose": "Opening"}),
    ("edit_turn", {"text": "Hello"}),
    ("bind_media", {"resource_id": "media-1"}),
    ("bind_speech_source", {"placement_id": "voice", "turn_ids": ["turn-1"]}),
    ("reassign_speech_source", {"placement_id": "picture", "turn_ids": ["turn-1", "turn-2"]}),
    ("reassign_speech_source", {"placement_id": None, "turn_ids": ["turn-1", "turn-2"]}),
    ("upsert_placement", {"placement": {"id": "shot", "kind": "visual", "role": "primary",
                                             "track_id": "visual", "start": ANCHOR, "end": END}}, "shot"),
    ("remove_entity", {"entity_kind": "turn"}),
    ("set_output", {"output": {"width": 1080, "height": 1920, "fps_num": 24, "fps_den": 1}}),
    ("batch", {"commands": [{"kind": "edit_turn", "target_id": "turn-1", "payload": {"text": "Hello"}}]}),
]


@pytest.mark.parametrize("case", CASES)
def test_every_command_minimal_shape_matches_native_and_mcp(case):
    from novelvideo.freezone.remix.production_command_schema import structural_command_errors

    kind, payload, *target = case
    value = command(kind, payload, target[0] if target else "intro")
    validate_production_command(value)
    Draft202012Validator(production_command_json_schema()).validate(value)
    assert structural_command_errors(value) == []


@pytest.mark.parametrize("bad", [
    command("upsert_placement", {"id": "shot", "kind": "visual", "role": "primary",
                                  "track_id": "visual", "start": ANCHOR, "end": END}),
    command("upsert_placement", {"placement": {"id": "shot", "kind": "graphic", "role": "effect",
                                                 "track_id": "graphic", "start": ANCHOR, "end": END}}, "shot"),
    command("upsert_placement", {"placement": {"id": "shot", "kind": "visual", "role": "primary",
                                                 "track_id": "visual", "start": {"kind": "beat", "frame": 0},
                                                 "end": END}}, "shot"),
    command("batch", {"commands": [{"kind": "edit_turn", "payload": {"text": "Hello"}}]}),
    command("batch", {"commands": [{"kind": "batch", "target_id": "intro", "payload": {"commands": []}}]}),
    command("edit_beat", {"purpose": "Opening", "unexpected": True}),
    command("reassign_speech_source", {"placement_id": "picture", "turn_ids": ["turn-1"]}),
])
def test_invalid_structure_fails_both_surfaces(bad):
    with pytest.raises(ValueError):
        validate_production_command(bad)
    assert not Draft202012Validator(production_command_json_schema()).is_valid(bad)


def test_optional_native_turn_speech_fields_and_generated_artifact_match():
    value = command("insert_turn", {"beat_id": "intro", "index": 0,
                                    "turn": {"id": "new", "text": "Hello", "speaker": "Host",
                                             "speech_placement_id": "voice", "timing_basis": "estimated"}})
    validate_production_command(value)
    schema = production_command_json_schema()
    Draft202012Validator(schema).validate(value)
    artifact = Path(__file__).resolve().parents[1] / ".hermes/plugins/freezone/production_command_schema.json"
    assert json.loads(artifact.read_text()) == schema


@pytest.mark.parametrize("value,path,branch,missing", [
    (command("batch", {"commands": [{"kind": "insert_turn", "target_id": "new",
                                      "payload": {"beat_id": "intro", "index": 0}}]}),
     "payload.commands[0].payload.turn", "insert_turn", ["turn"]),
    ({**command("edit_turn", {"text": "Hello"}), "plan_ref": {"artifact_id": "plan-1", "revision": 0}},
     "plan_ref.revision", "edit_turn", []),
    ({**command("edit_turn", {"text": "Hello"}), "plan_ref": {"artifact_id": "bad id", "revision": 1}},
     "plan_ref.artifact_id", "edit_turn", []),
    ({**command("edit_turn", {"text": "Hello"}), "command_id": "bad id"},
     "command_id", "edit_turn", []),
])
def test_error_details_select_deepest_command_and_validate_envelope_first(value, path, branch, missing):
    from novelvideo.freezone.remix.production_command_schema import production_command_error_details

    assert production_command_error_details(value) == {
        "error_path": path, "expected_branch": branch, "missing_fields": missing,
    }
