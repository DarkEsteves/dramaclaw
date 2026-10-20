"""Persist separate technical and content observations for a material candidate."""
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

from .production_plan_v2 import ResourceV2


class ReviewCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    status: Literal["passed", "rejected", "unavailable"]
    checks: list[str] = Field(default_factory=list, max_length=100)
    findings: list[str] = Field(default_factory=list, max_length=100)


class MaterialReviewV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    resource: ResourceV2
    step_id: str = Field(min_length=1, max_length=128)
    work_ref: dict[str, Any]
    technical: ReviewCheck
    content: ReviewCheck
    actor_kind: Literal["user", "agent", "system"]
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    source_evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    decision_receipt_id: str | None = None
    user_decision: dict[str, Any] | None = None
    content_observation: dict[str, Any] | None = None


def validate_material_review_lineage(store, payload: dict, refs: list[dict]) -> None:
    ref = payload["work_ref"]
    if not any(r["artifact_id"] == ref.get("artifact_id") and r["revision"] == ref.get("revision") for r in refs):
        raise ValueError("material review requires its exact work dependency")
    work = store._read(ref["artifact_id"], ref["revision"])
    if work["schema"] != "material_work_package.v1":
        raise ValueError("material review requires a material work package")
    step = next((s for s in work["payload"]["steps"] if s["id"] == payload["step_id"]), None)
    if not step or step["media_kind"] != payload["resource"]["media_kind"]:
        raise ValueError("material review step or kind differs")
    for evidence in payload.get("evidence_refs", []):
        if not any(r["artifact_id"] == evidence.get("artifact_id") and r["revision"] == evidence.get("revision") for r in refs):
            raise ValueError("material observation requires saved evidence dependency")
    if step['source'] in {'existing', 'upload'}:
        for evidence in payload.get('source_evidence', []):
            if evidence.get('role') == 'supplied_source' and (evidence.get('node_id') != payload['resource']['node_id']
                    or evidence.get('media_hash') != payload['resource']['media_hash']):
                raise ValueError('material source evidence differs from candidate')
    observation = payload.get("content_observation") or {}
    observed = (observation.get("host_observed") is True and observation.get("status") == "passed"
                and observation.get("media_hash") == payload["resource"]["media_hash"]
                and isinstance(observation.get("model"), str) and bool(observation["model"])
                and set(observation.get("checked_items", [])) == set(observation.get("required_items", []))
                and bool(observation.get("required_items")))
    if payload["content"]["status"] == "passed" and not payload.get("evidence_refs") and not payload.get("decision_receipt_id") and not observed:
        raise ValueError("content review cannot pass without observed evidence or a user decision")

    decision = payload.get("user_decision")
    if payload.get("decision_receipt_id"):
        import hashlib
        if (payload.get("actor_kind") != "user" or not isinstance(decision, dict)
                or set(decision) != {"requester_user_id", "canvas_id", "project_id", "media_hash"}
                or decision["project_id"] != store.project_id or decision["requester_user_id"] != store.created_by
                or decision["media_hash"] != payload["resource"]["media_hash"]
                or hashlib.sha256(store._canonical(decision)).hexdigest() != payload["decision_receipt_id"]):
            raise ValueError("material content requires a valid user decision receipt")
