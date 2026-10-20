"""Selecting local material work does not expand to unrelated production beats."""

from novelvideo.freezone.remix.material_generation_scope import select_material_generation_steps
from novelvideo.freezone.remix.material_work import material_workflow_intent


def test_selected_placement_includes_shared_reference_once_and_reuses_accepted():
    work = {"requirements": [
        {"id": "a", "placement_ids": ["pa"], "final_step_id": "clip-a"},
        {"id": "b", "placement_ids": ["pb"], "final_step_id": "clip-b"},
    ], "steps": [
        {"id": "character", "source": "generate", "status": "accepted"},
        {"id": "clip-a", "source": "generate", "status": "planned", "depends_on": ["character"]},
        {"id": "clip-b", "source": "generate", "status": "planned", "depends_on": ["character"]},
    ]}
    scoped = select_material_generation_steps(work, placement_ids=["pa"])
    assert scoped["selected"] == ["clip-a"]
    assert scoped["required_dependencies"] == ["character"]
    assert scoped["reusable"] == ["character"]
    assert scoped["ready"] == ["clip-a"]
    assert "clip-b" not in {value for values in scoped.values() for value in values}


def test_waiting_unknown_and_rejected_steps_are_not_resubmitted():
    work = {"requirements": [{"id": "a", "placement_ids": ["a"], "final_step_id": "a"},
                              {"id": "b", "placement_ids": ["b"], "final_step_id": "b"},
                              {"id": "c", "placement_ids": ["c"], "final_step_id": "c"}],
            "steps": [{"id": "a", "status": "submitted", "source": "generate", "task_id": "job"},
                      {"id": "b", "status": "submitted", "source": "generate", "execution": {"status": "unknown"}},
                      {"id": "c", "status": "rejected", "source": "generate"}]}
    scoped = select_material_generation_steps(work, placement_ids=None)
    assert scoped["waiting"] == ["a"]
    assert scoped["reconcile"] == ["b"]
    assert scoped["blocked"] == ["c"]
    assert scoped["ready"] == []


def test_selected_placement_compiles_only_its_ready_native_material_step():
    work = {"requirements": [
        {"id": "a", "placement_ids": ["pa"], "final_step_id": "clip-a"},
        {"id": "b", "placement_ids": ["pb"], "final_step_id": "clip-b"},
    ], "steps": [
        {"id": "clip-a", "source": "generate", "status": "planned", "media_kind": "video",
         "recipe_id": "general-video", "prompt": "A", "label": "A", "references": []},
        {"id": "clip-b", "source": "generate", "status": "planned", "media_kind": "video",
         "recipe_id": "general-video", "prompt": "B", "label": "B", "references": []},
    ]}
    scope = select_material_generation_steps(work, placement_ids=["pa"])
    intent = material_workflow_intent(work, selected_step_ids=scope["ready"])
    assert [item["id"] for item in intent["items"]] == ["clip-a"]


def test_explicit_regenerate_does_not_submit_an_accepted_step_without_new_attempt():
    work = {"requirements": [{"id": "a", "placement_ids": ["pa"], "final_step_id": "a"}],
            "steps": [{"id": "a", "source": "generate", "status": "accepted"}]}
    scope = select_material_generation_steps(work, placement_ids=["pa"], regenerate=True)
    assert scope["ready"] == []
    assert scope["retry_required"] == ["a"]
