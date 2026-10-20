"""Host-owned proposal and decision receipts for reference production."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

from .reference_commission import AuthenticatedDecisionEvidence, advance_reference_commission, read_reference_commission


class ReferenceDecisionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    commission_ref: dict[str, Any]
    kind: Literal["direction", "plan", "material", "composition"]
    summary: str = Field(min_length=1, max_length=10000)
    proposal: dict[str, Any]
    command_id: str = Field(min_length=1, max_length=128)
    status: Literal["pending", "confirmed", "superseded"] = "pending"
    receipt_id: str | None = None
    decided_by: str | None = None


def reviewed_plan_command_id(commission: dict) -> str:
    identity = [commission['commission_ref'], commission['plan_ref'], commission['review_ref']]
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':')).encode()
    return 'reviewed-plan-' + hashlib.sha256(encoded).hexdigest()[:32]


def prepare_reference_decision(store, commission_ref: dict, kind: str, proposal: dict, summary: str, command_id: str) -> dict:
    commission = read_reference_commission(store, commission_ref, latest=True)
    if kind == 'direction':
        from .direction_contract import resolve_direction_review_contract
        resolve_direction_review_contract(proposal)
    if commission["commission_ref"]["revision"] != commission_ref["revision"]:
        raise ValueError("commission changed before preparing decision")
    if kind == "direction" and commission["phase"] != "awaiting_direction":
        if proposal == commission.get('direction'):
            raise ValueError('direction is unchanged; reuse its existing confirmed receipt')
        from .reference_commission import ReferenceCommission, _result
        current = store.read_latest(commission_ref['artifact_id'])
        data = ReferenceCommission.model_validate(current['payload']).model_dump(mode='json')
        data['phase'] = 'awaiting_direction'
        commission = _result(store.revise(current['id'], current['revision'], data, current['upstream_refs']))
        commission_ref = commission['commission_ref']
    if kind == "plan" and commission["phase"] != "awaiting_plan_confirmation":
        raise ValueError("plan must be reviewed before user confirmation")
    if kind == "plan" and proposal.get("plan_ref") != commission["plan_ref"]:
        raise ValueError(
            "proposal must match reviewed plan; for kind=plan copy the complete current "
            "commission.plan_ref verbatim into proposal.plan_ref, including project_id; "
            "do not trim fields or use stale refs"
        )
    if kind == 'plan' and commission.get('review_ref'):
        canonical_command = reviewed_plan_command_id(commission)
        canonical_id = hashlib.sha256((commission_ref['artifact_id'] + ':decision:'
                                       + canonical_command).encode()).hexdigest()[:32]
        if command_id != canonical_command:
            try:
                existing = store.read_latest(canonical_id)
            except FileNotFoundError:
                existing = None
            if (existing and existing['schema'] == 'reference_decision.v1'
                    and existing['created_by'] == store.created_by
                    and existing['payload']['commission_ref'] == commission['commission_ref']
                    and existing['payload']['proposal']['plan_ref'] == commission['plan_ref']
                    and existing['payload']['status'] == 'pending'):
                return {'decision_ref': {'artifact_id': existing['id'],
                                         'revision': existing['revision']},
                        'decision': existing['payload']}
    if kind == "composition" and (commission['phase'] != 'reviewing_output' or commission['production_scope'] != '可编辑合成' or proposal.get('manifest_ref') != commission.get('manifest_ref')):
        raise ValueError("composition proposal must match current editable manifest")
    payload = ReferenceDecisionV1(commission_ref=commission_ref, kind=kind, proposal=proposal,
                                  summary=summary, command_id=command_id).model_dump(mode="json", exclude_none=True)
    artifact_id = hashlib.sha256((commission_ref["artifact_id"] + ":decision:" + command_id).encode()).hexdigest()[:32]
    with store._locked() as guard:
        try:
            old = store._read(artifact_id, 1)
        except FileNotFoundError:
            old = None
        if old:
            if old["payload"] != payload:
                raise ValueError("decision command_id reused for different proposal")
        else:
            refs = store._reference_list([commission_ref], store.project_id)
            store._publish(artifact_id=artifact_id, schema="reference_decision.v1", revision=1, payload=payload,
                           upstream_refs=refs, created_by=store.created_by, guard=guard)
            if kind == 'direction':
                # A revised conversational direction replaces the earlier
                # proposal before a short user reply can confirm it.
                for directory in store.root.iterdir():
                    if directory.name == artifact_id or directory.is_symlink() or not directory.is_dir() or directory.name.startswith('_'):
                        continue
                    try:
                        prior = store._read(directory.name, store._latest_revision(directory.name))
                    except (ValueError, FileNotFoundError):
                        continue
                    prior_payload = prior['payload']
                    if (prior['schema'] != 'reference_decision.v1' or prior['created_by'] != store.created_by
                            or prior_payload['kind'] != 'direction' or prior_payload['status'] != 'pending'
                            or prior_payload['commission_ref'] != commission_ref):
                        continue
                    superseded = ReferenceDecisionV1.model_validate(
                        {**prior_payload, 'status': 'superseded'}).model_dump(mode='json', exclude_none=True)
                    store._publish(artifact_id=prior['id'], schema=prior['schema'], revision=prior['revision'] + 1,
                                   payload=superseded, upstream_refs=prior['upstream_refs'],
                                   created_by=prior['created_by'], guard=guard)
    return {"decision_ref": {"artifact_id": artifact_id, "revision": 1}, "decision": payload}


def confirm_reference_decision(store, decision_ref: dict, *, project_id: str, canvas_id: str, requester_user_id: str, message_id: str | None = None, message_content: str | None = None, canvas: dict | None = None, workflow_state_dir=None, _canvas_guard=None, expected_commission_ref: dict | None = None) -> dict:
    from .store import RemixArtifactConflict
    decision = store.read_latest(decision_ref["artifact_id"])
    if decision["schema"] != "reference_decision.v1":
        raise ValueError("not a director decision")
    if decision['payload']['status'] == 'superseded':
        raise ValueError('decision was superseded by a newer direction')
    if decision['payload']['kind'] == 'material':
        if message_id is not None:
            raise ValueError('material confirmation requires an explicit browser candidate action')
        return _confirm_material_decision(store, decision_ref, project_id=project_id, canvas_id=canvas_id,
            requester_user_id=requester_user_id, canvas=canvas, workflow_state_dir=workflow_state_dir, canvas_guard=_canvas_guard, expected_commission_ref=expected_commission_ref)
    if decision_ref.get('revision') is not None and decision_ref['revision'] != decision['revision'] and decision['payload']['status'] != 'confirmed':
        raise ValueError('decision revision changed; review its current proposal')
    payload = decision["payload"]
    commission = read_reference_commission(store, payload["commission_ref"], latest=True)
    if expected_commission_ref is not None and (expected_commission_ref.get('artifact_id'), expected_commission_ref.get('revision'), expected_commission_ref.get('project_id', store.project_id)) != (commission['commission_ref']['artifact_id'], commission['commission_ref']['revision'], store.project_id):
        raise RemixArtifactConflict('commission changed; refresh the current card')
    if (project_id, canvas_id, requester_user_id) != (commission["project_id"], commission["canvas_id"], commission["requester_user_id"]):
        raise PermissionError("director decision actor or canvas differs")
    if payload["status"] == "confirmed":
        return {"commission": commission, "decision_ref": {"artifact_id": decision["id"], "revision": decision["revision"]}}
    receipt_id = "chat-message:" + message_id if message_id else "director-decision:" + decision["id"]
    previous = next((row for row in commission["decisions"] if row.get("message_id") == receipt_id), None)
    if previous is None:
        if commission["commission_ref"]["revision"] != payload["commission_ref"]["revision"]:
            raise RemixArtifactConflict("Decision is stale; review the new direction or plan")
        if payload["kind"] == "direction":
            event = {"type": "direction_confirmed", "direction": payload["proposal"]}
        elif payload["kind"] == "plan":
            event = {"type": "plan_confirmed", "plan_ref": payload["proposal"]["plan_ref"]}
        elif payload["kind"] == "composition":
            event = {"type": "editable_composition_confirmed", "manifest_ref": payload["proposal"]["manifest_ref"]}
        else:
            raise ValueError('unsupported director decision kind')
        commission = advance_reference_commission(store, commission["commission_ref"], event,
            commission["commission_ref"]["revision"], host_evidence=AuthenticatedDecisionEvidence(project_id, canvas_id, requester_user_id, receipt_id, message_content))
    saved = store.revise(decision["id"], decision["revision"], {**payload, "status": "confirmed", "receipt_id": receipt_id,
                         "decided_by": requester_user_id}, decision["upstream_refs"])
    return {"commission": commission, "decision_ref": {"artifact_id": saved["id"], "revision": saved["revision"]}}


class _MaterialPublicationGuard:
    def __init__(self, artifact_guard, canvas_guard):
        self.artifact_guard, self.canvas_guard = artifact_guard, canvas_guard

    def reassert(self):
        self.artifact_guard.reassert()
        if self.canvas_guard is not None:
            self.canvas_guard.reassert()


def _confirm_material_decision(store, decision_ref, *, project_id, canvas_id, requester_user_id,
                               canvas, workflow_state_dir, canvas_guard, expected_commission_ref=None):
    """Confirm only the exact selected review; publication never accepts or generates."""
    from .contracts import validate_payload
    from .material_work import _exact_ref
    from .material_review import validate_material_review_lineage
    from .material_execution import canvas_material_resource, confirm_material_review, validate_material_native_execution
    from .reference_commission import _result
    from .store import RemixArtifactConflict

    def plan_identity(ref):
        if not isinstance(ref, dict) or set(ref) not in ({'artifact_id', 'revision'}, {'artifact_id', 'revision', 'project_id'}):
            raise ValueError('material selection requires an exact owning plan reference')
        if ref.get('project_id', store.project_id) != store.project_id:
            raise ValueError('material selection plan project differs')
        return _exact_ref({key: ref[key] for key in ('artifact_id', 'revision')})

    with store._locked() as artifact_guard:
        guard = _MaterialPublicationGuard(artifact_guard, canvas_guard)
        decision = store._read(decision_ref['artifact_id'], store._latest_revision(decision_ref['artifact_id']))
        payload = decision['payload']
        if decision['schema'] != 'reference_decision.v1' or payload['kind'] != 'material':
            raise ValueError('single material decision required')
        if decision_ref.get('revision') is not None and decision_ref['revision'] != decision['revision'] and payload['status'] != 'confirmed':
            raise RemixArtifactConflict('material decision changed; review its current proposal')
        commission_ref = payload['commission_ref']
        commission = _result(store._read(commission_ref['artifact_id'], store._latest_revision(commission_ref['artifact_id'])))
        if expected_commission_ref is not None and (expected_commission_ref.get('artifact_id'), expected_commission_ref.get('revision'), expected_commission_ref.get('project_id', store.project_id)) != (commission['commission_ref']['artifact_id'], commission['commission_ref']['revision'], store.project_id):
            raise RemixArtifactConflict('commission changed; refresh the current card')
        if ((project_id, canvas_id, requester_user_id) != (commission['project_id'], commission['canvas_id'], commission['requester_user_id'])
                or requester_user_id != store.created_by or decision['created_by'] != requester_user_id):
            raise PermissionError('material decision actor or canvas differs')
        if commission['phase'] != 'preparing_materials' or commission['commission_ref']['revision'] != commission_ref['revision']:
            raise RemixArtifactConflict('material decision commission changed; review current work')
        if canvas is None:
            raise ValueError('material selection requires the authorized current canvas')
        proposal = payload['proposal']
        if not isinstance(proposal, dict) or not {'work_ref', 'plan_ref', 'candidate'} <= proposal.keys() or set(proposal) - {'work_ref', 'plan_ref', 'candidate', 'acceptance_note', 'next_action'}:
            raise ValueError('material selection requires one exact work/step/review/hash candidate; narrative or batch approval is unsupported')
        candidate = proposal['candidate']
        if not isinstance(candidate, dict) or not {'step_id', 'review_ref', 'media_hash'} <= candidate.keys() or set(candidate) - {'step_id', 'review_ref', 'media_hash', 'duration_us', 'has_audio', 'node_label'}:
            raise ValueError('material selection requires one exact work/step/review/hash candidate')
        work_ref, review_ref = _exact_ref(proposal['work_ref']), _exact_ref(candidate['review_ref'])
        plan_ref = plan_identity(proposal['plan_ref'])
        if plan_ref != plan_identity(commission.get('plan_ref')):
            raise ValueError('material selection plan differs from its commission')
        plan = store._read(plan_ref['artifact_id'], store._latest_revision(plan_ref['artifact_id']))
        if plan['schema'] != 'production_plan.v2' or plan['revision'] != plan_ref['revision']:
            raise RemixArtifactConflict('material selection plan changed')
        if not any(node.get('type') == 'scriptNode' and
                   (node.get('data', {}).get('productionPlanV2Ref') or {}).get('artifactId',
                   (node.get('data', {}).get('productionPlanV2Ref') or {}).get('artifact_id')) == plan['id']
                   for node in canvas.get('nodes', [])):
            raise ValueError('material selection plan is not on the current canvas')
        work = store._read(work_ref['artifact_id'], store._latest_revision(work_ref['artifact_id']))
        if work['schema'] != 'material_work_package.v1' or work['revision'] != work_ref['revision'] or plan_identity(work['payload']['plan_ref']) != plan_ref or work['created_by'] != requester_user_id:
            raise RemixArtifactConflict('material selection work changed or belongs to another plan/actor')
        original = store._read(review_ref['artifact_id'], review_ref['revision'])
        current_review = store._read(review_ref['artifact_id'], store._latest_revision(review_ref['artifact_id']))
        review = original['payload']
        if original['schema'] != 'material_review.v1' or original['created_by'] != requester_user_id:
            raise ValueError('material selection requires its actual saved review')
        validate_material_review_lineage(store, review, original['upstream_refs'])
        step = next((row for row in work['payload']['steps'] if row['id'] == candidate['step_id']), None)
        resource = review['resource']
        if (not step or review['work_ref'] != work_ref or review['step_id'] != step['id']
                or resource['media_kind'] != step['media_kind'] or candidate['media_hash'] != resource['media_hash']
                or (step['source'] == 'existing' and step.get('node_id') != resource['node_id'])):
            raise ValueError('material selection step, review or media hash differs')
        for key, value_type in (('duration_us', int), ('has_audio', bool)):
            if key in candidate and (type(candidate[key]) is not value_type or candidate[key] != resource.get(key)):
                raise ValueError('material selection candidate metadata contradicts its saved resource')
        if step['source'] == 'generate':
            validate_material_native_execution(store, canvas, step, resource['node_id'], require_complete=True, workflow_state_dir=workflow_state_dir)
        if canvas_material_resource(store, canvas, resource['node_id'], resource['media_kind']) != resource:
            raise ValueError('material changed after review')
        if current_review['revision'] != original['revision']:
            # Recover only the single genuine user-confirmation publication that
            # could have preceded a failed decision publication, never a new review.
            expected = {**review, 'actor_kind': 'user', 'content': {'status': 'passed',
                'checks': ['authenticated_user_viewed_current_material'], 'findings': []},
                'user_decision': {'project_id': project_id, 'canvas_id': canvas_id,
                    'requester_user_id': requester_user_id, 'media_hash': resource['media_hash']}}
            expected['decision_receipt_id'] = hashlib.sha256(store._canonical(expected['user_decision'])).hexdigest()
            expected = validate_payload('material_review.v1', expected)
            if (current_review['revision'] != original['revision'] + 1 or current_review['payload'] != expected
                    or current_review['created_by'] != original['created_by'] or current_review['upstream_refs'] != original['upstream_refs']):
                raise RemixArtifactConflict('material review changed; select and inspect its current exact revision')
        if payload['status'] == 'confirmed':
            if payload.get('decided_by') != requester_user_id or payload.get('receipt_id') != 'director-decision:' + decision['id']:
                raise PermissionError('material decision receipt actor differs')
            proof = {'artifact_id': current_review['id'], 'revision': current_review['revision'], 'project_id': store.project_id}
            if proof not in decision['upstream_refs'] or current_review['payload'].get('user_decision') != {
                'project_id': project_id, 'canvas_id': canvas_id, 'requester_user_id': requester_user_id, 'media_hash': resource['media_hash']}:
                raise ValueError('material decision requires its exact persisted user review')
            confirmed = {'review_ref': {'artifact_id': current_review['id'], 'revision': current_review['revision']}, 'review': current_review['payload']}
        else:
            confirmed = confirm_material_review(store, {'artifact_id': current_review['id'], 'revision': current_review['revision']},
                canvas, canvas_id=canvas_id, requester_user_id=requester_user_id, _guard=guard)
            if confirmed['review']['technical']['status'] != 'passed' or confirmed['review']['content']['status'] != 'passed':
                raise ValueError('material selection requires a passed persisted user review')
            refs = store._reference_list([*decision['upstream_refs'], work_ref, confirmed['review_ref']], store.project_id)
            updated = validate_payload('reference_decision.v1', {**payload, 'status': 'confirmed',
                'receipt_id': 'director-decision:' + decision['id'], 'decided_by': requester_user_id})
            store._validate_upstream_refs(refs)
            decision = store._publish(artifact_id=decision['id'], schema=decision['schema'], revision=decision['revision'] + 1,
                payload=updated, upstream_refs=refs, created_by=decision['created_by'], guard=guard)
        if confirmed['review']['technical']['status'] != 'passed' or confirmed['review']['content']['status'] != 'passed':
            raise ValueError('material selection requires a passed persisted user review')
        return {'commission': commission, 'decision_ref': {'artifact_id': decision['id'], 'revision': decision['revision']},
                'review_ref': confirmed['review_ref'], 'resource': confirmed['review']['resource'],
                'material_decision_receipt': decision['payload']['receipt_id']}


def list_director_artifacts(store, canvas_id: str, requester_user_id: str, *, limit: int | None = 100) -> dict:
    commissions, decisions, works, speech, material_reviews, outputs = [], [], [], [], [], []
    # Read only this authorized project's artifact root. Headers are small; do
    # not load video/media or every historical payload to render the panel.
    if not store.root.exists():
        return {"commissions": [], "decisions": [], "material_work": [], "speech_corrections": [], "material_reviews": [], "output_reviews": []}
    with store._locked():
        for directory in store.root.iterdir():
            if directory.name.startswith("_") or directory.is_symlink() or not directory.is_dir():
                continue
            try:
                revision = store._latest_revision(directory.name)
                header = store._read_json(store._path(directory.name, revision, ".json"))
                if header.get("schema") not in {"reference_commission.v1", "reference_decision.v1", "material_work_package.v1", "speech_correction_preview.v1", "material_review.v1", "production_output_review.v1"}:
                    continue
                artifact = store._read(directory.name, revision)
            except (ValueError, FileNotFoundError):
                continue
            if artifact["schema"] == "reference_commission.v1":
                value = artifact["payload"]
                if value["canvas_id"] == canvas_id and value["requester_user_id"] == requester_user_id:
                    commissions.append({**value, "commission_ref": {"artifact_id": artifact["id"], "revision": artifact["revision"], "project_id": store.project_id}})
            elif artifact["schema"] == "reference_decision.v1":
                decisions.append({**artifact["payload"], "decision_ref": {"artifact_id": artifact["id"], "revision": artifact["revision"]}})
            elif artifact["schema"] == "material_work_package.v1":
                works.append({**artifact["payload"], "work_ref": {"artifact_id": artifact["id"], "revision": artifact["revision"]}})
            elif artifact["schema"] == "speech_correction_preview.v1":
                if store._latest_revision(artifact["payload"]["source_plan_ref"]["artifact_id"]) == artifact["payload"]["source_plan_ref"]["revision"]:
                    speech.append({**artifact["payload"], "preview_ref": {"artifact_id": artifact["id"], "revision": artifact["revision"]}})
            elif artifact["schema"] == "material_review.v1" and artifact["payload"]["content"]["status"] == "unavailable":
                material_reviews.append({**artifact["payload"], "review_ref": {"artifact_id": artifact["id"], "revision": artifact["revision"]}})
            elif artifact["schema"] == "production_output_review.v1" and artifact["payload"]["status"] == "needs_review":
                outputs.append({**artifact["payload"], "review_ref": {"artifact_id": artifact["id"], "revision": artifact["revision"]}})
    allowed = {c["commission_ref"]["artifact_id"] for c in commissions}
    current_commissions = {c['commission_ref']['artifact_id']: c for c in commissions}
    plan_ids = {c["plan_ref"]["artifact_id"] for c in commissions if c.get("plan_ref")}
    filtered_work = [w for w in works if w["plan_ref"]["artifact_id"] in plan_ids][-20:]
    pending = []
    phases = {'direction': 'awaiting_direction', 'plan': 'awaiting_plan_confirmation', 'composition': 'reviewing_output', 'material': 'preparing_materials'}
    for decision in decisions:
        current = current_commissions.get(decision['commission_ref']['artifact_id'])
        if not current or decision['status'] != 'pending' or decision['commission_ref']['revision'] != current['commission_ref']['revision'] or current['phase'] != phases[decision['kind']]:
            continue
        if decision['kind'] == 'plan' and decision['proposal'].get('plan_ref') != current.get('plan_ref'):
            continue
        pending.append(decision)
    return {"commissions": commissions[-limit:] if limit is not None else commissions,
            "decisions": pending[-limit:] if limit is not None else pending,
            "material_work": filtered_work,
            "speech_corrections": [p for p in speech if p["source_plan_ref"]["artifact_id"] in plan_ids][-20:],
            "material_reviews": [r for r in material_reviews if r["work_ref"]["artifact_id"] in {w["work_ref"]["artifact_id"] for w in filtered_work}][-20:],
            "output_reviews": [r for r in outputs if r["commission_ref"]["artifact_id"] in allowed][-20:]}


def confirm_reference_card(store, ctx, *, canvas_id, decision_ref, commission_ref, agent_id, expected_projection_revision, runs):
    """Recheck authorized projection and publish the user decision under one lease.

    Lock order is canvas then artifacts. Existing unlocked/mutation adapters
    reuse that composite publication guard, including every actual write.
    """
    from novelvideo.freezone import canvas_store
    from .conversation_cards import _authorize
    from .production_advancement import _MutationStore
    from .production_discovery_index import read_with_production_index
    from .production_inventory import read_current_production
    from .store import RemixArtifactConflict
    with canvas_store.get_canvas_write_mutex().write_mutex(ctx.state_dir, canvas_id) as canvas_guard:
        canvas = canvas_store.read_canvas(ctx.state_dir, canvas_id)
        if not canvas:
            raise RemixArtifactConflict('canvas no longer exists')
        with store._locked() as artifact_guard:
            guard = _MaterialPublicationGuard(artifact_guard, canvas_guard)
            locked = _MutationStore(store, guard)
            commission = _authorize(locked, canvas, canvas_id, ctx.requester_user_id, agent_id, runs, commission_ref['artifact_id'])
            if commission['commission_ref'] != {**commission_ref, 'project_id': ctx.project_id}:
                raise RemixArtifactConflict('作品方案版本已变化，请刷新卡片')
            decision = locked.read_latest(decision_ref['artifact_id'])
            if (decision['schema'] != 'reference_decision.v1' or decision_ref.get('revision') != decision['revision']
                    or decision_ref.get('project_id', ctx.project_id) != ctx.project_id
                    or decision['payload']['commission_ref']['artifact_id'] != commission_ref['artifact_id']):
                raise RemixArtifactConflict('制作选择版本已变化，请刷新卡片')
            projection = read_with_production_index(ctx, locked, canvas_id=canvas_id, agent_id=agent_id,
                reader=read_current_production, _defer_rebuild=True, canvas=canvas, requester_user_id=ctx.requester_user_id,
                state_dir=ctx.state_dir, runs=runs, commission_id=commission_ref['artifact_id'])
            if projection['projection_revision'] != expected_projection_revision:
                raise RemixArtifactConflict('制作状态已变化，请刷新卡片')
            guard.reassert()
            result = confirm_reference_decision(locked, decision_ref, project_id=ctx.project_id, canvas_id=canvas_id,
                requester_user_id=ctx.requester_user_id, canvas=canvas, workflow_state_dir=ctx.state_dir,
                _canvas_guard=canvas_guard, expected_commission_ref=commission_ref)
            guard.reassert()
            return result
