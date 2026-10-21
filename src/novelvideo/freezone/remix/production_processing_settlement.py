"""Publish verified native processing results to the canvas under its write lock.

The project task and workflow action are the authority for completion. This
module never starts a task; callers may replay it after a worker or page crash.
"""
from __future__ import annotations

import json
import hashlib
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any

from novelvideo.freezone import canvas_store
from novelvideo.freezone.remix.store import RemixArtifactStore
from novelvideo.freezone.workflow_runs import read_workflow_run
from novelvideo.llm_instrumentation import log_production_plan_diagnostic


def _ref(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    artifact_id = value.get("artifactId", value.get("artifact_id"))
    revision = value.get("revision")
    if not isinstance(artifact_id, str) or not artifact_id or type(revision) is not int or revision < 1:
        return None
    return {"artifactId": artifact_id, "revision": revision}


def _json_value(value: object) -> object:
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return None


def _node_input(node: dict) -> dict:
    data = node.get("data") or {}
    return {
        "action": data.get("processingAction"),
        "payload": data.get("processingPayload"),
        "revision": data.get("remixNodeRevision") or 1,
        "remixRole": data.get("remixRole"),
        "productionPlanNodeId": data.get("productionPlanNodeId"),
    }


def _analysis_input(node: dict) -> dict:
    data = node.get("data") or {}
    return {
        "rawResult": data.get("rawResult"),
        "blueprintRef": data.get("referenceBlueprintRef"),
        "blueprintDirty": data.get("blueprintDirty", False),
        "sourceVideoUrl": data.get("sourceVideoUrl"),
    }


def _empty_plan(value: object) -> bool:
    return not isinstance(value, dict) or all(
        isinstance(value.get(key), list) and not value[key]
        for key in ("beats", "resources", "placements")
    )


def _reference_binding_for_plan(ctx: Any, canvas_id: str, node: dict) -> dict | None:
    data = node.get("data") or {}
    draft_id = data.get("workflowInstanceId")
    if not draft_id:
        return None
    plan_node_id = data.get("workflowPlanNodeId")
    if not isinstance(draft_id, str) or not isinstance(plan_node_id, str) or not plan_node_id:
        raise ValueError("invalid native plan draft binding")
    from novelvideo.freezone.workflow_drafts import read_workflow_draft

    draft, _ = read_workflow_draft(project_dir=ctx.state_dir, canvas_id=canvas_id, draft_id=draft_id)
    if not draft or draft.get("project_id") != ctx.project_id or draft.get("status") != "confirmed":
        raise ValueError("native plan draft is no longer confirmed")
    binding = (draft.get("compiled") or {}).get("reference_binding")
    if not isinstance(binding, dict) or not binding:
        return None  # Ordinary manually constructed workflow has no commission.
    if (binding.get("project_id") != ctx.project_id
            or binding.get("canvas_id") != canvas_id
            or binding.get("requester_user_id") != ctx.requester_user_id
            or not isinstance(binding.get("commission_id"), str)
            or not binding["commission_id"]):
        raise ValueError("native plan draft belongs to another commission scope")
    return binding


def _plan_binding_current(ctx: Any, canvas_id: str, node: dict, store: RemixArtifactStore) -> bool:
    from novelvideo.freezone.remix.reference_commission import read_reference_commission
    from novelvideo.chat.reference_production_observer import _bound_plan_node

    try:
        binding = _reference_binding_for_plan(ctx, canvas_id, node)
        if binding is None:
            return True
        commission_id = binding["commission_id"]
        commission = read_reference_commission(store, {"artifact_id": commission_id}, latest=True)
        if commission.get("requester_user_id") != ctx.requester_user_id:
            return False
        return _bound_plan_node(node, commission, ctx.state_dir)
    except (KeyError, ValueError, FileNotFoundError, PermissionError):
        return False


def native_action_automation_control(ctx: Any, *, canvas_id: str, canvas: dict,
                                     action: dict) -> dict | None:
    """Resolve a trusted automation control for this exact native action target."""
    from novelvideo.freezone.remix.production_automation import read_production_control

    nodes = [node for node in canvas.get("nodes", []) if node.get("id") == action.get("node_id")]
    if len(nodes) != 1:
        raise ValueError("native action node is unavailable")
    plan_node = (_source_plan(canvas, nodes[0]) if action.get("action") == "compile_production_plan"
                 else nodes[0])
    if plan_node is None:
        raise ValueError("native action plan source is unavailable")
    binding = _reference_binding_for_plan(ctx, canvas_id, plan_node)
    if binding is None:
        return None
    if not _plan_binding_current(ctx, canvas_id, plan_node,
                                 RemixArtifactStore(ctx.output_dir, ctx.project_id)):
        raise ValueError("native action plan binding changed")
    return read_production_control(ctx, canvas_id=canvas_id,
                                   commission_id=binding["commission_id"], agent_id="main")


def _operation_automation_current(ctx: Any, action: dict, *, canvas_id: str,
                                  canvas: dict) -> bool:
    from novelvideo.freezone.remix.production_automation import assert_operation_automation_current

    try:
        control = native_action_automation_control(ctx, canvas_id=canvas_id, canvas=canvas, action=action)
        bound = assert_operation_automation_current(ctx, str(action.get("processing_operation_id") or ""))
        if action.get("_host_execution_source") == "canvas_user":
            return bound is None
        if control is not None and (not control.get("active") or bound is None):
            return False
        return True
    except (KeyError, ValueError, FileNotFoundError, PermissionError):
        return False


def _safe_plan_target(ctx: Any, canvas_id: str, canvas: dict, node: dict,
                      action: dict, result_ref: dict, store: RemixArtifactStore) -> bool:
    data = node.get("data") or {}
    submission = data.get("productionWorkflowSubmission")
    if node.get("type") != "scriptNode" or data.get("remixRole") != "production_plan":
        return False
    if data.get("processingAction") != "generate_production_plan_draft":
        return False
    if not _operation_automation_current(ctx, action, canvas_id=canvas_id, canvas=canvas) or not _plan_binding_current(ctx, canvas_id, node, store):
        return False
    if data.get("productionPlanV2Dirty") or data.get("productionPlanV2Conflict"):
        return False
    existing = _ref(data.get("productionPlanV2Ref"))
    if not _empty_plan(data.get("productionPlanV2Draft")):
        return False
    if existing and existing != result_ref:
        # The editor may auto-save an empty goal-only plan before the AI draft
        # completes. Replace it only when the action was submitted against this
        # exact unchanged version; a later edit keeps the generated artifact as
        # historical evidence rather than overwriting the user's node.
        if not isinstance(submission, dict) or _ref(submission.get("planRef")) != existing:
            return False
        try:
            saved_empty = store.read(existing['artifactId'], existing['revision'])
            latest_empty = store.read_latest(existing['artifactId'])
        except (FileNotFoundError, KeyError, ValueError):
            return False
        if (saved_empty.get('schema') != 'production_plan.v2'
                or latest_empty['revision'] != existing['revision']
                or saved_empty.get('payload') != data.get('productionPlanV2Draft')):
            return False
    if not isinstance(submission, dict) or submission.get("action") != action.get("action"):
        return False
    try:
        from .production_requirements import hydrate_native_requirements, validate_native_requirements
        action_payload = deepcopy(action.get('payload') or {})
        validate_native_requirements(store, action_payload)
        submitted_payload = deepcopy(submission.get('payload') or {})
        hydrate_native_requirements(store, submitted_payload,
            canvas={**canvas, 'canvas_id': canvas_id}, node_id=node['id'], requester_user_id=ctx.requester_user_id)
    except (ValueError, PermissionError, KeyError, FileNotFoundError):
        return False
    if submitted_payload != action_payload:
        return False
    if _json_value(submission.get("nodeData")) != _node_input(node):
        return False
    if data.get("remixNodeRevision", 1) != action.get("node_revision"):
        return False
    analyses = [source for source in canvas.get("nodes", [])
                if source.get("type") == "videoStoryNode" and any(
                    edge.get("source") == source.get("id") and edge.get("target") == node.get("id")
                    for edge in canvas.get("edges", []))]
    if len(analyses) > 1:
        return False
    submitted_analysis_id = submission.get("analysisNodeId")
    submitted_analysis_data = submission.get("analysisData")
    if submitted_analysis_id is None and submitted_analysis_data is None:
        if analyses:
            return False
    elif (not isinstance(submitted_analysis_id, str) or len(analyses) != 1
          or analyses[0].get("id") != submitted_analysis_id
          or _json_value(submitted_analysis_data) != _analysis_input(analyses[0])):
        return False
    for compose in canvas.get("nodes", []):
        if compose.get("type") != "videoComposeNode":
            continue
        compose_data = compose.get("data") or {}
        linked = compose_data.get("productionPlanNodeId") == node.get("id") or any(
            edge.get("source") == node.get("id") and edge.get("target") == compose.get("id")
            for edge in canvas.get("edges", [])
        )
        if not linked:
            continue
        bound_elsewhere = compose_data.get("productionPlanNodeId") not in (None, node.get("id"))
        other_plan_sources = any(
            edge.get("target") == compose.get("id") and edge.get("source") != node.get("id")
            and any(candidate.get("id") == edge.get("source") and candidate.get("type") == "scriptNode"
                    for candidate in canvas.get("nodes", []))
            for edge in canvas.get("edges", [])
        )
        current_ref = _ref(compose_data.get("productionPlanV2Ref"))
        if (bound_elsewhere or other_plan_sources or compose_data.get("productionPlanV2Dirty")
                or compose_data.get("productionPlanV2Conflict")
                or current_ref is not None and current_ref != result_ref):
            return False
    return True


def _safe_arrangement_target(ctx: Any, canvas_id: str, canvas: dict, node: dict,
                             action: dict, result_ref: dict, store: RemixArtifactStore) -> bool:
    data = node.get("data") or {}
    submission = data.get("productionWorkflowSubmission")
    source_ref = _ref((action.get("payload") or {}).get("plan_ref"))
    if (node.get("type") != "scriptNode" or data.get("remixRole") != "production_plan"
            or data.get("productionPlanV2Dirty") or data.get("productionPlanV2Conflict")
            or not source_ref or _ref(data.get("productionPlanV2Ref")) != source_ref
            or not isinstance(submission, dict) or submission.get("action") != action.get("action")
            or submission.get("payload") != action.get("payload")
            or _ref(submission.get("planRef")) != source_ref
            or _json_value(submission.get("nodeData")) != _node_input(node)
            or data.get("remixNodeRevision", 1) != action.get("node_revision")
            or result_ref != {**source_ref, "revision": source_ref["revision"] + 1}):
        return False
    if not _operation_automation_current(ctx, action, canvas_id=canvas_id, canvas=canvas) or not _plan_binding_current(ctx, canvas_id, node, store):
        return False
    try:
        saved = store.read(source_ref["artifactId"], source_ref["revision"])
        latest = store.read_latest(source_ref["artifactId"])
    except (ValueError, KeyError, FileNotFoundError):
        return False
    if (saved.get("payload") != data.get("productionPlanV2Draft")
            or latest["revision"] != result_ref["revision"]
            or latest["payload"].get("provenance") != saved["payload"].get("provenance")):
        return False
    for compose in canvas.get("nodes", []):
        if compose.get("type") != "videoComposeNode":
            continue
        compose_data = compose.get("data") or {}
        linked = compose_data.get("productionPlanNodeId") == node["id"] or any(
            edge.get("source") == node["id"] and edge.get("target") == compose.get("id")
            for edge in canvas.get("edges", []))
        if linked and (compose_data.get("productionPlanV2Dirty") or compose_data.get("productionPlanV2Conflict")
                       or compose_data.get("productionPlanNodeId") not in (None, node["id"])
                       or _source_plan(canvas, compose) != node
                       or _ref(compose_data.get("productionPlanV2Ref")) not in (None, source_ref, result_ref)):
            return False
    return True


def _plan_result_applied(data: dict, *, action: dict, expected_ref: dict) -> bool:
    """Whether this operation's result is what the canvas node adopted.

    A later editor save advances ``productionPlanV2Ref`` to a newer revision of
    the same plan artifact. The generated result is still the one the node
    adopted, so a delivery replay must report "already" instead of turning the
    user's own edit into a permanent settlement conflict.
    """
    if data.get('productionWorkflowAction') != action['action']:
        return False
    if _ref(data.get('productionWorkflowResultRef')) != expected_ref:
        return False
    plan_ref = _ref(data.get('productionPlanV2Ref'))
    if plan_ref == expected_ref:
        return True
    return bool(plan_ref
                and plan_ref['artifactId'] == expected_ref['artifactId']
                and plan_ref['revision'] > expected_ref['revision']
                and not data.get('productionPlanV2Dirty')
                and not data.get('productionPlanV2Conflict'))


def _publish_plan(ctx: Any, *, canvas_id: str, action: dict, artifact: dict,
                  store: RemixArtifactStore) -> str:
    from .operations import RemixOperationStore

    expected_ref = {"artifactId": artifact["id"], "revision": artifact["revision"]}
    operations = RemixOperationStore(ctx.output_dir, ctx.project_id)
    operation_id = action['processing_operation_id']
    outcome = "conflict"
    confirmation_claimed = False

    def check(canvas: dict | None) -> dict | None:
        nonlocal outcome, confirmation_claimed
        if not isinstance(canvas, dict) or canvas.get("project_id") != ctx.project_id:
            return {"status": "conflict"}
        nodes = [node for node in canvas.get("nodes", []) if node.get("id") == action["node_id"]]
        if len(nodes) != 1:
            return {"status": "conflict"}
        node = nodes[0]
        data = node.get("data") or {}
        operation = operations.get(operation_id)
        if (operation.get('canvas_id') != canvas_id or operation.get('node_id') != action['node_id']
                or operation.get('node_revision') != action.get('node_revision')
                or operation.get('producer_version') != 'remix-process.v1'
                or not operation.get('task_id')
                or operation.get('normalized_parameters') != {
                    'action': action['action'], 'payload': action.get('payload') or {}}
                or [_ref(ref) for ref in operation.get('output_refs', [])] != [expected_ref]
                or operation.get('cancel_reason') or operation.get('superseded_by')
                or operation.get('status') not in {'needs_confirmation', 'confirming', 'completed'}):
            return {'status': 'conflict'}
        applied = _plan_result_applied(data, action=action, expected_ref=expected_ref)
        if operation['status'] == 'confirming':
            operation = operations.recover_confirmation(operation_id, applied_to_canvas=applied)
            if operation['status'] == 'confirming':
                outcome = 'pending'
                return {'status': 'pending'}
        if applied:
            # Repair a crash or an older publisher that saved the node without
            # acknowledging delivery. Do not rewrite an already-applied plan.
            if operation['status'] == 'needs_confirmation':
                operations.claim_confirmation(operation_id, canvas_id=canvas_id, node_id=action['node_id'])
                confirmation_claimed = True
            outcome = "already"
            return {"status": "already"}
        if operation['status'] == 'completed':
            return {'status': 'conflict'}
        target_check = (_safe_arrangement_target if action["action"] == "generate_production_plan_arrangement"
                        else _safe_plan_target)
        if not target_check(ctx, canvas_id, canvas, node, action, expected_ref, store):
            return {"status": "conflict"}
        for key in ('requirements', 'requirements_fingerprint', 'requirements_provenance', 'locked_turns'):
            if key in action.get('payload', {}) and artifact['payload'].get('provenance', {}).get(key) != action['payload'][key]:
                return {'status': 'conflict'}
        target = artifact['payload'].get('provenance', {}).get('node_target')
        if action['action'] == 'generate_production_plan_draft' and target and (target.get('canvas_id'), target.get('node_id'), target.get('node_revision'), target.get('goal')) != (
            canvas_id, action['node_id'], action.get('node_revision'), action.get('payload', {}).get('topic')):
            return {'status': 'conflict'}
        operations.claim_confirmation(operation_id, canvas_id=canvas_id, node_id=action['node_id'])
        confirmation_claimed = True
        outcome = "publish"
        return None

    def build(canvas: dict | None) -> dict:
        updated = deepcopy(canvas)
        node = next(node for node in updated["nodes"] if node.get("id") == action["node_id"])
        data = node["data"]
        submission = data["productionWorkflowSubmission"]
        data.update(
            productionPlanV2Ref=expected_ref,
            productionPlanV2Draft=deepcopy(artifact["payload"]),
            productionPlanV2Dirty=False,
            productionPlanV2Conflict=False,
            productionPlanV2SaveError=None,
            productionWorkflowAction=action["action"],
            productionWorkflowInputs=submission["nodeData"],
            productionWorkflowRequestPayload=submission.get("requestPayload"),
            productionWorkflowResultRef=expected_ref,
            productionWorkflowAnalysisNodeId=next((edge.get("source") for edge in updated.get("edges", [])
                if edge.get("target") == node["id"] and any(n.get("id") == edge.get("source") and n.get("type") == "videoStoryNode"
                for n in updated["nodes"])), None),
            productionWorkflowAnalysisInputs=submission.get("analysisData"),
        )
        for compose in updated["nodes"]:
            if compose.get("type") != "videoComposeNode":
                continue
            compose_data = compose.get("data") or {}
            if compose_data.get("productionPlanNodeId") == node["id"] or any(
                edge.get("source") == node["id"] and edge.get("target") == compose.get("id")
                for edge in updated.get("edges", [])
            ):
                compose_data.update(productionPlanNodeId=node["id"], productionPlanV2Ref=expected_ref,
                                    remixInputsCurrent=False)
        updated["revision"] = int(canvas.get("revision") or 0) + 1
        updated["updated_at"] = canvas_store.utc_now_iso()
        updated["updated_by"] = str(ctx.requester_user_id)
        updated["save_source"] = "production_processing"
        return updated

    try:
        canvas_store.save_canvas(ctx.state_dir, canvas_id, base_revision=None, build_payload=build,
                                 skip_if=check, enforce_revision=False, save_source="production_processing")
    except Exception:
        if confirmation_claimed:
            operations.release_confirmation(operation_id)
        raise
    if outcome in {'publish', 'already'}:
        operations.finish_confirmation(operation_id)
    return outcome


def _source_plan(canvas: dict, compose: dict) -> dict | None:
    """A compose action may use one and only one saved plan node."""
    explicit = (compose.get("data") or {}).get("productionPlanNodeId")
    sources = [node for node in canvas.get("nodes", [])
               if node.get("type") == "scriptNode" and (node.get("data") or {}).get("remixRole") == "production_plan"
               and (node.get("id") == explicit or any(
                   edge.get("source") == node.get("id") and edge.get("target") == compose.get("id")
                   for edge in canvas.get("edges", [])))]
    return sources[0] if len(sources) == 1 else None


def _artifact_linked_to(artifact: dict, reference: dict) -> bool:
    return any(_ref(row) == reference for row in artifact.get("upstream_refs", []))


def _safe_compile_target(canvas: dict, node: dict, action: dict, artifact: dict) -> tuple[bool, dict | None]:
    data = node.get("data") or {}
    submission = data.get("productionWorkflowSubmission")
    if node.get("type") != "videoComposeNode" or data.get("processingAction") != "compile_production_plan":
        return False, None
    if (not isinstance(submission, dict) or submission.get("action") != action.get("action")
            or submission.get("payload") != action.get("payload")
            or _json_value(submission.get("nodeData")) != _node_input(node)
            or data.get("remixNodeRevision", 1) != action.get("node_revision")):
        return False, None
    source = _source_plan(canvas, node)
    if source is None:
        return False, None
    source_data = source.get("data") or {}
    plan_ref = _ref(source_data.get("productionPlanV2Ref"))
    if (plan_ref is None or source_data.get("productionPlanV2Dirty") or source_data.get("productionPlanV2Conflict")
            or _ref(submission.get("planRef")) != plan_ref
            or _ref((action.get("payload") or {}).get("plan_ref")) != plan_ref
            or not _artifact_linked_to(artifact, plan_ref)
            or [_ref(row) for row in artifact.get("payload", {}).get("source_versions", [])] != [plan_ref]):
        return False, None
    if submission.get("planData") is not None and _json_value(submission["planData"]) != source_data.get("productionPlanV2Draft"):
        return False, None
    manifest_ref = _ref(data.get("remixManifestRef"))
    expected_ref = {"artifactId": artifact["id"], "revision": artifact["revision"]}
    if (manifest_ref is not None and manifest_ref != expected_ref or data.get("remixManifestDirty")
            or data.get("remixWorkbenchDraft") or data.get("remixOperationId")
            or data.get("resultVideoUrl") or data.get("remixManifestConflicts")):
        return False, None
    return True, plan_ref


def _publish_compile(ctx: Any, *, canvas_id: str, action: dict, artifact: dict) -> str:
    expected_ref = {"artifactId": artifact["id"], "revision": artifact["revision"]}
    outcome = "conflict"

    def check(canvas: dict | None) -> dict | None:
        nonlocal outcome
        if not isinstance(canvas, dict) or canvas.get("project_id") != ctx.project_id:
            return {"status": "conflict"}
        nodes = [node for node in canvas.get("nodes", []) if node.get("id") == action["node_id"]]
        if len(nodes) != 1:
            return {"status": "conflict"}
        data = nodes[0].get("data") or {}
        if data.get("productionWorkflowAction") == action["action"] and _ref(data.get("productionWorkflowResultRef")) == expected_ref:
            outcome = "already"
            return {"status": "already"}
        if not _operation_automation_current(ctx, action, canvas_id=canvas_id, canvas=canvas):
            return {"status": "conflict"}
        if not _safe_compile_target(canvas, nodes[0], action, artifact)[0]:
            return {"status": "conflict"}
        outcome = "publish"
        return None

    def build(canvas: dict | None) -> dict:
        updated = deepcopy(canvas)
        node = next(node for node in updated["nodes"] if node.get("id") == action["node_id"])
        data = node["data"]
        source = _source_plan(updated, node)
        assert source is not None
        plan_ref = _ref(source["data"]["productionPlanV2Ref"])
        assert plan_ref is not None
        submission = data["productionWorkflowSubmission"]
        data.update(
            productionPlanNodeId=source["id"], productionPlanV2Ref=plan_ref,
            productionWorkflowAction=action["action"],
            productionWorkflowRequestPayload=submission.get("requestPayload"),
            productionWorkflowResultRef=expected_ref,
            remixManifestRef=expected_ref, remixManifestLoadedRef=None,
            remixManifestLoadState="loading", remixManifestDirty=False,
            remixInputsCurrent=True, remixOperationId=None, resultVideoUrl=None,
        )
        data["productionWorkflowInputs"] = json.dumps(_node_input(node), ensure_ascii=False, separators=(",", ":"))
        updated["revision"] = int(canvas.get("revision") or 0) + 1
        updated["updated_at"] = canvas_store.utc_now_iso()
        updated["updated_by"] = str(ctx.requester_user_id)
        updated["save_source"] = "production_processing"
        return updated

    canvas_store.save_canvas(ctx.state_dir, canvas_id, base_revision=None, build_payload=build,
                             skip_if=check, enforce_revision=False, save_source="production_processing")
    return outcome


def _speech_source(plan: dict, payload: dict) -> tuple[list[tuple[dict, dict]], str, dict, dict | None] | None:
    turns = [(beat, turn) for beat in plan.get("beats", []) for turn in beat.get("turns", [])]
    leader = next((turn for _, turn in turns if turn.get("id") == payload.get("turn_id")), None)
    if leader is None:
        return None
    take_id = payload.get("take_id")
    take = next((row for row in plan.get("speech_takes", []) if row.get("id") == take_id), None) if take_id else None
    if take_id and take is None:
        return None
    placement_id = take.get("origin_placement_id") if take else payload.get("placement_id")
    if not isinstance(placement_id, str) or not placement_id:
        return None
    placement = next((row for row in plan.get("placements", []) if row.get("id") == placement_id), None)
    resource_id = take.get("resource_id") if take else placement.get("resource_id") if placement else None
    resource = next((row for row in plan.get("resources", []) if row.get("id") == resource_id), None)
    if resource is None or resource.get("media_path") != payload.get("media_path"):
        return None
    if take:
        ids = take.get("turn_ids") or []
        group = [row for row in turns if row[1].get("id") in ids]
        if [row[1].get("id") for row in group] != ids or leader.get("id") not in ids:
            return None
    else:
        group = [row for row in turns if row[1].get("speech_placement_id") == placement_id
                 or not row[1].get("speech_placement_id") and
                 (row[1].get("speech_timing") or {}).get("placement_id") == placement_id]
        if not any(turn.get("id") == leader.get("id") for _, turn in group):
            group = [row for row in turns if row[1].get("id") == leader.get("id")]
    return group, placement_id, resource, take


def _speech_artifacts(store: RemixArtifactStore, plan: dict, payload: dict, artifact: dict) -> tuple[list[tuple[dict, dict, dict]], str, dict, dict | None] | None:
    source = _speech_source(plan, payload)
    if source is None:
        return None
    group, placement_id, resource, take = source
    plan_ref = _ref(payload.get("plan_ref"))
    if plan_ref is None or not _artifact_linked_to(artifact, plan_ref):
        return None
    raw = artifact.get("payload") or {}
    group_ids = [turn["id"] for _, turn in group]
    if (raw.get("turn_id") != payload.get("turn_id") or raw.get("placement_id") != placement_id
            or raw.get("media_hash") != "sha256:" + resource.get("media_hash", "")
            or raw.get("source_in_us") != 0 or raw.get("source_out_us") != resource.get("duration_us")
            or _ref(raw.get("plan_ref")) != plan_ref
            or len(group_ids) > 1 and raw.get("group_turn_ids") != group_ids):
        return None
    if raw.get("status") != "completed":
        return ([(group[0][0], group[0][1], artifact)], placement_id, resource, take)
    extra_refs = raw.get("group_refs") or []
    if len(extra_refs) != len(group) - 1 or not isinstance(extra_refs, list):
        return None
    by_turn = {raw["turn_id"]: artifact}
    for extra in extra_refs:
        if not isinstance(extra, dict) or extra.get("turn_id") not in group_ids or extra.get("turn_id") in by_turn:
            return None
        ref = _ref(extra)
        if ref is None or not _artifact_linked_to(artifact, ref):
            return None
        try:
            sibling = store.read(ref["artifactId"], ref["revision"])
        except (KeyError, ValueError, FileNotFoundError):
            return None
        by_turn[extra["turn_id"]] = sibling
    if set(by_turn) != set(group_ids):
        return None
    for _, turn in group:
        evidence = by_turn[turn["id"]]
        value = evidence.get("payload") or {}
        words = value.get("words")
        if (evidence.get("schema") != "word_timeline.v1" or not _artifact_linked_to(evidence, plan_ref)
                or value.get("status") != "completed" or value.get("turn_id") != turn["id"]
                or value.get("placement_id") != placement_id
                or value.get("media_hash") != "sha256:" + resource.get("media_hash", "")
                or value.get("source_in_us") != 0 or value.get("source_out_us") != resource.get("duration_us")
                or value.get("transcript") != turn.get("text")
                or value.get("text_hash") != hashlib.sha256(turn["text"].encode()).hexdigest()
                or not isinstance(words, list) or not words
                or any(not isinstance(word, dict) or not isinstance(word.get("text"), str)
                       or type(word.get("start_us")) is not int or type(word.get("end_us")) is not int
                       or word["start_us"] >= word["end_us"] or word["end_us"] > resource["duration_us"]
                       for word in words)):
            return None
    return [(beat, turn, by_turn[turn["id"]]) for beat, turn in group], placement_id, resource, take


def _identified_words(turn: dict, evidence: dict) -> list[dict]:
    prior = (turn.get("speech_timing") or {}).get("words") or []
    recognized = evidence["payload"]["words"]
    def unique_match(word: dict, key: str) -> dict | None:
        if key == "char":
            signature = (word.get("start_char"), word.get("end_char"), word.get("text"))
            if signature[0] is None or signature[1] is None:
                return None
            old = [row for row in prior if (row.get("start_char"), row.get("end_char"), row.get("text")) == signature]
            new = [row for row in recognized if (row.get("start_char"), row.get("end_char"), row.get("text")) == signature]
        else:
            old = [row for row in prior if row.get("text") == word.get("text")]
            new = [row for row in recognized if row.get("text") == word.get("text")]
        return old[0] if len(old) == len(new) == 1 else None
    used: set[str] = set()
    result: list[dict] = []
    for index, word in enumerate(recognized):
        previous = unique_match(word, "char") or unique_match(word, "text")
        identity = previous.get("id") if previous and previous.get("id") not in used else (
            f"{turn['id']}:spoken:{evidence['id']}:{evidence['revision']}:{index}")
        used.add(identity)
        result.append({**word, "id": identity})
    return result


def _accepted_speech_plan(plan: dict, rows: list[tuple[dict, dict, dict]], placement_id: str,
                          resource: dict, take: dict | None) -> dict | None:
    updated = deepcopy(plan)
    for original_beat, original_turn, evidence in rows:
        beat = next(row for row in updated["beats"] if row["id"] == original_beat["id"])
        turn = next(row for row in beat["turns"] if row["id"] == original_turn["id"])
        existing_cues = [row for row in updated.get("speech_cues", []) if row.get("turn_id") == turn["id"]]
        if len(existing_cues) > 1 or any(row.get("timing_source") != "asr" for row in existing_cues):
            return None
        words = _identified_words(turn, evidence)
        evidence_ref = {"artifact_id": evidence["id"], "revision": evidence["revision"]}
        turn["speech_timing"] = {
            "placement_id": placement_id, "media_hash": resource["media_hash"],
            "text": turn["text"], "text_hash": evidence["payload"]["text_hash"],
            "source_in_us": 0, "source_out_us": resource["duration_us"],
            "evidence_ref": evidence_ref, "words": words,
        }
        target_take = next((row for row in updated.get("speech_takes", [])
                            if row.get("beat_id") == beat["id"] and
                            (turn["id"] in row.get("turn_ids", []) or row.get("origin_placement_id") == placement_id)), None)
        if target_take is None:
            placement = next((row for row in updated["placements"] if row["id"] == placement_id), None)
            if placement is None:
                return None
            target_take = {"id": f"take:{beat['id']}:{placement_id}", "beat_id": beat["id"],
                           "resource_id": resource["id"], "turn_ids": [], "origin_placement_id": placement_id,
                           "source_in_us": placement.get("source_in_us") or 0,
                           "source_out_us": placement.get("source_out_us"),
                           "speed_num": placement.get("speed_num") or 1,
                           "speed_den": placement.get("speed_den") or 1, "lead_in_frames": 0}
            updated.setdefault("speech_takes", []).append(target_take)
        if turn["id"] not in target_take["turn_ids"]:
            target_take["turn_ids"].append(turn["id"])
        placement = next((row for row in updated["placements"] if row["id"] == placement_id), None)
        if placement and placement.get("kind") == "visual":
            placement["speech_take_id"] = target_take["id"]
        cue = existing_cues[0] if existing_cues else {
            "id": f"cue:{turn['id']}", "turn_id": turn["id"], "timing_source": "asr"}
        old_ids = cue.get("word_ids") or []
        new_ids = [word["id"] for word in words]
        cue.update(take_id=target_take["id"], text=turn["text"],
                   source_start_us=min(word["start_us"] for word in words),
                   source_end_us=max(word["end_us"] for word in words),
                   word_ids=new_ids, evidence_ref=evidence_ref,
                   timing_status="review" if any(identity not in new_ids for identity in old_ids) else "ready")
        if not existing_cues:
            updated.setdefault("speech_cues", []).append(cue)
    return updated


def _safe_speech_target(node: dict, action: dict, artifact: dict,
                        store: RemixArtifactStore) -> tuple[bool, dict | None, tuple | None]:
    data = node.get("data") or {}
    submission = data.get("productionWorkflowSubmission")
    if node.get("type") != "scriptNode" or data.get("remixRole") != "production_plan":
        return False, None, None
    if (data.get("processingAction") != "align_production_speech" or data.get("productionPlanV2Dirty")
            or data.get("productionPlanV2Conflict") or not isinstance(submission, dict)
            or submission.get("action") != action.get("action") or submission.get("payload") != action.get("payload")
            or _json_value(submission.get("nodeData")) != _node_input(node)
            or data.get("remixNodeRevision", 1) != action.get("node_revision")):
        return False, None, None
    plan_ref = _ref(data.get("productionPlanV2Ref"))
    if (plan_ref is None or _ref(submission.get("planRef")) != plan_ref
            or _ref((action.get("payload") or {}).get("plan_ref")) != plan_ref
            or not isinstance(data.get("productionPlanV2Draft"), dict)
            or submission.get("planData") is not None
            and _json_value(submission["planData"]) != data["productionPlanV2Draft"]):
        return False, None, None
    try:
        saved_plan = store.read(plan_ref["artifactId"], plan_ref["revision"])
    except (KeyError, ValueError, FileNotFoundError):
        return False, None, None
    if saved_plan.get("payload") != data["productionPlanV2Draft"]:
        return False, None, None
    rows = _speech_artifacts(store, saved_plan["payload"], action.get("payload") or {}, artifact)
    return (rows is not None), plan_ref, rows


def _publish_speech(ctx: Any, *, canvas_id: str, action: dict, artifact: dict,
                    store: RemixArtifactStore) -> str:
    result_ref = {"artifactId": artifact["id"], "revision": artifact["revision"]}
    canvas = canvas_store.read_canvas(ctx.state_dir, canvas_id)
    if not canvas or canvas.get("project_id") != ctx.project_id:
        return "conflict"
    nodes = [node for node in canvas.get("nodes", []) if node.get("id") == action["node_id"]]
    if len(nodes) != 1:
        return "conflict"
    data = nodes[0].get("data") or {}
    if data.get("productionWorkflowAction") == action["action"] and _ref(data.get("productionWorkflowResultRef")) == result_ref:
        return "already"
    if not _operation_automation_current(ctx, action, canvas_id=canvas_id, canvas=canvas):
        return "conflict"
    valid, plan_ref, speech = _safe_speech_target(nodes[0], action, artifact, store)
    if not valid or plan_ref is None or speech is None:
        return "conflict"
    rows, placement_id, resource, take = speech
    review = (artifact.get("payload") or {}).get("status") != "completed"
    updated_plan = None
    published_plan_ref = plan_ref
    if not review:
        updated_plan = _accepted_speech_plan(data["productionPlanV2Draft"], rows, placement_id, resource, take)
        if updated_plan is None:
            return "conflict"
        evidence_refs = [{"artifact_id": evidence["id"], "revision": evidence["revision"]}
                         for _, _, evidence in rows]
        old = store.read(plan_ref["artifactId"], plan_ref["revision"])
        upstream_refs = [*old.get("upstream_refs", []),
                         {"artifact_id": plan_ref["artifactId"], "revision": plan_ref["revision"]},
                         *evidence_refs]
        unique_refs = list({(ref["artifact_id"], ref["revision"]): ref for ref in upstream_refs}.values())
        try:
            saved = store.revise(plan_ref["artifactId"], plan_ref["revision"], updated_plan, unique_refs)
        except ValueError:
            # If the worker crashed between artifact revision and canvas write,
            # reuse only the exact revision and source evidence it already wrote.
            try:
                saved = store.read(plan_ref["artifactId"], plan_ref["revision"] + 1)
            except (ValueError, FileNotFoundError):
                return "conflict"
            if (saved.get("payload") != updated_plan or not all(
                    _artifact_linked_to(saved, _ref(ref)) for ref in evidence_refs)):
                return "conflict"
        published_plan_ref = {"artifactId": saved["id"], "revision": saved["revision"]}
        updated_plan = saved["payload"]
    outcome = "conflict"

    def check(current: dict | None) -> dict | None:
        nonlocal outcome
        if not isinstance(current, dict) or current.get("project_id") != ctx.project_id:
            return {"status": "conflict"}
        matching = [node for node in current.get("nodes", []) if node.get("id") == action["node_id"]]
        if len(matching) != 1:
            return {"status": "conflict"}
        latest_data = matching[0].get("data") or {}
        if latest_data.get("productionWorkflowAction") == action["action"] and _ref(latest_data.get("productionWorkflowResultRef")) == result_ref:
            outcome = "already"
            return {"status": "already"}
        if not _operation_automation_current(ctx, action, canvas_id=canvas_id, canvas=current):
            return {"status": "conflict"}
        if not _safe_speech_target(matching[0], action, artifact, store)[0]:
            return {"status": "conflict"}
        for compose in current.get("nodes", []):
            if compose.get("type") != "videoComposeNode":
                continue
            compose_data = compose.get("data") or {}
            linked = compose_data.get("productionPlanNodeId") == action["node_id"] or any(
                edge.get("source") == action["node_id"] and edge.get("target") == compose.get("id")
                for edge in current.get("edges", []))
            if linked and compose_data.get("productionPlanNodeId") not in (None, action["node_id"]):
                return {"status": "conflict"}
        outcome = "publish"
        return None

    def build(current: dict | None) -> dict:
        updated = deepcopy(current)
        node = next(node for node in updated["nodes"] if node.get("id") == action["node_id"])
        node_data = node["data"]
        submission = node_data["productionWorkflowSubmission"]
        if review:
            group = _speech_source(node_data["productionPlanV2Draft"], action["payload"])
            assert group is not None
            reviews = dict(node_data.get("productionSpeechReviews") or {})
            for _, turn in group[0]:
                reviews[turn["id"]] = {
                    "status": "no_speech" if artifact["payload"]["status"] == "no_speech" else "script_mismatch",
                    "artifactRef": result_ref, "placementId": placement_id,
                    "mediaHash": resource["media_hash"], "text": turn["text"],
                    "sourceInUs": 0, "sourceOutUs": resource["duration_us"],
                    "message": ("这段素材没有识别到语音，可按画面节奏继续" if artifact["payload"]["status"] == "no_speech"
                                else "完整发声素材与作品方案台词不一致，请试听并校正 SRT"),
                }
            node_data["productionSpeechReviews"] = reviews
        else:
            node_data["productionPlanV2Ref"] = published_plan_ref
            node_data["productionPlanV2Draft"] = updated_plan
            node_data["productionPlanV2Dirty"] = False
            reviews = dict(node_data.get("productionSpeechReviews") or {})
            for _, turn, _ in rows:
                reviews.pop(turn["id"], None)
            node_data["productionSpeechReviews"] = reviews
            for compose in updated["nodes"]:
                if compose.get("type") != "videoComposeNode":
                    continue
                compose_data = compose.get("data") or {}
                if compose_data.get("productionPlanNodeId") == node["id"] or any(
                    edge.get("source") == node["id"] and edge.get("target") == compose.get("id")
                    for edge in updated.get("edges", [])):
                    compose_data.update(productionPlanNodeId=node["id"], productionPlanV2Ref=published_plan_ref,
                                        remixInputsCurrent=False)
        node_data.update(productionWorkflowAction=action["action"],
                         productionWorkflowResultRef=result_ref,
                         productionWorkflowInputs=submission["nodeData"],
                         productionWorkflowRequestPayload=submission.get("requestPayload"))
        updated["revision"] = int(current.get("revision") or 0) + 1
        updated["updated_at"] = canvas_store.utc_now_iso()
        updated["updated_by"] = str(ctx.requester_user_id)
        updated["save_source"] = "production_processing"
        return updated

    canvas_store.save_canvas(ctx.state_dir, canvas_id, base_revision=None, build_payload=build,
                             skip_if=check, enforce_revision=False, save_source="production_processing")
    return outcome


def _conflict_path(ctx: Any, canvas_id: str, run_id: str, node_id: str, action: str) -> Path:
    identity = json.dumps([ctx.project_id, canvas_id, run_id, node_id, action], separators=(",", ":"))
    digest = hashlib.sha256(identity.encode()).hexdigest()
    return Path(ctx.state_dir) / "freezone" / "_native_settlement_conflicts" / f"{digest}.json"


def read_processing_settlement_conflicts(ctx: Any, *, canvas_id: str, run_id: str) -> list[dict[str, Any]]:
    """Read durable review evidence for actions whose result cannot replace canvas edits."""
    run = read_workflow_run(project_dir=ctx.state_dir, canvas_id=canvas_id, run_id=run_id)
    if not run or run.get("project_id") != ctx.project_id:
        return []
    records = []
    for action in run.get("actions", []):
        path = _conflict_path(ctx, canvas_id, run_id, action["node_id"], action["action"])
        if not path.is_file() or path.is_symlink():
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (record.get("project_id") == ctx.project_id and record.get("canvas_id") == canvas_id
                and record.get("workflow_run_id") == run_id and record.get("node_id") == action["node_id"]
                and record.get("action") == action["action"]):
            records.append(record)
    return records


def _record_conflict(ctx: Any, *, canvas_id: str, run_id: str, action: dict,
                     reason: str) -> dict[str, Any]:
    from novelvideo.freezone.workflow_drafts import read_workflow_draft, workflow_drafts_db_path

    draft_ids: set[str] = set()
    canvas = canvas_store.read_canvas(ctx.state_dir, canvas_id)
    for node in (canvas or {}).get("nodes", []):
        if node.get("id") == action["node_id"]:
            draft_id = (node.get("data") or {}).get("workflowInstanceId")
            if isinstance(draft_id, str) and draft_id:
                draft_ids.add(draft_id)
    db_path = workflow_drafts_db_path(ctx.state_dir)
    if db_path.is_file():
        try:
            with sqlite3.connect(db_path) as conn:
                draft_ids.update(str(row[0]) for row in conn.execute(
                    "SELECT DISTINCT draft_id FROM native_plan_slots WHERE plan_node_id=?",
                    (action["node_id"],)).fetchall())
        except sqlite3.Error:
            pass
    binding: dict = {}
    if len(draft_ids) == 1:
        try:
            draft, _ = read_workflow_draft(project_dir=ctx.state_dir, canvas_id=canvas_id,
                                           draft_id=next(iter(draft_ids)))
            if (draft and draft.get("project_id") == ctx.project_id and
                    (candidate := (draft.get("compiled") or {}).get("reference_binding"))
                    and candidate.get("project_id") == ctx.project_id
                    and candidate.get("canvas_id") == canvas_id
                    and candidate.get("requester_user_id") == ctx.requester_user_id):
                binding = candidate
        except ValueError:
            pass
    record = {
        "project_id": ctx.project_id, "canvas_id": canvas_id, "workflow_run_id": run_id,
        "node_id": action["node_id"], "action": action["action"],
        "operation_id": action.get("processing_operation_id"),
        "task_key": action.get("task_key"), "result_ref": action.get("result_ref"),
        "reason": reason, "recorded_at": canvas_store.utc_now_iso(),
        "commission_id": binding.get("commission_id"),
        "direction_revision": binding.get("direction_revision"),
        "direction_fingerprint": binding.get("direction_fingerprint"),
    }
    path = _conflict_path(ctx, canvas_id, run_id, action["node_id"], action["action"])
    if path.is_file() and not path.is_symlink():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and {
                key: value for key, value in existing.items() if key != "recorded_at"
            } == {key: value for key, value in record.items() if key != "recorded_at"}:
                return existing
        except (OSError, ValueError):
            pass
    canvas_store.atomic_write_json(path, record)
    return record


def _clear_conflict(ctx: Any, *, canvas_id: str, run_id: str, action: dict) -> None:
    """Drop a stale settlement conflict once delivery is proven again."""
    path = _conflict_path(ctx, canvas_id, run_id, action["node_id"], action["action"])
    try:
        if path.is_file() and not path.is_symlink():
            path.unlink()
    except OSError:
        pass


def has_settleable_native_action(run: dict[str, Any]) -> bool:
    """Whether a saved run contains a native result that needs canvas publication."""
    for action in run.get("actions", []):
        valid = (action.get("action") in {"generate_production_plan_draft", "generate_production_plan_arrangement", "compile_production_plan",
                                           "align_production_speech"}
                 and action.get("status") == "completed" and action.get("artifact_status") == "valid")
        review = (action.get("action") == "align_production_speech"
                  and action.get("status") == "blocked" and action.get("artifact_status") == "needs_review")
        if valid or review:
            return True
    return False


def settle_production_processing(ctx: Any, *, canvas_id: str, run_id: str) -> dict[str, Any]:
    """Publish each proven native plan result once; retain conflicts for review."""
    run = read_workflow_run(project_dir=ctx.state_dir, canvas_id=canvas_id, run_id=run_id)
    if not run or run.get("project_id") != ctx.project_id or run.get("actor_id") != ctx.requester_user_id:
        raise ValueError("workflow run is outside the project actor scope")
    if run.get("status") in {"cancelled", "interrupted"}:
        return {"run": run, "published": [], "conflicts": [], "conflict_records": []}
    store = RemixArtifactStore(ctx.output_dir, ctx.project_id, created_by=ctx.requester_user_id)
    published: list[str] = []
    conflicts: list[str] = []
    conflict_records: list[dict[str, Any]] = []
    for action in run.get("actions", []):
        if action.get("action") not in {"generate_production_plan_draft", "generate_production_plan_arrangement", "compile_production_plan", "align_production_speech"}:
            continue
        from .native_execution_context import is_manual_native_run
        if is_manual_native_run(run):
            action = {**action, "_host_execution_source": "canvas_user"}
        valid = action.get("status") == "completed" and action.get("artifact_status") == "valid"
        review = (action.get("action") == "align_production_speech" and action.get("status") == "blocked"
                  and action.get("artifact_status") == "needs_review")
        if not (valid or review):
            continue
        ref = action.get("result_ref") or {}
        if not action.get("processing_operation_id") or not action.get("task_key"):
            continue
        try:
            artifact = store.read(ref["artifact_id"], ref["revision"])
        except (KeyError, ValueError, FileNotFoundError):
            conflicts.append(action["node_id"])
            conflict_records.append(_record_conflict(ctx, canvas_id=canvas_id, run_id=run_id,
                                                     action=action, reason="result_artifact_unavailable"))
            continue
        expected_schema = {"generate_production_plan_draft": "production_plan.v2",
                           "generate_production_plan_arrangement": "production_plan.v2",
                           "compile_production_plan": "composition_manifest.v2",
                           "align_production_speech": "word_timeline.v1"}[action["action"]]
        if artifact.get("schema") != expected_schema or artifact.get("project_id") != ctx.project_id:
            conflicts.append(action["node_id"])
            conflict_records.append(_record_conflict(ctx, canvas_id=canvas_id, run_id=run_id,
                                                     action=action, reason="result_artifact_mismatch"))
            continue
        if action["action"] in {"generate_production_plan_draft", "generate_production_plan_arrangement"}:
            outcome = _publish_plan(ctx, canvas_id=canvas_id, action=action, artifact=artifact, store=store)
        elif action["action"] == "compile_production_plan":
            outcome = _publish_compile(ctx, canvas_id=canvas_id, action=action, artifact=artifact)
        else:
            outcome = _publish_speech(ctx, canvas_id=canvas_id, action=action, artifact=artifact, store=store)
        if outcome == "publish":
            published.append(action["node_id"])
            _clear_conflict(ctx, canvas_id=canvas_id, run_id=run_id, action=action)
            from novelvideo.chat.reference_production_observer import wake_local_observers
            wake_local_observers()
        elif outcome == "conflict":
            conflicts.append(action["node_id"])
            conflict_records.append(_record_conflict(ctx, canvas_id=canvas_id, run_id=run_id,
                                                     action=action, reason="target_changed_or_stopped"))
        elif outcome == "already":
            # Delivery is proven again, so a conflict recorded for an earlier
            # attempt no longer describes the current canvas.
            _clear_conflict(ctx, canvas_id=canvas_id, run_id=run_id, action=action)
        if action["action"] in {"generate_production_plan_draft", "generate_production_plan_arrangement"}:
            log_production_plan_diagnostic(
                "production_plan.publishing", project_id=ctx.project_id, canvas_id=canvas_id,
                node_id=action["node_id"], workflow_run_id=run_id,
                operation_id=str(action.get("processing_operation_id") or ""),
                stage="published" if outcome == "publish" else outcome,
                error_code="target_changed_or_stopped" if outcome == "conflict" else "",
            )
    return {"run": run, "published": published, "conflicts": conflicts,
            "conflict_records": conflict_records}


def settle_native_task_delivery(
    ctx: Any, manager: Any, *, payload: dict[str, Any], task_type: str,
    expected_task_id: str, episode: int, scope: str | None,
) -> dict[str, Any] | None:
    """Use the just-saved task row to settle its bound workflow without a page."""
    if task_type not in {"remix_generate_production_plan_draft", "remix_generate_production_plan_arrangement", "remix_compile_production_plan",
                         "remix_align_production_speech"}:
        return None
    operation_id = str(payload.get("operation_id") or "")
    run_id = str(payload.get("workflow_run_id") or "")
    if not operation_id or not run_id:
        return None
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.freezone.workflow_runs import reconcile_workflow_runs_with_tasks

    operation = RemixOperationStore(ctx.output_dir, ctx.project_id).get(operation_id)
    canvas_id = str(operation.get("canvas_id") or "")
    if not canvas_id or operation.get("task_id") != expected_task_id:
        return None
    task = manager.get_task_for_project(ctx, task_type, episode, scope=scope)
    if task is None or task.task_id != expected_task_id:
        return None
    task_key = manager._task_key_for_state(task)
    reconcile_workflow_runs_with_tasks(
        project_dir=ctx.state_dir, canvas_id=canvas_id, artifact_project_dir=ctx.output_dir,
        tasks_by_key={task_key: {"task_id": task.task_id, "status": task.status,
                                 "result": task.result, "error": task.error}},
    )
    return settle_production_processing(ctx, canvas_id=canvas_id, run_id=run_id)
