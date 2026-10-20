"""Server checked creative reviews tied to actual plan and direction revisions."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .production_plan_v2 import ProductionPlanV2, resolve_production_placement_timing
from .production_audio_sources import planned_speech_sources

REVIEW_CATEGORIES = ("dialogue", "narrative", "audio_visual_roles", "material_requirements",
                     "native_effects", "output", "semantic_anchors", "direction")


class ReviewEvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    entity_kind: Literal["plan", "beat", "turn", "placement", "resource", "selection"]
    entity_id: str = Field(min_length=1)
    field: Literal["goal", "purpose", "text", "description", "media_path", "graphic.text"]


class ReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal["dialogue", "narrative", "audio_visual_roles", "material_requirements",
                      "native_effects", "output", "semantic_anchors", "direction"]
    entity_kind: Literal["plan", "beat", "turn", "placement", "resource", "selection"]
    entity_id: str = Field(min_length=1)
    verdict: Literal["pass", "change", "unsupported"]
    reason: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)
    evidence_refs: list[ReviewEvidenceRef] | None = None
    requirement_id: str | None = Field(default=None, min_length=1)


class ProductionReviewV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["production_review.v1"] = "production_review.v1"
    plan_ref: dict[str, Any]
    direction_ref: dict[str, Any]
    plan_content_hash: str
    decision_fingerprint: str
    direction_fingerprint: str
    findings: list[ReviewFinding]
    resolved_evidence: list[dict[str, Any]] = Field(default_factory=list)
    checks: list[dict[str, Any]]
    status: Literal["passed", "changes_required"]


def is_unarranged_outline(payload: dict[str, Any]) -> bool:
    """Recognize an AI outline without blocking a manually completed arrangement."""
    return payload.get("authoring_stage") == "outline" and not payload.get("placements")


def _fingerprint(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def production_decision_fingerprint(payload: dict[str, Any]) -> str:
    """Target, dialogue and creative arrangement; adopted media/timing may refine later."""
    return _fingerprint({"goal": payload["goal"], "output": payload["output"],
                         "placements": [{key: row[key] for key in
                                         ("id", "kind", "role", "turn_id", "text", "graphic", "track_id", "z_order", "fit", "x", "y", "width", "height", "opacity", "source_audio") if key in row}
                                        for row in payload.get("placements", [])],
                         "beats": [{"id": b["id"], "purpose": b["purpose"],
                                    "turns": [{"id": t["id"], "speaker": t.get("speaker", ""), "text": t["text"],
                                               **({"speech_placement_id": t["speech_placement_id"]}
                                                  if "speech_placement_id" in t else {})}
                                              for t in b.get("turns", [])]} for b in payload["beats"]]})


def evaluate_production_review(payload: dict[str, Any], direction: dict[str, Any],
                               findings: list[dict[str, Any]], *, project_dir: Path = Path('.'),
                               locked_turns: list[dict] | None = None) -> dict[str, Any]:
    from .direction_contract import resolve_direction_review_contract
    from .production_requirements import check_production_requirements
    plan = ProductionPlanV2.model_validate(payload).model_dump(mode="json", exclude_none=True)
    rows = [ReviewFinding.model_validate(row).model_dump(mode="json", exclude_none=True) for row in findings]
    entities = {"plan": {"plan"}, "beat": {b["id"] for b in plan["beats"]},
                "turn": {t["id"] for b in plan["beats"] for t in b["turns"]},
                **{kind: {r["id"] for r in plan[field]} for kind, field in
                   (("placement", "placements"), ("resource", "resources"), ("selection", "selections"))}}
    for row in rows:
        if row["entity_id"] not in entities[row["entity_kind"]]:
            raise ValueError("review finding references an unknown entity")
        if not row["reason"].strip() or any(not evidence.strip() for evidence in row["evidence"]):
            raise ValueError("review finding requires concrete reason and evidence")
    contract = resolve_direction_review_contract(direction)
    requirements = contract['requirements']
    checks = check_production_requirements(plan, requirements, project_dir=project_dir, locked_turns=locked_turns)
    from .production_plan_diagnostics import collect_plan_diagnostics
    checks.extend({"code": issue["code"], "entity_id": issue["entity_id"],
                   "message": issue["message"], "status": "failed"}
                  for issue in collect_plan_diagnostics(plan) if issue["severity"] == "error")

    def fail(code: str, entity_id: str, message: str):
        checks.append({"code": code, "entity_id": entity_id, "message": message, "status": "failed"})

    semantic = {row['id']: row for row in requirements.get('semantic', [])}
    content_entities = {
        'plan': {'plan': plan},
        'beat': {row['id']: row for row in plan['beats']},
        'turn': {row['id']: row for beat in plan['beats'] for row in beat['turns']},
        'placement': {row['id']: row for row in plan['placements']},
        'resource': {row['id']: row for row in plan['resources']},
        'selection': {row['id']: row for row in plan['selections']},
    }
    resolved_evidence: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        for ref in row.get('evidence_refs') or []:
            entity = content_entities[ref['entity_kind']].get(ref['entity_id'])
            if entity is None:
                raise ValueError('review evidence references an unknown entity')
            if ref['entity_kind'] == 'plan' and ref['field'] != 'goal':
                raise ValueError('review evidence field is invalid for plan')
            value = (entity.get('graphic') or {}).get('text') if ref['field'] == 'graphic.text' else entity.get(ref['field'])
            if not isinstance(value, str) or not value.strip():
                raise ValueError('review evidence field is absent or blank in the saved plan')
            resolved_evidence.append({'finding_index': index, **ref, 'value': value})

    def actual_evidence(index: int) -> bool:
        return any(row['finding_index'] == index and row['entity_kind'] != 'plan'
                   for row in resolved_evidence)

    for row in rows:
        if row.get('requirement_id') is not None and row['requirement_id'] not in semantic:
            raise ValueError('review finding references an unknown requirement_id')
    for requirement_id in semantic:
        matching = [(index, row) for index, row in enumerate(rows)
                    if row.get('requirement_id') == requirement_id]
        if not matching:
            fail('semantic_requirement_review_missing', requirement_id, 'Semantic requirement needs an actual entity finding')
        elif not all(row['verdict'] == 'pass' and actual_evidence(index) for index, row in matching):
            fail('semantic_requirement_unsupported', requirement_id, 'Semantic requirement needs a supported verdict with saved entity content evidence')

    if not plan["beats"]:
        fail("empty_plan", "plan", "Create the narrative beats before review")
    missing = set(REVIEW_CATEGORIES) - {row["category"] for row in rows}
    if missing:
        fail("creative_review_incomplete", "plan", "Missing reasoned review: " + ", ".join(sorted(missing)))
    planned_sources = planned_speech_sources(plan)
    sound_only_ids = {placement["id"] for placement in plan["placements"]
                      if placement.get("source_audio") is True and placement["id"] not in planned_sources}
    sound_context_refs: dict[str, set[tuple[str, str, str]]] = {}
    if sound_only_ids:
        _, timing_rows = resolve_production_placement_timing(plan)
        timing = {row["id"]: row for row in timing_rows}
        beat_ranges = []
        frame = 0
        for beat in plan["beats"]:
            beat_ranges.append((beat["id"], frame, frame + beat["duration_frames"]))
            frame += beat["duration_frames"]
        for placement in plan["placements"]:
            placement_id = placement["id"]
            if placement_id not in sound_only_ids:
                continue
            refs = set()
            if placement.get("description", "").strip():
                refs.add(("placement", placement_id, "description"))
            placement_timing = timing[placement_id]
            if placement_timing["timing_status"] == "resolved":
                start, end = placement_timing["start_frame"], placement_timing["end_frame"]
                refs.update(("beat", beat_id, "purpose") for beat_id, begin, stop in beat_ranges
                            if start < stop and end > begin)
            sound_context_refs[placement_id] = refs
    for placement in plan["placements"]:
        placement_id = placement["id"]
        if placement.get("source_audio") is not True and placement_id not in planned_sources:
            continue
        turn_ids = planned_sources.get(placement_id, [])
        required_refs = {("turn", turn_id, "text") for turn_id in turn_ids}
        grounded = any(
            row["category"] == "audio_visual_roles" and row["entity_kind"] == "placement"
            and row["entity_id"] == placement_id and row["verdict"] == "pass"
            and (required_refs <= {(ref["entity_kind"], ref["entity_id"], ref["field"])
                                  for ref in row.get("evidence_refs") or []}
                 if turn_ids else bool(sound_context_refs[placement_id] & {
                     (ref["entity_kind"], ref["entity_id"], ref["field"])
                     for ref in row.get("evidence_refs") or []}))
            for row in rows
        )
        if not grounded:
            fail("planned_audio_source_review_missing", placement_id,
                 f"Review planned audio source placement {placement_id} and turns "
                 f"{', '.join(turn_ids) if turn_ids else '(no dialogue)'} with saved content evidence; "
                 "actual media sound still needs material review")
    if contract['goal'] is not None and contract['goal'] != plan['goal']:
        direction_findings = [index for index, row in enumerate(rows)
                              if row['category'] == 'direction' and row['verdict'] == 'pass']
        if not any(
            any(e['finding_index'] == index and e['entity_kind'] == 'plan' and e['field'] == 'goal'
                for e in resolved_evidence)
            and actual_evidence(index) for index in direction_findings
        ):
            fail('direction_goal_evidence_missing', 'plan',
                 'Expanded plan goal needs a direction finding grounded in the saved goal and content')
    turns = {t["id"]: t for b in plan["beats"] for t in b["turns"]}
    for turn_id, text in contract['exact_turns'].items():
        if turn_id not in turns or turns[turn_id]["text"] != text:
            fail("preserved_dialogue_changed", turn_id, "Confirmed preserved dialogue differs")
    choices = contract['preserve_replace']
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                fail("direction_constraint_unresolved", "plan", "Direction choice requires a concrete entity and value")
                continue
            kind, entity_id = choice.get("entity_kind"), choice.get("entity_id")
            if kind == "turn" and choice.get("action") == "preserve" and "value" in choice:
                if entity_id not in turns or turns[entity_id]["text"] != choice["value"]:
                    fail("preserved_dialogue_changed", entity_id or "plan", "Preserved dialogue differs from direction")
            elif kind == "turn" and choice.get("action") == "replace" and "value" in choice:
                if entity_id not in turns or turns[entity_id]["text"] != choice["value"]:
                    fail("replacement_dialogue_missing", entity_id or "plan", "Confirmed replacement dialogue is missing")
            elif not choice.get("reason"):
                fail("direction_constraint_unresolved", entity_id or "plan", "Nonliteral direction requires reasoned Agent review")
    voiced_ranges = []
    frame = 0
    for beat in plan["beats"]:
        if any(turn["text"].strip() for turn in beat["turns"]):
            voiced_ranges.append((frame, frame + beat["duration_frames"]))
        frame += beat["duration_frames"]
    for placement in plan["placements"]:
        if placement["kind"] != "visual" or placement["role"] != "primary":
            continue
        start, end = placement["start"], placement["end"]
        if (start["kind"] == end["kind"] == "program_frame"
                and any(start["frame"] < stop and end["frame"] > begin
                        for begin, stop in voiced_ranges)):
            fail("dialogue_visual_anchor_missing", placement["id"],
                 "A visual over dialogue needs a beat, turn or speech anchor so timing can follow accepted speech")
    checks.append({"code": "schema_valid", "entity_id": "plan", "message": "Validated native effects, roles and semantic references", "status": "passed"})
    return {"findings": rows, "resolved_evidence": resolved_evidence, "checks": checks,
            "status": "changes_required" if any(c["status"] == "failed" for c in checks) or any(r["verdict"] != "pass" for r in rows) else "passed"}


def _verified_task_target(store, payload: dict, commission: dict,
                          plan_ref: dict | None) -> dict:
    """Authenticate an edited node target against the immutable runner output."""
    from .operations import RemixOperationStore

    if not plan_ref:
        raise ValueError('edited plan target requires a saved task artifact')
    original = store.read(plan_ref['artifact_id'], 1)
    if original['schema'] != 'production_plan.v2':
        raise ValueError('edited plan target requires a production plan')
    original_provenance = original['payload'].get('provenance') or {}
    current_provenance = payload.get('provenance') or {}
    fields = ('operation_id', 'node_target', 'requirements',
              'requirements_fingerprint', 'requirements_provenance', 'locked_turns')
    if any(current_provenance.get(key) != original_provenance.get(key) for key in fields):
        raise ValueError('plan task provenance changed after generation')
    operation_id = original_provenance.get('operation_id')
    target = original_provenance.get('node_target')
    if not isinstance(operation_id, str) or not isinstance(target, dict):
        raise ValueError('edited plan target has no signed task operation')
    try:
        operation = RemixOperationStore(store.project_dir, store.project_id).get(operation_id)
    except (FileNotFoundError, ValueError) as exc:
        raise ValueError('edited plan task operation is unavailable') from exc
    parameters = operation.get('normalized_parameters') or {}
    snapshot = parameters.get('payload') or {}
    # Delivery status can lag the saved canvas result. The immutable task
    # output and input snapshot establish provenance; the review route checks
    # the current canvas binding separately.
    if (operation.get('producer_version') != 'remix-process.v1'
            or parameters.get('action') != 'generate_production_plan_draft'
            or not operation.get('task_id')
            or {'artifact_id': original['id'], 'revision': 1} not in [
                {key: ref.get(key) for key in ('artifact_id', 'revision')}
                for ref in operation.get('output_refs', []) if isinstance(ref, dict)]
            or target.get('canvas_id') != commission['payload'].get('canvas_id')
            or target.get('canvas_id') != operation.get('canvas_id')
            or target.get('node_id') != operation.get('node_id')
            or target.get('node_revision') != operation.get('node_revision')
            or target.get('goal') != str(snapshot.get('topic') or '').strip()
            or original_provenance.get('requirements') != snapshot.get('requirements')
            or original_provenance.get('requirements_fingerprint') != snapshot.get('requirements_fingerprint')
            or original_provenance.get('locked_turns') != snapshot.get('locked_turns')):
        raise ValueError('edited plan target differs from its signed task operation')
    return original_provenance


def evaluate_saved_production_review(store, payload: dict, commission: dict,
                                     findings: list[dict], *, plan_ref: dict | None = None) -> dict:
    """Use exact saved direction evidence both before and at review publication."""
    direction = commission['payload'].get('direction') or {}
    provenance = payload.get('provenance') or {}
    node_target = provenance.get('node_target') or {}
    confirmed_goal = direction.get('target_goal', direction.get('goal'))
    target_constraints = provenance.get('requirements')
    if (isinstance(node_target, dict) and isinstance(node_target.get('goal'), str)
            and confirmed_goal is not None
            and (node_target['goal'] != confirmed_goal
                 or target_constraints != direction.get('requirements'))):
        # A confirmed canvas edit changes the native node's input version. The
        # immutable commission remains audit evidence for the original goal.
        # Plan provenance is editable, so verify it against the original runner
        # artifact and host operation before using the newer node constraints.
        _verified_task_target(store, payload, commission, plan_ref)
        direction = {'target_goal': node_target['goal'],
                     'requirements': provenance.get('requirements') or {}}
        return evaluate_production_review(payload, direction, findings,
            project_dir=store.project_dir, locked_turns=provenance.get('locked_turns'))
    locked_turns = None
    if direction.get('requirements', {}).get('dialogue', {}).get('mode') in {'preserve_source', 'use_confirmed_text'}:
        from .production_requirements import resolve_production_requirements
        from .reference_commission import read_reference_commission
        ref = {'artifact_id': commission['id'], 'revision': commission['revision']}
        current = read_reference_commission(store, ref, latest=True)
        locked_turns = resolve_production_requirements(store, commission=current).get('locked_turns')
    return evaluate_production_review(payload, direction, findings,
                                      project_dir=store.project_dir, locked_turns=locked_turns)


def review_production_plan(store, plan_ref: dict[str, Any], direction_ref: dict[str, Any],
                           findings: list[dict[str, Any]], *, review_id: str | None = None) -> dict[str, Any]:
    """Save a review of an exact current plan against its confirmed commission direction."""
    from .store import RemixArtifactConflict
    for ref in (plan_ref, direction_ref):
        if ref.get("project_id", store.project_id) != store.project_id:
            raise ValueError("cross-project review reference")
    plan = store.read(plan_ref["artifact_id"], plan_ref["revision"])
    commission = store.read(direction_ref["artifact_id"], direction_ref["revision"])
    if plan["schema"] != "production_plan.v2" or commission["schema"] != "reference_commission.v1":
        raise ValueError("review requires a saved V2 plan and reference commission")
    direction = commission["payload"].get("direction")
    if not direction or not commission["payload"].get("direction_revision"):
        raise ValueError("review requires confirmed direction")
    if store.read_latest(plan["id"])["revision"] != plan["revision"]:
        raise RemixArtifactConflict("plan changed before review")
    current_direction = store.read_latest(commission["id"])["payload"]
    if current_direction.get("direction_fingerprint") != commission["payload"].get("direction_fingerprint"):
        raise RemixArtifactConflict("direction changed before review")
    evaluated = evaluate_saved_production_review(store, plan['payload'], commission, findings,
                                                  plan_ref=plan_ref)
    payload = ProductionReviewV1(plan_ref={**plan_ref, "project_id": store.project_id},
                                direction_ref={**direction_ref, "project_id": store.project_id},
                                plan_content_hash=plan["content_hash"],
                                decision_fingerprint=production_decision_fingerprint(plan["payload"]),
                                direction_fingerprint=_fingerprint(direction), **evaluated).model_dump(mode="json", exclude_none=True)
    if review_id is None:
        return store.create("production_review.v1", payload, [plan_ref, direction_ref])
    refs = store._reference_list([plan_ref, direction_ref], store.project_id)
    with store._locked() as guard:
        try:
            saved = store._read(review_id, 1)
        except FileNotFoundError:
            saved = None
        if saved is not None:
            if (saved['schema'] != 'production_review.v1' or saved['payload'] != payload
                    or saved['upstream_refs'] != refs or saved['created_by'] != store.created_by):
                raise ValueError('review request identity reused with different content')
            return saved
        store._validate_upstream_refs(refs)
        store._validate_v2_lineage('production_review.v1', payload, refs, target_artifact_id=review_id)
        return store._publish(artifact_id=review_id, schema='production_review.v1', revision=1,
                              payload=payload, upstream_refs=refs, created_by=store.created_by, guard=guard)
