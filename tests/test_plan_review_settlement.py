"""A passed review must settle one current conversational confirmation target."""
# ruff: noqa: F811
from __future__ import annotations

import pytest

from test_plan_confirmation_contract import reviewed_plan  # noqa: F401
from test_reference_director_api import setup_api  # noqa: F401
from novelvideo.freezone.remix.plan_review_settlement import (
    ensure_reviewed_plan_confirmation,
    submit_plan_review,
    verified_plan_review_settlement,
)


def _reviews(store):
    with store._locked():
        return [store._read(path.name, store._latest_revision(path.name))
                for path in store.root.iterdir()
                if path.is_dir() and not path.name.startswith('_')
                and store._read(path.name, store._latest_revision(path.name))['schema'] == 'production_review.v1']


def _unreviewed_plan(setup_api):
    from test_reference_director_api import confirm_direction
    from novelvideo.freezone.remix.reference_commission import advance_reference_commission
    from novelvideo.freezone.remix.production_review import REVIEW_CATEGORIES

    _, store, commission, _ = setup_api
    commission = confirm_direction(setup_api[0], commission)
    saved = store.create('production_plan.v2', {
        'schema_version': 2, 'goal': '产品介绍',
        'output': {'width': 1280, 'height': 720, 'fps_num': 24, 'fps_den': 1},
        'beats': [{'id': 'intro', 'purpose': '产品开场', 'duration_frames': 72}],
    }, [])
    plan_ref = {'artifact_id': saved['id'], 'revision': saved['revision'], 'project_id': 'p'}
    commission = advance_reference_commission(store, commission['commission_ref'],
        {'type': 'plan_ready', 'plan_ref': plan_ref}, commission['commission_ref']['revision'])
    findings = [{'category': category, 'entity_kind': 'plan', 'entity_id': 'plan',
                 'verdict': 'pass', 'reason': '核对已确认的产品开场方案',
                 'evidence': ['方案包含三秒产品开场，横屏 24fps']}
                for category in REVIEW_CATEGORIES]
    return store, commission, plan_ref, findings


def test_repeated_review_returns_same_saved_review_and_decision(reviewed_plan):
    _, store, commission, _ = reviewed_plan
    review = store.read(commission['review_ref']['artifact_id'], commission['review_ref']['revision'])
    result = submit_plan_review(store, commission_ref=commission['commission_ref'],
                                plan_ref=commission['plan_ref'], findings=review['payload']['findings'])
    recovered = ensure_reviewed_plan_confirmation(store, commission_ref=commission['commission_ref'])
    assert result['review_ref'] == commission['review_ref'] == recovered['review_ref']
    assert result['decision_ref'] == recovered['decision_ref']
    assert len(_reviews(store)) == 1


def test_retry_with_short_plan_ref_reuses_saved_review(reviewed_plan):
    _, store, commission, _ = reviewed_plan
    review = store.read(commission['review_ref']['artifact_id'], commission['review_ref']['revision'])
    short_ref = {key: value for key, value in commission['plan_ref'].items()
                 if key != 'project_id'}
    result = submit_plan_review(store, commission_ref=commission['commission_ref'],
                                plan_ref=short_ref, findings=review['payload']['findings'])
    assert result['review_ref'] == commission['review_ref']
    assert len(_reviews(store)) == 1


def test_invalid_evidence_does_not_advance_unreviewed_commission(setup_api):
    from test_reference_director_api import confirm_direction
    from novelvideo.freezone.remix.production_review import REVIEW_CATEGORIES

    client, store, commission, _ = setup_api
    commission = confirm_direction(client, commission)
    saved = store.create('production_plan.v2', {
        'schema_version': 2, 'goal': '产品介绍',
        'output': {'width': 1280, 'height': 720, 'fps_num': 24, 'fps_den': 1},
        'beats': [{'id': 'intro', 'purpose': '产品开场', 'duration_frames': 72}],
    }, [])
    plan_ref = {'artifact_id': saved['id'], 'revision': saved['revision'], 'project_id': 'p'}
    findings = [{'category': category, 'entity_kind': 'plan', 'entity_id': 'plan',
                 'verdict': 'pass', 'reason': 'Checked plan', 'evidence': ['Current plan']}
                for category in REVIEW_CATEGORIES]
    findings[0]['evidence_refs'] = [{
        'entity_kind': 'beat', 'entity_id': 'missing', 'field': 'purpose',
    }]

    with pytest.raises(ValueError, match='unknown entity'):
        submit_plan_review(store, commission_ref=commission['commission_ref'],
                           plan_ref=plan_ref, findings=findings)

    current = store.read_latest(commission['commission_ref']['artifact_id'])
    assert current['revision'] == commission['commission_ref']['revision']
    assert current['payload']['phase'] == 'drafting_plan'


