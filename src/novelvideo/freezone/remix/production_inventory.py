"""Authorized, bounded host evidence for current production and separate history.

Every result is ephemeral. Discovery does not choose or publish a work package.
The optional production index may later supply IDs; saved artifacts, canvas and
media remain the authority for each response.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
from itertools import islice
import json
from types import SimpleNamespace
from urllib.parse import quote, urlsplit

from .material_execution import (
    canvas_material_resource,
    read_material_state,
    validate_material_native_execution,
)
from .production_state import build_production_state
from .production_plan_diagnostics import collect_plan_diagnostics, measure_plan_speech
from .reference_commission import _read_selectable_work, _result
from .store import RemixArtifactConflict

SCHEMAS = frozenset(
    {
        "reference_commission.v1",
        "reference_decision.v1",
        "material_work_package.v1",
        "speech_correction_preview.v1",
        "material_review.v1",
        "production_output_review.v1",
        "production_plan.v2",
        "production_review.v1",
    }
)
MAX_DISCOVERY = 512
MAX_BRANCHES = 20
MAX_HISTORY = 2048


class _LockedEvidenceStore:
    """Reuse the caller's artifact lease for existing read helpers.

    The local file mutex cannot recursively acquire the same OS lock. This
    read-only adapter runs only within the outer artifact critical section.
    """

    def __init__(self, store, guard):
        self._store, self._guard = store, guard

    def __getattr__(self, name):
        return getattr(self._store, name)

    @contextmanager
    def _locked(self):
        self._guard.reassert()
        yield self._guard

    def read(self, artifact_id, revision):
        return self._store._read(artifact_id, revision)

    def read_latest(self, artifact_id):
        return self._store._read(artifact_id, self._store._latest_revision(artifact_id))


def _ref(saved, project_id):
    return {
        "artifact_id": saved["id"],
        "revision": saved["revision"],
        "project_id": project_id,
    }


def _same_ref(left, right):
    return bool(
        left
        and right
        and (left.get("artifact_id"), left.get("revision"))
        == (right.get("artifact_id"), right.get("revision"))
    )


def _safe(value):
    """Keep browser details free of host paths and credential-bearing metadata."""
    if isinstance(value, dict):
        return {
            key: _safe(item)
            for key, item in value.items()
            if not any(
                secret in key.lower()
                for secret in (
                    "password",
                    "secret",
                    "credential",
                    "authorization",
                    "api_key",
                    "access_token",
                )
            )
            and key
            not in {
                "project_dir",
                "state_dir",
                "output_dir",
                "payload_path",
                "token",
                "bearer",
            }
        }
    if isinstance(value, list):
        return [_safe(item) for item in value]
    if isinstance(value, str):
        if (
            value.startswith("/")
            or "://" in value
            or value.startswith(("file:", "data:", "javascript:"))
        ):
            try:
                parsed = urlsplit(value)
            except ValueError:
                return None
            if parsed.scheme or parsed.netloc:
                return None
            if value.startswith("/") and not value.startswith(
                ("/api/v1/projects/", "/static/projects/")
            ):
                return None
            if parsed.query or parsed.fragment:
                return parsed.path
    return value


def discover_artifact_heads(store):
    """Bound directory/header reads and report incomplete legacy discovery."""
    cached_reader = getattr(store, "production_discovery_heads", None)
    if callable(cached_reader):
        cached = cached_reader()
        if cached is not None:
            return cached
    rows = []
    if not store.root.exists():
        return rows, False
    entries = iter(store.root.iterdir())
    for directory in islice(entries, MAX_DISCOVERY):
        if (
            directory.is_symlink()
            or not directory.is_dir()
            or directory.name.startswith("_")
        ):
            continue
        try:
            revision = store._latest_revision(directory.name)
            header = store._read_json(store._path(directory.name, revision, ".json"))
            if (
                header.get("schema") not in SCHEMAS
                or header.get("project_id") != store.project_id
                or header.get("created_by") != store.created_by
            ):
                continue
            rows.append(header)
        except (ValueError, FileNotFoundError):
            continue
    rows = sorted(rows, key=lambda row: row["id"])
    truncated = next(entries, None) is not None
    capture = getattr(store, "capture_production_heads", None)
    if callable(capture):
        capture(rows, truncated)
    return rows, truncated


def _artifact(store, artifact_id):
    saved = store._read(artifact_id, store._latest_revision(artifact_id))
    if saved["created_by"] != store.created_by:
        raise PermissionError("artifact belongs to another actor")
    return saved


def _commission(store, commission_id, canvas):
    saved = _artifact(store, commission_id)
    if saved["schema"] != "reference_commission.v1":
        raise ValueError("reference commission required")
    c = _result(saved)
    if (c["project_id"], c["canvas_id"], c["requester_user_id"]) != (
        store.project_id,
        canvas["canvas_id"],
        store.created_by,
    ):
        raise PermissionError("commission belongs to another canvas or actor")
    if not any(n["id"] == c["analysis_node_id"] for n in canvas.get("nodes", [])):
        raise RemixArtifactConflict("commission analysis is not on the current canvas")
    return c


def _current_plan(store, c, canvas):
    if not c.get("plan_ref"):
        return None
    pinned_ref = c["plan_ref"]
    latest = _artifact(store, pinned_ref["artifact_id"])
    if (latest["schema"] != "production_plan.v2"
            or latest["revision"] < pinned_ref["revision"]):
        raise RemixArtifactConflict("commission plan is not the current saved revision")
    nodes = [
        n
        for n in canvas.get("nodes", [])
        if n.get("type") == "scriptNode"
        and (n.get("data", {}).get("productionPlanV2Ref") or {}).get(
            "artifactId",
            (n.get("data", {}).get("productionPlanV2Ref") or {}).get("artifact_id"),
        )
        == latest["id"]
    ]
    if len(nodes) != 1:
        raise RemixArtifactConflict("commission plan is not on the current canvas")
    data = nodes[0].get("data", {})
    ref = data.get("productionPlanV2Ref") or {}
    if ref.get("project_id", store.project_id) != store.project_id or data.get("productionPlanV2Dirty") is True:
        raise RemixArtifactConflict(
            "canvas plan differs from the current saved revision"
        )
    canvas_revision = ref.get("revision")
    if canvas_revision == latest["revision"]:
        saved = latest
        sync_status = "pending_commission_sync" if canvas_revision != pinned_ref["revision"] else None
    elif canvas_revision == pinned_ref["revision"] and latest["revision"] > canvas_revision:
        saved = store._read(latest["id"], canvas_revision)
        sync_status = "pending_canvas_save"
    else:
        raise RemixArtifactConflict("canvas plan differs from the current saved revision")
    reachable = {c["analysis_node_id"]}
    for _ in canvas.get("nodes", []):
        children = {
            e.get("target")
            for e in canvas.get("edges", [])
            if e.get("source") in reachable
        }
        if children <= reachable:
            break
        reachable |= children
    if nodes[0]["id"] not in reachable:
        raise RemixArtifactConflict("plan is not derived from the commission analysis")
    if sync_status:
        saved = {**saved, "plan_revision_sync": {
            "status": sync_status,
            "plan_ref": _ref(latest, store.project_id),
            "canvas_plan_ref": _ref(saved, store.project_id),
            "commission_plan_ref": pinned_ref,
        }}
    return saved


def _candidate_refs(store, heads, c, canvas):
    eligible = []
    for head in heads:
        if head["schema"] != "material_work_package.v1":
            continue
        try:
            ref = _ref(head, store.project_id)
            _read_selectable_work(store, c, ref, canvas)
            eligible.append(ref)
        except (ValueError, PermissionError, FileNotFoundError):
            continue
        if len(eligible) >= 20:
            break
    return eligible


def _plan_review_is_valid(store, c, plan):
    """Only the pinned review identity can revoke its original approval."""
    ref = c.get("review_ref")
    if not ref:
        return True
    try:
        from .production_review import production_decision_fingerprint

        original = store._read(ref["artifact_id"], ref["revision"])
        current = _artifact(store, ref["artifact_id"])
        return bool(
            plan
            and original["schema"] == current["schema"] == "production_review.v1"
            and original["created_by"] == store.created_by
            and original["payload"]["status"]
            == current["payload"]["status"]
            == "passed"
            and original["payload"]["decision_fingerprint"]
            == current["payload"]["decision_fingerprint"]
            == production_decision_fingerprint(plan["payload"])
            and original["payload"]["direction_fingerprint"]
            == current["payload"]["direction_fingerprint"]
            == c["direction_fingerprint"]
        )
    except (ValueError, FileNotFoundError, PermissionError, KeyError):
        return False


def _action(identity, kind, owner, basis, step_id=None):
    return {
        "action_id": identity,
        "kind": kind,
        "owner": owner,
        "basis_ref": basis,
        "step_id": step_id,
        "status": "pending",
    }


def _material_evidence(store, work, canvas, canvas_id, state_dir, agent_id='main'):
    """Resolve actual media and original reviews before deriving each unique action."""
    discovered = read_material_state(
        store,
        {"artifact_id": work["id"], "revision": work["revision"]},
        canvas=canvas,
        canvas_id=canvas_id,
        include_workflow_intent=False,
        workflow_state_dir=state_dir,
        agent_id=agent_id,
    )
    reviews = [
        r
        for r in discovered.get("material_reviews", [])
        if r["review_compatible_with_current_work"] is True
    ]
    facts, actions, pending_reviews, steps = [], [], [], []
    basis = _ref(work, store.project_id)
    for step in work["payload"]["steps"]:
        resources = []
        node_ids = [
            step.get("node_id"),
            (step.get("accepted_resource") or {}).get("node_id"),
        ]
        node_ids += [
            r["resource"]["node_id"] for r in reviews if r["step_id"] == step["id"]
        ]
        node_ids += [
            (r.get("resource") or {}).get("node_id", r.get("node_id"))
            for r in step.get("candidates", [])
            if not r.get("attempt_history")
        ]
        for node_id in dict.fromkeys(n for n in node_ids if n):
            try:
                resource = canvas_material_resource(
                    store, canvas, node_id, step["media_kind"]
                )
                if step["source"] == "generate":
                    validate_material_native_execution(
                        store,
                        canvas,
                        step,
                        node_id,
                        require_complete=True,
                        workflow_state_dir=state_dir,
                        work_ref={
                            "artifact_id": work["id"], "revision": work["revision"]
                        },
                    )
                resources.append(resource)
            except (ValueError, FileNotFoundError):
                continue
        # A saved acceptance is valid only while its exact original review is
        # still the review head, authorized, passed, and measures current media.
        accepted = False
        if (
            step.get("status") == "accepted"
            and step.get("review_ref")
            and step.get("accepted_resource") in resources
        ):
            try:
                from .material_review import validate_material_review_lineage

                ref = step["review_ref"]
                saved = _artifact(store, ref["artifact_id"])
                payload = saved["payload"]
                decision = payload.get("user_decision")
                validate_material_review_lineage(store, payload, saved["upstream_refs"])
                accepted = (
                    saved["schema"] == "material_review.v1"
                    and _same_ref(_ref(saved, store.project_id), ref)
                    and payload["step_id"] == step["id"]
                    and payload["work_ref"]["artifact_id"] == work["id"]
                    and payload["work_ref"]["revision"] <= work["revision"]
                    and payload["resource"] == step["accepted_resource"]
                    and payload["technical"]["status"]
                    == payload["content"]["status"]
                    == "passed"
                    and (
                        not decision
                        or (decision["canvas_id"], decision["requester_user_id"])
                        == (canvas_id, store.created_by)
                    )
                )
            except (ValueError, FileNotFoundError, PermissionError, KeyError):
                accepted = False
        relevant = [
            r
            for r in reviews
            if r["step_id"] == step["id"] and r["resource"] in resources
        ]
        passed = [
            r
            for r in relevant
            if r["technical"]["status"] == r["content"]["status"] == "passed"
        ]
        # A compatible original pass wins over a later duplicate unavailable
        # review for the same resource. A changed hash cannot use that pass.
        unavailable = [
            r
            for r in relevant
            if r["technical"]["status"] == "passed"
            and r["content"]["status"] == "unavailable"
            and not any(p["resource"] == r["resource"] for p in passed)
        ]
        chosen = next(
            iter(
                sorted(
                    unavailable,
                    key=lambda r: (
                        r["review_ref"]["artifact_id"],
                        r["review_ref"]["revision"],
                    ),
                )
            ),
            None,
        )
        if not accepted:
            if chosen:
                pending_reviews.append(chosen)
                actions.append(
                    _action(
                        chosen["review_ref"]["artifact_id"],
                        "review_materials",
                        "user",
                        basis,
                        step["id"],
                    )
                )
            else:
                kind = (
                    "accept_materials"
                    if passed
                    else "check_materials"
                    if resources
                    else "prepare_materials"
                )
                actions.append(
                    _action("step:" + step["id"], kind, "agent", basis, step["id"])
                )
        facts.append(
            {"step_id": step["id"], "available": bool(resources), "accepted": accepted}
        )
        steps.append(
            {
                "step_id": step["id"],
                "status": "accepted"
                if accepted
                else "needs_review"
                if chosen
                else "checked"
                if passed
                else "checking"
                if resources
                else "needs_rework"
                if step["status"] == "rejected"
                else "preparing",
                "node_id": resources[0]["node_id"]
                if resources
                else step.get("node_id"),
                "media_hash": resources[0]["media_hash"] if resources else None,
                "preview_url": "/api/v1/projects/"
                + quote(store.project_id, safe="")
                + "/media/"
                + quote(resources[0]["media_path"], safe="/")
                if resources
                else None,
            }
        )
    evidence = discovered.get('material_execution') or {}
    draft_actions = [row['next_action'] for row in evidence.get('drafts', []) if row.get('next_action')]
    if draft_actions:
        actions = [row for row in actions if row['kind'] != 'prepare_materials']
        actions.extend({**action, 'basis_ref': basis, 'status': 'pending'} for action in draft_actions)
    return facts, actions, pending_reviews, steps, discovered.get('material_reviews_truncated', False)


def _details(store, heads, c, work, canvas, *, pending_material_reviews=()):
    result = {
        "commissions": [_safe(c)],
        "decisions": [],
        "material_work": [
            _safe({**work["payload"], "work_ref": _ref(work, store.project_id)})
        ]
        if work
        else [],
        "speech_corrections": [],
        "material_reviews": [],
        "output_reviews": [],
    }
    phases = {
        "direction": "awaiting_direction",
        "plan": "awaiting_plan_confirmation",
        "material": "preparing_materials",
        "composition": "reviewing_output",
    }
    for head in heads:
        schema = head["schema"]
        if schema not in {
            "reference_decision.v1",
            "speech_correction_preview.v1",
            "production_output_review.v1",
        }:
            continue
        try:
            saved = _artifact(store, head["id"])
            row = saved["payload"]
            if schema == "reference_decision.v1":
                if (
                    not _same_ref(row["commission_ref"], c["commission_ref"])
                    or row["status"] != "pending"
                    or c["phase"] != phases[row["kind"]]
                ):
                    continue
                proposal = row["proposal"]
                if row["kind"] == "plan" and not _same_ref(
                    proposal.get("plan_ref"), c.get("plan_ref")
                ):
                    continue
                if row["kind"] == "material" and (
                    not work
                    or not _same_ref(
                        proposal.get("work_ref"), _ref(work, store.project_id)
                    )
                ):
                    continue
                if row["kind"] == "material":
                    candidate = proposal.get("candidate")
                    if (
                        not isinstance(candidate, dict)
                        or not {"step_id", "review_ref", "media_hash"}
                        <= candidate.keys()
                        or set(candidate)
                        - {
                            "step_id",
                            "review_ref",
                            "media_hash",
                            "duration_us",
                            "has_audio",
                            "node_label",
                        }
                        or set(proposal)
                        - {
                            "work_ref",
                            "plan_ref",
                            "candidate",
                            "acceptance_note",
                            "next_action",
                        }
                        or not _same_ref(proposal.get("plan_ref"), c.get("plan_ref"))
                        or not _same_ref(work["payload"]["plan_ref"], c.get("plan_ref"))
                    ):
                        continue
                    current = next(
                        (
                            review
                            for review in pending_material_reviews
                            if _same_ref(review["review_ref"], candidate["review_ref"])
                            and _same_ref(review["work_ref"], proposal["work_ref"])
                            and review["step_id"] == candidate["step_id"]
                            and review["resource"]["media_hash"]
                            == candidate["media_hash"]
                        ),
                        None,
                    )
                    if not current or any(
                        key in candidate
                        and (
                            type(candidate[key]) is not kind
                            or candidate[key] != current["resource"].get(key)
                        )
                        for key, kind in (("duration_us", int), ("has_audio", bool))
                    ):
                        continue
                if row["kind"] == "composition" and not _same_ref(
                    proposal.get("manifest_ref"), c.get("manifest_ref")
                ):
                    continue
                result["decisions"].append(
                    _safe({**row, "decision_ref": _ref(saved, store.project_id)})
                )
            elif schema == "speech_correction_preview.v1":
                if _same_ref(row["source_plan_ref"], c.get("plan_ref")):
                    from .agent_speech_review import _source

                    _, _, resource, turns, words = _source(
                        store,
                        row["source_plan_ref"],
                        row["take_id"],
                        row["recognition_ref"],
                    )
                    current = canvas_material_resource(
                        store, canvas, resource["node_id"], resource["media_kind"]
                    )
                    if any(
                        current.get(key) != resource.get(key)
                        for key in (
                            "node_id",
                            "media_hash",
                            "media_path",
                            "media_kind",
                            "duration_us",
                        )
                    ):
                        continue
                    plan_node = next(
                        n
                        for n in canvas.get("nodes", [])
                        if n.get("type") == "scriptNode"
                        and (n.get("data", {}).get("productionPlanV2Ref") or {}).get(
                            "artifactId",
                            (n.get("data", {}).get("productionPlanV2Ref") or {}).get(
                                "artifact_id"
                            ),
                        )
                        == row["source_plan_ref"]["artifact_id"]
                    )
                    selected_ids = {change["word_id"] for change in row["corrections"]}
                    result["speech_corrections"].append(
                        _safe(
                            {
                                **row,
                                "preview_ref": _ref(saved, store.project_id),
                                "node_id": plan_node["id"],
                                "source_node_id": resource["node_id"],
                                "source_url": "/api/v1/projects/"
                                + quote(store.project_id, safe="")
                                + "/media/"
                                + quote(resource["media_path"], safe="/"),
                                "original_words": [
                                    word for word in words if word["id"] in selected_ids
                                ],
                                "target_text": "\n".join(
                                    (
                                        turn.get("speaker", "") + "："
                                        if turn.get("speaker")
                                        else ""
                                    )
                                    + turn["text"]
                                    for turn in turns
                                ),
                            }
                        )
                    )
            elif (
                row["status"] == "needs_review"
                and _same_ref(row["commission_ref"], c["commission_ref"])
                and _same_ref(row["plan_ref"], c.get("plan_ref"))
                and _same_ref(row["manifest_ref"], c.get("manifest_ref"))
            ):
                from types import SimpleNamespace
                from .production_output_review import _observe

                observed = _observe(
                    store,
                    SimpleNamespace(
                        project_id=store.project_id, requester_user_id=store.created_by
                    ),
                    canvas,
                    c["commission_ref"],
                    row["compose_node_id"],
                )
                _, manifest_ref, plan_ref, operation_id, url, media, technical = (
                    observed
                )
                if (
                    not _same_ref(row["manifest_ref"], manifest_ref)
                    or not _same_ref(row["plan_ref"], plan_ref)
                    or row["operation_id"] != operation_id
                    or row["media_url"] != url
                    or row["media"] != media
                    or technical["status"] != "passed"
                ):
                    continue
                result["output_reviews"].append(
                    _safe({**row, "review_ref": _ref(saved, store.project_id)})
                )
        except (
            ValueError,
            FileNotFoundError,
            PermissionError,
            KeyError,
            StopIteration,
        ):
            continue
    return result


def _execution(
    c, actions, runs, canvas_id, agent_id, requester, store, work, state_dir
):
    matching = [
        r
        for r in runs
        if (
            r.get("project_id"),
            r.get("canvas_id"),
            r.get("requester_user_id"),
            r.get("agent_id", "main"),
        )
        == (store.project_id, canvas_id, requester, agent_id)
        and (r.get("commission_ref") or {}).get("artifact_id")
        == c["commission_ref"]["artifact_id"]
    ]
    # The coordinator query orders newest durable records first, never file mtime.
    run = matching[0] if matching else None
    status = run.get("status") if run else None
    if status in {"stopped", "cancelled", "canceled"}:
        return {"status": "stopped", "blockers": []}
    if run and run.get('manual_resume_required'):
        return {'status': 'failed', 'blockers': [{'owner': 'user', 'kind': 'resume_execution', 'action_id': run.get('run_id')}]}
    if work and c['phase'] == 'preparing_materials':
        from .material_execution import read_material_execution_evidence
        evidence = read_material_execution_evidence(work['payload'], project_id=store.project_id,
            canvas_id=canvas_id, state_dir=state_dir, requester_user_id=requester, agent_id=agent_id)
        if any(row['status'] == 'cancelled' for row in evidence['drafts']):
            return {'status': 'stopped', 'blockers': []}
        if evidence['problems']:
            return {'status': 'failed', 'blockers': [
                {'owner': 'agent', 'kind': row['kind'], 'action_id': row['draft_id']}
                for row in evidence['problems']], 'problems': evidence['problems']}
    workflow_states, native_problems = [], []
    if work:
        from novelvideo.freezone.workflow_runs import read_workflow_run

        saved_runs = {}
        for step in work["payload"]["steps"]:
            execution = (step.get("execution") or {}).get("run_id")
            if not execution:
                continue
            if execution not in saved_runs:
                saved_runs[execution] = read_workflow_run(
                    project_dir=state_dir, canvas_id=canvas_id, run_id=execution
                )
            saved = saved_runs[execution]
            if saved and saved.get("project_id") == store.project_id:
                native_status = saved.get("status")
                workflow_states.append(native_status)
                if native_status in {"failed", "interrupted"}:
                    # Native failures belong to the selected work. Publish a
                    # curated message, never provider payloads or signed URLs.
                    native_problems.append(
                        {
                            "run_id": execution,
                            "step_id": step["id"],
                            "status": native_status,
                            "message": "Native material execution failed."
                            if native_status == "failed"
                            else "Native material execution was interrupted.",
                        }
                    )
    if any(s in {"cancelled", "stopped"} for s in workflow_states):
        return {"status": "stopped", "blockers": []}
    if not native_problems and (
        (c['phase'] != 'preparing_materials' and status in {"submitting", "analyzing", "analysis_ready", "resuming"})
        or any(s in {"running", "queued", "pending"} for s in workflow_states)
    ):
        return {"status": "running", "blockers": []}
    blocker = (run or {}).get("execution_blocker") or {}
    blocker_current = bool(
        run
        and _same_ref(run.get("commission_ref"), c["commission_ref"])
        and run.get("commission_phase", status) == c["phase"]
    )
    user_required = blocker_current and blocker.get("code") == "user_required"
    if user_required:
        actions[:] = [action for action in actions if action["owner"] == "user"]
        actions.append(
            _action(
                "execution:" + run["run_id"],
                "resolve_execution_blocker",
                "user",
                c["commission_ref"],
            )
        )
    if native_problems:
        failed_steps = {problem["step_id"] for problem in native_problems}
        actions[:] = [
            action
            for action in actions
            if action["owner"] == "user" or action.get("step_id") not in failed_steps
        ]
        return {
            "status": "failed",
            "blockers": [
                {"owner": "agent", "kind": "recover_execution", "action_id": run_id}
                for run_id in sorted({problem["run_id"] for problem in native_problems})
            ],
            "problems": native_problems,
        }
    if user_required:
        return {"status": "waiting_user", "blockers": []}
    users = any(a["owner"] == "user" for a in actions)
    if users:
        return {"status": "waiting_user", "blockers": []}
    if (
        work is not None
        and not blocker_current
        and status in {"resume_failed", "analysis_failed", "stale"}
        and not any(s in {"failed", "interrupted"} for s in workflow_states)
    ):
        return {"status": "waiting_agent" if actions else "idle", "blockers": []}
    if status in {"resume_failed", "analysis_failed", "failed", "stale"} or any(
        s in {"failed", "interrupted"} for s in workflow_states
    ):
        return {
            "status": "failed",
            "blockers": [
                {"owner": "system", "kind": "recover_execution", "action_id": None}
            ],
        }
    if c["phase"] == "completed" and status == "completed":
        return {"status": "completed", "blockers": []}
    return {"status": "waiting_agent" if actions else "idle", "blockers": []}


def read_current_production(
    store,
    *,
    canvas,
    canvas_id,
    agent_id,
    requester_user_id,
    runs,
    state_dir,
    commission_id=None,
):
    """Read under host canvas guard; authorize each saved head under artifact mutex."""
    if (
        canvas.get("access_model") in {"private", "user_private"}
        and canvas.get("owner_principal_id") != requester_user_id
    ):
        raise PermissionError("private canvas access denied")
    if (canvas.get("project_id"), canvas.get("canvas_id"), store.created_by) != (
        store.project_id,
        canvas_id,
        requester_user_id,
    ):
        raise PermissionError("host production scope differs")
    with store._locked() as guard:
        store = _LockedEvidenceStore(store, guard)
        heads, truncated = discover_artifact_heads(store)
        if commission_id:
            selected = [_commission(store, commission_id, canvas)]
        else:
            selected = []
            for head in heads:
                if head["schema"] != "reference_commission.v1":
                    continue
                try:
                    c = _commission(store, head["id"], canvas)
                    if c.get("plan_branches") and not c.get("plan_ref"):
                        continue
                    _current_plan(store, c, canvas)
                    selected.append(c)
                except (ValueError, FileNotFoundError, PermissionError):
                    continue
                if len(selected) >= MAX_BRANCHES:
                    truncated = True
                    break
        states = []
        for c in selected:
            plan = _current_plan(store, c, canvas)
            review_valid = _plan_review_is_valid(store, c, plan)
            candidates = (
                _candidate_refs(store, heads, c, canvas)
                if review_valid
                and c["phase"] == "preparing_materials"
                and not c.get("work_ref")
                else []
            )
            work = None
            if c.get("work_ref"):
                try:
                    saved = _artifact(store, c["work_ref"]["artifact_id"])
                    _, work = _read_selectable_work(
                        store, c, _ref(saved, store.project_id), canvas
                    )
                except (ValueError, FileNotFoundError, PermissionError):
                    work = None
            facts, actions, pending_reviews, steps, review_truncated = (
                _material_evidence(store, work, canvas, canvas_id, state_dir, agent_id)
                if work
                else ([], [], [], [], False)
            )
            details = _details(
                # Candidate suggestions remain observations, never adoption.
                store, heads, c, work, canvas, pending_material_reviews=pending_reviews
            )
            if c['phase'] == 'preparing_materials' and not c.get('work_ref'):
                actions.append(_action('select-work:' + c['commission_ref']['artifact_id'],
                    'work_selection_required' if candidates else 'prepare_materials',
                    'user' if candidates else 'agent', c['commission_ref']))
            # Keep original review provenance while supplying the current work
            # identity for candidate inspection/explicit batch actions. Compact
            # material-state rows are not the browser's full review contract.
            displayed_reviews = []
            for pending in pending_reviews:
                saved_review = store.read(pending["review_ref"]["artifact_id"],
                                          pending["review_ref"]["revision"])
                displayed_reviews.append({**pending,
                    "current_work_ref": {"artifact_id": work["id"], "revision": work["revision"]},
                    "technical": saved_review["payload"]["technical"],
                    "content": saved_review["payload"]["content"]})
            details["material_reviews"] = _safe(displayed_reviews)
            for decision in details["decisions"]:
                # The step-specific discovery above owns material pending actions.
                if decision["kind"] == "material":
                    continue
                kind = {
                    "direction": "confirm_direction",
                    "plan": "confirm_plan",
                    "composition": "confirm_output",
                }[decision["kind"]]
                actions.append(
                    _action(
                        decision["decision_ref"]["artifact_id"],
                        kind,
                        "user",
                        c["commission_ref"],
                    )
                )
            for preview in details["speech_corrections"]:
                actions.append(
                    _action(
                        preview["preview_ref"]["artifact_id"],
                        "review_speech_correction",
                        "user",
                        c["plan_ref"],
                    )
                )
            for review in details["output_reviews"]:
                actions.append(
                    _action(
                        review["review_ref"]["artifact_id"],
                        "review_output",
                        "user",
                        c["commission_ref"],
                    )
                )
            if not review_valid:
                actions = [
                    _action(
                        "plan-review:" + c["review_ref"]["artifact_id"],
                        "review_production_plan",
                        "agent",
                        c["commission_ref"],
                    )
                ]
                for key in (
                    "decisions",
                    "material_reviews",
                    "speech_corrections",
                    "output_reviews",
                ):
                    details[key] = []
            execution = _execution(
                c,
                actions,
                runs,
                canvas_id,
                agent_id,
                requester_user_id,
                store,
                work,
                state_dir,
            )
            # The projection is pinned to the commission's published plan.
            # A newer canvas edit is exposed separately via plan_revision_sync
            # until the commission catches up; it is not yet its plan fact.
            projected_plan = (
                None
                if plan and (plan.get("plan_revision_sync") or {}).get("status") == "pending_commission_sync"
                else plan
            )
            state = build_production_state(
                scope={
                    "project_id": store.project_id,
                    "canvas_id": canvas_id,
                    "agent_id": agent_id,
                },
                commission=c,
                plan={**projected_plan["payload"], "plan_ref": _ref(projected_plan, store.project_id)}
                if projected_plan
                else None,
                active_work={
                    **work["payload"],
                    "work_ref": _ref(work, store.project_id),
                }
                if work
                else None,
                material_facts=facts,
                pending_actions=actions,
                execution=execution,
            )
            state.update(
                director_state=details,
                analysis_node_id=c["analysis_node_id"],
                material_steps=steps,
                candidate_work_refs=candidates,
                suggested_work_ref=candidates[0] if len(candidates) == 1 else None,
                inventory_truncated=truncated or review_truncated,
                plan_review_valid=review_valid,
                plan_revision_sync=plan.get("plan_revision_sync") if plan else None,
                execution_problem=execution.get("problems", []),
                speech_estimates=measure_plan_speech(plan["payload"]) if plan else [],
                plan_diagnostics=collect_plan_diagnostics(plan["payload"]) if plan else [],
            )
            if c.get('direction'):
                from .direction_contract import resolve_direction_review_contract
                contract = resolve_direction_review_contract(c['direction'])
                state['review_context'] = {
                    'direction_revision': c['direction_revision'],
                    'direction_fingerprint': c['direction_fingerprint'],
                    'goal': contract['goal'],
                    'requirements': contract['requirements'],
                    'exact_turns': contract['exact_turns'],
                    'preserve_replace': contract['preserve_replace'],
                }
                state['visual_preservation_status'] = (
                    'confirmed' if 'preserve_replace' in c['direction'] else 'unverified_legacy'
                )
            source_run = next((run for run in runs
                if (run.get("commission_ref") or {}).get("artifact_id") == c["commission_ref"]["artifact_id"]
                and run.get("analysis_node_id") == c["analysis_node_id"]
                and run.get("project_id") == store.project_id
                and run.get("canvas_id") == canvas_id
                and run.get("requester_user_id") == requester_user_id
                and run.get("agent_id") == agent_id
                and run.get("source_node_id") and run.get("run_id")), None)
            if source_run:
                state.update(source_node_id=source_run["source_node_id"], run_id=source_run["run_id"])
            from .production_advancement import enrich_production_state

            state = enrich_production_state(store, state, canvas=canvas, workflow_state_dir=state_dir)
            if c["phase"] == "drafting_plan":
                from .native_plan_stage import read_native_plan_stage

                native_stage = read_native_plan_stage(
                    ctx=SimpleNamespace(state_dir=state_dir, output_dir=store.project_dir, project_id=store.project_id, is_home_node=True,
                                        requester_user_id=requester_user_id),
                    canvas=canvas, commission=c, artifact_reader=store,
                )
                from .production_state import native_plan_execution_status

                state["native_plan_stage"] = native_stage
                if state["execution_status"] not in {"stopped", "failed", "recovering"}:
                    state["execution_status"] = native_plan_execution_status(native_stage)
                state["next_action"] = native_stage["next_action"]
            state["projection_revision"] = hashlib.sha256(
                json.dumps(
                    state, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                ).encode()
            ).hexdigest()
            states.append(state)
        return (
            states[0]
            if commission_id
            else {
                "branches": states,
                "selection_required": len(states) > 1,
                "inventory_truncated": truncated,
            }
        )


def _history_related(row, c, work_ids, plan_ids):
    schema, p = row["schema"], row["payload"]
    if schema == "reference_commission.v1":
        return row["id"] == c["commission_ref"]["artifact_id"]
    if schema in {"reference_decision.v1", "production_output_review.v1"}:
        return (p.get("commission_ref") or {}).get("artifact_id") == c[
            "commission_ref"
        ]["artifact_id"]
    if schema == "production_review.v1":
        return (p.get("direction_ref") or {}).get("artifact_id") == c["commission_ref"][
            "artifact_id"
        ]
    if schema == "material_review.v1":
        return p["work_ref"]["artifact_id"] in work_ids
    if schema == "material_work_package.v1":
        return p["plan_ref"]["artifact_id"] in plan_ids
    if schema == "production_plan.v2":
        return row["id"] in plan_ids
    if schema == "speech_correction_preview.v1":
        return p["source_plan_ref"]["artifact_id"] in plan_ids
    return False


def read_production_history(
    store,
    *,
    canvas,
    canvas_id,
    agent_id,
    requester_user_id,
    commission_id,
    cursor=None,
    limit=20,
):
    if not 1 <= limit <= 100:
        raise ValueError("history limit must be between 1 and 100")
    scope = {
        "project_id": store.project_id,
        "canvas_id": canvas_id,
        "agent_id": agent_id,
        "requester_user_id": requester_user_id,
        "commission_id": commission_id,
    }
    scope_hash = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
    after = None
    if cursor:
        try:
            if len(cursor) > 1024:
                raise ValueError
            token = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            if (
                set(token) != {"scope", "after"}
                or token["scope"] != scope_hash
                or not isinstance(token["after"], list)
                or len(token["after"]) != 3
            ):
                raise ValueError
            if (
                not isinstance(token["after"][0], str)
                or not isinstance(token["after"][1], str)
                or type(token["after"][2]) is not int
            ):
                raise ValueError
            after = tuple(token["after"])
        except (ValueError, UnicodeError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid scoped history cursor") from exc
    with store._locked():
        c = _commission(store, commission_id, canvas)
        heads, truncated = discover_artifact_heads(store)
        plan_ids = {c["plan_ref"]["artifact_id"]} if c.get("plan_ref") else set()
        plan_ids.update(
            row["plan_ref"]["artifact_id"]
            for row in c.get("decisions", [])
            if row.get("plan_ref")
        )
        work_ids = {c["work_ref"]["artifact_id"]} if c.get("work_ref") else set()
        for head in heads:
            if head["schema"] != "material_work_package.v1":
                continue
            try:
                work = _artifact(store, head["id"])
                if work["payload"]["plan_ref"]["artifact_id"] in plan_ids:
                    work_ids.add(work["id"])
            except (ValueError, FileNotFoundError, PermissionError):
                continue
        items, reads = [], 0
        for head in heads:
            for revision in range(1, head["revision"] + 1):
                if reads >= MAX_HISTORY:
                    truncated = True
                    break
                reads += 1
                try:
                    saved = store._read(head["id"], revision)
                    if saved["created_by"] != requester_user_id or not _history_related(
                        saved, c, work_ids, plan_ids
                    ):
                        continue
                    key = (saved.get("created_at", ""), saved["id"], revision)
                    if after is not None and key <= after:
                        continue
                    items.append(
                        {
                            "id": saved["id"],
                            "revision": revision,
                            "created_at": saved.get("created_at", ""),
                            "schema": saved["schema"],
                            "payload": _safe(saved["payload"]),
                        }
                    )
                except (ValueError, FileNotFoundError):
                    continue
            if reads >= MAX_HISTORY:
                break
        items.sort(key=lambda row: (row["created_at"], row["id"], row["revision"]))
        page = items[:limit]
        next_cursor = None
        if len(items) > limit:
            last = page[-1]
            token = {
                "scope": scope_hash,
                "after": [last["created_at"], last["id"], last["revision"]],
            }
            next_cursor = base64.urlsafe_b64encode(json.dumps(token).encode()).decode()
        return {
            "items": page,
            "next_cursor": next_cursor,
            "history_truncated": truncated,
            "ordering_provenance": "artifact_id_revision",
        }
