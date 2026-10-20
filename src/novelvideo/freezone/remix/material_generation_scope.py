"""Pure selection and dependency closure for existing material work packages."""

from __future__ import annotations

from typing import Any


def select_material_generation_steps(
    work: dict[str, Any], *, placement_ids: list[str] | None, regenerate: bool = False,
) -> dict[str, list[str]]:
    steps = {step["id"]: step for step in work["steps"]}
    if len(steps) != len(work["steps"]):
        raise ValueError("material step IDs must be unique")
    known_placements = {placement for row in work["requirements"]
                        for placement in row.get("placement_ids", [])}
    if placement_ids is not None and (len(set(placement_ids)) != len(placement_ids)
                                      or set(placement_ids) - known_placements):
        raise ValueError("selected placement IDs must be unique saved material targets")
    chosen = set(placement_ids or [])
    selected = {row["final_step_id"] for row in work["requirements"]
                if placement_ids is None or chosen.intersection(row.get("placement_ids", []))}
    order: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(step_id: str) -> None:
        if step_id in visited:
            return
        if step_id not in steps or step_id in visiting:
            raise ValueError("material dependency is missing or cyclic")
        visiting.add(step_id)
        step = steps[step_id]
        for dependency in [*step.get("depends_on", []),
                           *(ref["step_id"] for ref in step.get("references", []) if ref.get("step_id"))]:
            visit(dependency)
        visiting.remove(step_id)
        visited.add(step_id)
        order.append(step_id)

    for step in work["steps"]:
        if step["id"] in selected:
            visit(step["id"])
    scope = {key: [] for key in ("selected", "required_dependencies", "ready", "waiting",
                               "reconcile", "reusable", "awaiting_upload", "blocked", "retry_required")}
    scope["selected"] = [step_id for step_id in order if step_id in selected]
    scope["required_dependencies"] = [step_id for step_id in order if step_id not in selected]
    accepted = {step_id for step_id in order if steps[step_id].get("status") == "accepted"
                and not (regenerate and step_id in selected)}
    for step_id in order:
        step = steps[step_id]
        status = step.get("status", "planned")
        dependencies = set(step.get("depends_on", [])) | {
            ref["step_id"] for ref in step.get("references", []) if ref.get("step_id")}
        if step_id in accepted:
            scope["reusable"].append(step_id)
        elif regenerate and step_id in selected and status in {"accepted", "rejected"}:
            # The old candidate remains evidence until retry_material_step
            # records a new attempt. Selection alone must never resubmit it.
            scope["retry_required"].append(step_id)
        elif status == "submitted":
            if (step.get("execution") or {}).get("status") == "unknown" or not step.get("task_id"):
                scope["reconcile"].append(step_id)
            else:
                scope["waiting"].append(step_id)
        elif status == "candidate":
            scope["waiting"].append(step_id)
        elif status == "awaiting_upload" or step.get("source") == "upload":
            scope["awaiting_upload"].append(step_id)
        elif status == "rejected" and not regenerate:
            scope["blocked"].append(step_id)
        elif dependencies <= accepted and step.get("source") == "generate":
            scope["ready"].append(step_id)
        else:
            scope["waiting"].append(step_id)
    return scope