def test_confirmed_preservation_list_reviews_existing_saved_plan_without_regeneration(tmp_path):
    from novelvideo.freezone.remix.direction_contract import resolve_direction_review_contract
    from novelvideo.freezone.remix.production_review import REVIEW_CATEGORIES
    from novelvideo.freezone.remix.reference_commission import (
        AuthenticatedDecisionEvidence, advance_reference_commission, create_reference_commission,
    )
    from novelvideo.freezone.remix.store import RemixArtifactStore

    store = RemixArtifactStore(tmp_path, 'p', created_by='u')
    commission = create_reference_commission(
        store, canvas_id='c', requester_user_id='u', analysis_node_id='analysis',
        goal='Remix reference', analysis_evidence={'task_id': 'task', 'result_fingerprint': 'hash'},
    )
    direction = {'goal': 'Keep the opening question',
                 'preserve': ['Keep the opening question']}
    commission = advance_reference_commission(
        store, commission['commission_ref'], {'type': 'direction_confirmed', 'direction': direction}, 1,
        host_evidence=AuthenticatedDecisionEvidence('p', 'c', 'u', 'message-1'),
    )
    saved = store.create('production_plan.v2', {
        'schema_version': 2, 'goal': 'Keep the opening question and introduce a new product',
        'output': {'width': 1080, 'height': 1920, 'fps_num': 24, 'fps_den': 1},
        'beats': [{'id': 'opening', 'purpose': 'Opening question introduces a new product',
                   'duration_frames': 72}],
    }, [])
    plan_ref = {'artifact_id': saved['id'], 'revision': saved['revision']}
    refs = [
        {'entity_kind': 'plan', 'entity_id': 'plan', 'field': 'goal'},
        {'entity_kind': 'beat', 'entity_id': 'opening', 'field': 'purpose'},
    ]
    findings = [
        {'category': category, 'entity_kind': 'plan', 'entity_id': 'plan',
         'verdict': 'pass', 'reason': 'Inspected the saved opening and expanded product goal',
         'evidence': ['The saved opening retains a question'], 'evidence_refs': refs}
        for category in REVIEW_CATEGORIES
    ]
    requirement = resolve_direction_review_contract(direction)['requirements']['semantic'][0]
    findings.append({
        'category': 'direction', 'entity_kind': 'beat', 'entity_id': 'opening',
        'requirement_id': requirement['id'], 'verdict': 'pass',
        'reason': 'The opening beat starts with a question',
        'evidence': ['A question introduces the product'], 'evidence_refs': refs[1:],
    })

    result = submit_plan_review(store, commission_ref=commission['commission_ref'],
                                plan_ref=plan_ref, findings=findings)
    assert result['review']['status'] == 'passed'
    assert result['commission']['phase'] == 'awaiting_plan_confirmation'
    assert result['decision']['status'] == 'pending'
    assert store.read_latest(saved['id'])['revision'] == saved['revision']


def test_changed_findings_cannot_reuse_prior_passed_review(reviewed_plan):
    _, store, commission, _ = reviewed_plan
    review = store.read(commission['review_ref']['artifact_id'], commission['review_ref']['revision'])
    findings = [{**row, 'verdict': 'change'} if row['category'] == 'direction' else row
                for row in review['payload']['findings']]
    with pytest.raises(ValueError, match='review request differs'):
        submit_plan_review(store, commission_ref=commission['commission_ref'],
                           plan_ref=commission['plan_ref'], findings=findings)


def test_recovery_evidence_must_match_the_actual_saved_review_and_pending_decision(reviewed_plan):
    _, store, commission, _ = reviewed_plan
    settled = ensure_reviewed_plan_confirmation(store, commission_ref=commission['commission_ref'])
    receipt = {'review_ref': settled['review_ref'], 'decision_ref': settled['decision_ref'],
               'commission': settled['commission']}
    verified = verified_plan_review_settlement(store, receipt)
    assert verified == {
        'verified': True, 'project_id': store.project_id,
        'canvas_id': commission['canvas_id'],
        'requester_user_id': commission['requester_user_id'],
        'commission_id': commission['commission_ref']['artifact_id'],
        'plan_ref': commission['plan_ref'],
        'direction_revision': commission['direction_revision'],
        'review_status': 'passed', 'decision_status': 'pending',
    }
    assert verified_plan_review_settlement(store, {
        **receipt, 'decision_ref': {'artifact_id': 'other', 'revision': 1},
    }) is None


