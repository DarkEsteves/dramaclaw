"""Generation dialogue is derived from the saved production script."""

import pytest

from novelvideo.freezone.remix.production_dialogue import generation_dialogue
from novelvideo.freezone.remix.material_work import material_workflow_intent, prepare_material_work
from novelvideo.freezone.remix.store import RemixArtifactStore
from novelvideo.freezone.agent_workflows.catalog import compile_workflow_intent


def _plan():
    return {"beats": [{"id": "b", "turns": [
        {"id": "t1", "speaker": "甲", "text": "先看这一段。"},
        {"id": "t2", "speaker": "乙", "text": "再看下一段。"},
        {"id": "t3", "speaker": "甲", "text": "最后一次。"},
    ]}]}


def test_generation_dialogue_uses_saved_script_order_and_exact_text():
    assert generation_dialogue(_plan(), ["t1", "t2"]) == [
        {"turn_id": "t1", "speaker": "甲", "text": "先看这一段。"},
        {"turn_id": "t2", "speaker": "乙", "text": "再看下一段。"},
    ]
    assert generation_dialogue(_plan(), []) == []


@pytest.mark.parametrize("requested", [["t2", "t1"], ["t1", "t1"], ["unknown"]])
def test_dialogue_rejects_wrong_order_duplicates_and_missing_turns(requested):
    with pytest.raises(ValueError, match="turn"):
        generation_dialogue(_plan(), requested)


def test_material_generation_injects_exact_saved_dialogue(tmp_path):
    store = RemixArtifactStore(tmp_path, project_id="p", created_by="alice")
    plan = store.create("production_plan.v2", {
        "schema_version": 2, "goal": "对话", "output": {
            "width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1},
        "beats": [{"id": "b", "purpose": "对白", "duration_frames": 90,
                   "turns": _plan()["beats"][0]["turns"]}],
        "resources": [], "placements": [], "provenance": {},
    }, [])
    ref = {"artifact_id": plan["id"], "revision": 1}
    requirements = [{"id": "r", "turn_ids": ["t1", "t2"], "description": "两句对白",
                     "media_kind": "video", "final_step_id": "clip"}]
    steps = [{"id": "clip", "label": "对话镜头", "media_kind": "video",
              "source": "generate", "recipe_id": "general-video", "prompt": "咖啡店里对话",
              "dialogue_turn_ids": ["t1", "t2"]}]
    work_ref = prepare_material_work(store, ref, requirements, steps, "dialogue-work")["work_ref"]
    work = store.read(work_ref["artifact_id"], 1)["payload"]
    intent = material_workflow_intent(work, store=store)
    assert intent["items"][0]["dialogue"] == "甲：先看这一段。\n乙：再看下一段。"
    assert intent["items"][0]["prompt"] == "咖啡店里对话"
    compiled = compile_workflow_intent(intent)
    assert compiled["ok"] is True
    assert any("先看这一段。" in str(node.get("data")) for node in compiled["plan"]["nodes"])
