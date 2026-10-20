"""Progress reports a real pending plan version instead of a false conflict."""
# ruff: noqa: F401, F811 -- imported fixtures are required by pytest.

from __future__ import annotations

from test_plan_confirmation_contract import reviewed_plan
from test_reference_director_api import setup_api
from novelvideo.freezone import canvas_store
from novelvideo.freezone.remix.production_commands import apply_production_command
from novelvideo.freezone.remix.production_inventory import _current_plan, read_current_production


def test_pending_canvas_and_commission_revision_are_readable(reviewed_plan):
    _, store, commission, _ = reviewed_plan
    ref = commission["plan_ref"]
    canvas = canvas_store.read_canvas(store.project_dir, "c")
    node = next(node for node in canvas["nodes"] if node.get("data", {}).get("productionPlanV2Ref"))
    node["data"]["productionPlanV2Ref"]["revision"] = ref["revision"]
    canvas_store.save_canvas(store.project_dir, "c", base_revision=canvas["revision"], build_payload=lambda _: canvas)
    updated = apply_production_command(store, {
        "command_id": "pending-inventory-edit", "plan_ref": {key: ref[key] for key in ("artifact_id", "revision")},
        "kind": "edit_beat", "target_id": "intro", "payload": {"purpose": "新反转"},
    })
    canvas = canvas_store.read_canvas(store.project_dir, "c")
    pending = _current_plan(store, commission, canvas)
    assert pending["revision"] == ref["revision"]
    assert pending["plan_revision_sync"]["status"] == "pending_canvas_save"
    progress = read_current_production(
        store, canvas=canvas, canvas_id="c", agent_id="main", requester_user_id="u",
        runs=[], state_dir=store.project_dir,
        commission_id=commission["commission_ref"]["artifact_id"],
    )
    assert progress["plan_revision_sync"]["status"] == "pending_canvas_save"
    node = next(node for node in canvas["nodes"] if node.get("data", {}).get("productionPlanV2Ref"))
    node["data"]["productionPlanV2Ref"]["revision"] = updated["plan_ref"]["revision"]
    canvas_store.save_canvas(store.project_dir, "c", base_revision=canvas["revision"], build_payload=lambda _: canvas)
    pending = _current_plan(store, commission, canvas_store.read_canvas(store.project_dir, "c"))
    assert pending["revision"] == updated["plan_ref"]["revision"]
    assert pending["plan_revision_sync"]["status"] == "pending_commission_sync"