def test_retry_after_review_save_reuses_same_review(setup_api, monkeypatch):
    from novelvideo.freezone.remix import plan_review_settlement as module

    store, commission, plan_ref, findings = _unreviewed_plan(setup_api)
    original = module.advance_reference_commission
    interrupted = False

    def fail_once(store_arg, commission_arg, event, revision):
        nonlocal interrupted
        if event['type'] == 'plan_reviewed' and not interrupted:
            interrupted = True
            raise RuntimeError('interrupted after review publication')
        return original(store_arg, commission_arg, event, revision)

    monkeypatch.setattr(module, 'advance_reference_commission', fail_once)
    with pytest.raises(RuntimeError, match='interrupted'):
        submit_plan_review(store, commission_ref=commission['commission_ref'],
                           plan_ref=plan_ref, findings=findings)
    assert len(_reviews(store)) == 1
    result = submit_plan_review(store, commission_ref=commission['commission_ref'],
                                plan_ref=plan_ref, findings=findings)
    assert result['commission']['phase'] == 'awaiting_plan_confirmation'
    assert result['decision']['status'] == 'pending'
    assert len(_reviews(store)) == 1


def test_retry_after_phase_save_finishes_internal_decision(setup_api, monkeypatch):
    from novelvideo.freezone.remix import reference_director as director

    store, commission, plan_ref, findings = _unreviewed_plan(setup_api)
    original = director.prepare_reference_decision
    interrupted = False

    def fail_once(*args, **kwargs):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise RuntimeError('interrupted before decision publication')
        return original(*args, **kwargs)

    monkeypatch.setattr(director, 'prepare_reference_decision', fail_once)
    with pytest.raises(RuntimeError, match='interrupted'):
        submit_plan_review(store, commission_ref=commission['commission_ref'],
                           plan_ref=plan_ref, findings=findings)
    current = store.read_latest(commission['commission_ref']['artifact_id'])
    assert current['payload']['phase'] == 'awaiting_plan_confirmation'
    result = submit_plan_review(store, commission_ref=commission['commission_ref'],
                                plan_ref=plan_ref, findings=findings)
    assert result['decision']['status'] == 'pending'
    assert result['commission']['review_ref'] == result['review_ref']
    assert len(_reviews(store)) == 1


def test_native_plan_review_transition_registers_the_same_internal_decision(setup_api):
    from novelvideo.freezone.remix.production_review import review_production_plan
    from novelvideo.freezone.remix.reference_commission import advance_reference_commission
    from novelvideo.freezone.remix.reference_director import list_director_artifacts

    store, commission, plan_ref, findings = _unreviewed_plan(setup_api)
    review = review_production_plan(store, plan_ref, commission['commission_ref'], findings)
    review_ref = {'artifact_id': review['id'], 'revision': review['revision'], 'project_id': 'p'}
    current = advance_reference_commission(store, commission['commission_ref'],
        {'type': 'plan_reviewed', 'review_ref': review_ref},
        commission['commission_ref']['revision'])
    decisions = [row for row in list_director_artifacts(store, 'c', 'u', limit=None)['decisions']
                 if row['kind'] == 'plan' and row['status'] == 'pending']
    assert len(decisions) == 1
    assert decisions[0]['proposal']['plan_ref'] == current['plan_ref']


def test_review_of_new_saved_revision_creates_new_review_and_decision(reviewed_plan):
    from test_plan_revision_settlement import _edit

    _, store, commission, _ = reviewed_plan
    old = ensure_reviewed_plan_confirmation(store, commission_ref=commission['commission_ref'])
    new_ref = _edit(store, commission['plan_ref'])
    findings = [{**row, 'reason': '核对新开场方案', 'evidence': ['新方案先展示产品细节']}
                for row in old['review']['findings']]
    result = submit_plan_review(store, commission_ref=commission['commission_ref'],
                               plan_ref=new_ref, findings=findings)
    assert result['commission']['phase'] == 'awaiting_plan_confirmation'
    assert result['commission']['plan_ref'] == new_ref
    assert result['review_ref'] != old['review_ref']
    assert result['decision_ref'] != old['decision_ref']
    assert result['decision']['proposal']['plan_ref'] == new_ref
    assert result['review']['findings'] == findings
    assert len(_reviews(store)) == 2
    again = submit_plan_review(store, commission_ref=result['commission']['commission_ref'],
                               plan_ref=new_ref, findings=findings)
    assert again == result
    assert len(_reviews(store)) == 2


def test_review_entry_rejects_a_different_plan_artifact(reviewed_plan):
    _, store, commission, _ = reviewed_plan
    saved = store.create('production_plan.v2',
                         store.read_latest(commission['plan_ref']['artifact_id'])['payload'], [])
    ref = {'artifact_id': saved['id'], 'revision': saved['revision'], 'project_id': 'p'}
    review = store.read(commission['review_ref']['artifact_id'], commission['review_ref']['revision'])
    with pytest.raises(ValueError):
        submit_plan_review(store, commission_ref=commission['commission_ref'],
                           plan_ref=ref, findings=review['payload']['findings'])
    assert len(_reviews(store)) == 1
