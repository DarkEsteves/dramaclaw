"""Saved revisions must settle only into their existing scoped commission."""
# ruff: noqa: F811
from __future__ import annotations

from copy import deepcopy

import pytest

from test_plan_confirmation_contract import reviewed_plan, _snapshot  # noqa: F401
from test_reference_director_api import setup_api  # noqa: F401
from novelvideo.freezone.remix.production_commands import apply_production_command
from novelvideo.freezone.remix.reference_commission import (
    AuthenticatedDecisionEvidence,
    advance_reference_commission,
)
from novelvideo.freezone.remix.store import RemixArtifactConflict, RemixArtifactStore


def _edit(store, plan_ref, *, payload=None, command_id='revise-reviewed-plan'):
    edited = apply_production_command(store, {
        'command_id': command_id,
        'plan_ref': {key: plan_ref[key] for key in ('artifact_id', 'revision')},
        'kind': 'edit_beat', 'target_id': 'intro',
        'payload': payload or {'purpose': '改为先展示产品细节'},
    })
    return {**edited['plan_ref'], 'project_id': store.project_id}


def test_new_saved_revision_reenters_review(reviewed_plan):
    from novelvideo.freezone.remix.plan_revision_settlement import reconcile_plan_revision

    _, store, commission, _ = reviewed_plan
    old_ref = commission['plan_ref']
    new_ref = _edit(store, old_ref)
    result = reconcile_plan_revision(store,
        commission_ref=commission['commission_ref'], plan_ref=new_ref)
    assert result['changed'] is True
    assert result['commission']['plan_ref'] == new_ref
    assert result['commission']['phase'] == 'awaiting_plan_review'
    assert result['commission']['review_ref'] is None
    assert result['commission']['direction'] == commission['direction']
    assert result['commission']['decisions'] == commission['decisions']
    assert store.read(old_ref['artifact_id'], old_ref['revision'])['payload']['beats'][0]['purpose'] == '产品介绍'
    again = reconcile_plan_revision(store,
        commission_ref=result['commission']['commission_ref'], plan_ref=new_ref)
    assert again == {'changed': False, 'commission': result['commission']}


@pytest.mark.parametrize('change', ['stale_commission', 'stale_plan', 'different_plan',
                                   'plan_project', 'commission_project', 'actor', 'plan_actor'])
def test_invalid_revision_scope_never_changes_commission(reviewed_plan, change):
    from novelvideo.freezone.remix.plan_revision_settlement import reconcile_plan_revision

    _, store, commission, _ = reviewed_plan
    ref = deepcopy(commission['commission_ref'])
    plan_ref = _edit(store, commission['plan_ref'])
    error = ValueError
    if change == 'stale_commission':
        ref['revision'] -= 1
        error = RemixArtifactConflict
    elif change == 'stale_plan':
        plan_ref = commission['plan_ref']
        error = RemixArtifactConflict
    elif change in {'different_plan', 'plan_actor'}:
        creator = store if change == 'different_plan' else RemixArtifactStore(store.project_dir, 'p', created_by='other')
        saved = creator.create('production_plan.v2', store.read_latest(plan_ref['artifact_id'])['payload'], [])
        plan_ref = {'artifact_id': saved['id'], 'revision': saved['revision'], 'project_id': 'p'}
    elif change == 'plan_project':
        plan_ref['project_id'] = 'other'
    elif change == 'commission_project':
        ref['project_id'] = 'other'
    else:
        store = RemixArtifactStore(store.project_dir, 'p', created_by='other')
        error = PermissionError
    before = _snapshot(store.project_dir)
    with pytest.raises(error):
        reconcile_plan_revision(store, commission_ref=ref, plan_ref=plan_ref)
    assert _snapshot(store.project_dir) == before


def test_unbound_plan_requires_existing_initial_binding_path(setup_api):
    from novelvideo.freezone.remix.plan_revision_settlement import reconcile_plan_revision
    from test_reference_director_api import confirm_direction

    client, store, commission, _ = setup_api
    plan = store.create('production_plan.v2', {
        'schema_version': 2, 'goal': '产品介绍',
        'output': {'width': 1280, 'height': 720, 'fps_num': 24, 'fps_den': 1},
        'beats': [{'id': 'intro', 'purpose': '开场', 'duration_frames': 72}],
    }, [])
    ref = {'artifact_id': plan['id'], 'revision': 1, 'project_id': 'p'}
    with pytest.raises(ValueError, match='direction'):
        reconcile_plan_revision(store, commission_ref=commission['commission_ref'], plan_ref=ref)
    commission = confirm_direction(client, commission)
    before = _snapshot(store.project_dir)
    with pytest.raises(ValueError, match='bound'):
        reconcile_plan_revision(store, commission_ref=commission['commission_ref'], plan_ref=ref)
    assert _snapshot(store.project_dir) == before


def test_same_ref_in_confirmation_is_read_only(reviewed_plan):
    from novelvideo.freezone.remix.plan_revision_settlement import reconcile_plan_revision

    _, store, commission, _ = reviewed_plan
    before = _snapshot(store.project_dir)
    assert reconcile_plan_revision(store, commission_ref=commission['commission_ref'],
                                   plan_ref=commission['plan_ref']) == {
        'changed': False, 'commission': commission,
    }
    assert _snapshot(store.project_dir) == before


def test_host_revised_direction_reuses_same_saved_branch_plan(reviewed_plan):
    from novelvideo.freezone.remix.plan_revision_settlement import reconcile_plan_revision
    from novelvideo.freezone.remix.reference_branches import prepare_reference_branches

    _, store, parent, _ = reviewed_plan
    branch = prepare_reference_branches(store, parent['commission_ref'],
                                       [parent['plan_ref']], 'direction-branch')['branches'][0]
    evidence = AuthenticatedDecisionEvidence('p', 'c', 'u', 'host-revised-direction')
    branch = advance_reference_commission(store, branch['commission_ref'], {
        'type': 'direction_confirmed',
        'direction': {**branch['direction'], 'opening': '增加细节'},
    }, branch['commission_ref']['revision'], host_evidence=evidence)
    assert branch['phase'] == 'drafting_plan'
    assert branch['plan_ref'] == parent['plan_ref']
    result = reconcile_plan_revision(store, commission_ref=branch['commission_ref'],
                                     plan_ref=branch['plan_ref'])
    assert result['changed'] is True
    assert result['commission']['phase'] == 'awaiting_plan_review'
    assert result['commission']['plan_ref'] == parent['plan_ref']
    assert result['commission']['direction'] == branch['direction']
    assert result['commission']['review_ref'] is None


def _confirm(store, commission):
    return advance_reference_commission(store, commission['commission_ref'], {
        'type': 'plan_confirmed', 'plan_ref': commission['plan_ref'],
    }, commission['commission_ref']['revision'],
        host_evidence=AuthenticatedDecisionEvidence('p', 'c', 'u', 'host-plan-confirmation'))


def _reviewed_48_frame_plan(tmp_path, requirements, *, scope='可编辑合成', split_shots=False,
                            include_primary=True):
    from novelvideo.freezone.remix.production_review import REVIEW_CATEGORIES, review_production_plan
    from novelvideo.freezone.remix.reference_commission import create_reference_commission

    store = RemixArtifactStore(tmp_path, 'p', created_by='u')
    commission = create_reference_commission(store, canvas_id='c', requester_user_id='u',
        analysis_node_id='analysis', goal='产品介绍', production_scope=scope,
        analysis_evidence={'task_id': 'analysis-task', 'result_fingerprint': 'analysis-hash'})
    direction = {'target_goal': '产品介绍', 'requirements': requirements}
    commission = advance_reference_commission(store, commission['commission_ref'],
        {'type': 'direction_confirmed', 'direction': direction}, 1,
        host_evidence=AuthenticatedDecisionEvidence('p', 'c', 'u', 'confirmed-direction'))
    placements = [{'id': 'primary', 'kind': 'visual', 'role': 'primary', 'track_id': 'main',
        'start': {'kind': 'beat', 'ref_id': 'intro', 'edge': 'start'},
        'end': {'kind': 'beat', 'ref_id': 'intro', 'edge': 'end'}}]
    if split_shots:
        placements = [{**placements[0], 'id': f'primary-{index}',
            'start': {'kind': 'program_frame', 'frame': index * 24},
            'end': {'kind': 'program_frame', 'frame': (index + 1) * 24}}
            for index in range(2)]
    if not include_primary:
        placements = []
    plan = store.create('production_plan.v2', {
        'schema_version': 2, 'goal': '产品介绍',
        'output': {'width': 1280, 'height': 720, 'fps_num': 24, 'fps_den': 1},
        'beats': [{'id': 'intro', 'purpose': '产品介绍', 'duration_frames': 48, 'turns': []}],
        'placements': placements,
    }, [])
    plan_ref = {'artifact_id': plan['id'], 'revision': 1, 'project_id': 'p'}
    commission = advance_reference_commission(store, commission['commission_ref'],
        {'type': 'plan_ready', 'plan_ref': plan_ref}, 2)
    findings = [{'category': category, 'entity_kind': 'plan', 'entity_id': 'plan',
        'verdict': 'pass', 'reason': 'Actual saved plan meets confirmed direction',
        'evidence': ['intro:产品介绍']} for category in REVIEW_CATEGORIES]
    review = review_production_plan(store, plan_ref, commission['commission_ref'], findings)
    assert review['payload']['status'] == 'passed'
    review_ref = {'artifact_id': review['id'], 'revision': 1, 'project_id': 'p'}
    commission = advance_reference_commission(store, commission['commission_ref'],
        {'type': 'plan_reviewed', 'review_ref': review_ref}, 3)
    commission = _confirm(store, commission)
    return store, commission


@pytest.mark.parametrize(('requirements', 'expected_phase'), [
    ({'duration_us': {'max': 2_000_000}}, 'awaiting_plan_review'),
    ({'duration_us': {'max': 10_000_000}}, 'preparing_materials'),
    ({}, 'preparing_materials'),
])
def test_timing_revision_rechecks_explicit_bounds_before_retaining_approval(tmp_path, requirements, expected_phase):
    from novelvideo.freezone.remix.plan_revision_settlement import reconcile_plan_revision
    from novelvideo.freezone.remix.production_requirements import check_production_requirements

    store, confirmed = _reviewed_48_frame_plan(tmp_path, requirements)
    old_review = confirmed['review_ref']
    new_ref = _edit(store, confirmed['plan_ref'], payload={'duration_frames': 240})
    checks = check_production_requirements(store.read_latest(new_ref['artifact_id'])['payload'],
        requirements, project_dir=store.project_dir)
    assert ('duration_mismatch' in {row['code'] for row in checks if row['status'] == 'failed'}) == (expected_phase == 'awaiting_plan_review')
    received = reconcile_plan_revision(store, commission_ref=confirmed['commission_ref'], plan_ref=new_ref)['commission']
    assert received['phase'] == expected_phase
    assert (received['review_ref'] is None) == (expected_phase == 'awaiting_plan_review')
    if expected_phase == 'preparing_materials':
        assert received['review_ref'] == old_review


def test_timing_revision_violating_bound_reopens_plan_only_scope(tmp_path):
    from novelvideo.freezone.remix.plan_revision_settlement import reconcile_plan_revision

    store, confirmed = _reviewed_48_frame_plan(tmp_path, {'duration_us': {'max': 2_000_000}},
        scope='仅作品方案')
    assert confirmed['phase'] == 'completed'
    new_ref = _edit(store, confirmed['plan_ref'], payload={'duration_frames': 240})
    received = reconcile_plan_revision(store, commission_ref=confirmed['commission_ref'], plan_ref=new_ref)['commission']
    assert received['phase'] == 'awaiting_plan_review'
    assert received['plan_ref'] == new_ref
    assert received['review_ref'] is None


def test_shorter_beat_invalidates_independent_primary_shot_requirement(tmp_path):
    from novelvideo.freezone.remix.plan_revision_settlement import reconcile_plan_revision
    from novelvideo.freezone.remix.production_requirements import check_production_requirements

    requirements = {'primary_shots': {'min': 2}}
    store, confirmed = _reviewed_48_frame_plan(tmp_path, requirements, split_shots=True)
    new_ref = _edit(store, confirmed['plan_ref'], payload={'duration_frames': 24})
    checks = check_production_requirements(store.read_latest(new_ref['artifact_id'])['payload'],
        requirements, project_dir=store.project_dir)
    assert any(row['status'] == 'failed' and row['code'].startswith('primary_shot_') for row in checks)
    received = reconcile_plan_revision(store, commission_ref=confirmed['commission_ref'], plan_ref=new_ref)['commission']
    assert received['phase'] == 'awaiting_plan_review'
    assert received['review_ref'] is None
    assert received['plan_ref'] == new_ref


def test_within_bound_timing_refinement_keeps_composing_phase_and_review(tmp_path):
    from novelvideo.freezone.remix.plan_revision_settlement import reconcile_plan_revision

    store, confirmed = _reviewed_48_frame_plan(tmp_path, {'duration_us': {'max': 4_000_000}},
        include_primary=False)
    composing = advance_reference_commission(store, confirmed['commission_ref'],
        {'type': 'speech_aligned', 'plan_ref': confirmed['plan_ref']},
        confirmed['commission_ref']['revision'])
    assert composing['phase'] == 'composing'
    new_ref = _edit(store, composing['plan_ref'], payload={'duration_frames': 72})
    received = reconcile_plan_revision(store, commission_ref=composing['commission_ref'], plan_ref=new_ref)['commission']
    assert received['phase'] == 'composing'
    assert received['review_ref'] == composing['review_ref']
    assert received['plan_ref'] == new_ref


def test_output_change_outside_confirmed_requirement_reopens_review(tmp_path):
    from novelvideo.freezone.remix.plan_revision_settlement import reconcile_plan_revision

    store, confirmed = _reviewed_48_frame_plan(tmp_path,
        {'output_requirements': {'width': 1280}})
    edited = apply_production_command(store, {
        'command_id': 'change-output-outside-direction',
        'plan_ref': {key: confirmed['plan_ref'][key] for key in ('artifact_id', 'revision')},
        'kind': 'set_output', 'target_id': 'plan',
        'payload': {'output': {'width': 1920, 'height': 720, 'fps_num': 24, 'fps_den': 1}},
    })
    new_ref = {**edited['plan_ref'], 'project_id': store.project_id}
    received = reconcile_plan_revision(store, commission_ref=confirmed['commission_ref'], plan_ref=new_ref)['commission']
    assert received['phase'] == 'awaiting_plan_review'
    assert received['review_ref'] is None


@pytest.mark.parametrize('creative', [True, False])
def test_production_revision_uses_existing_creative_fingerprint_rules(reviewed_plan, creative):
    from novelvideo.freezone.remix.plan_revision_settlement import reconcile_plan_revision

    _, store, commission, _ = reviewed_plan
    commission = _confirm(store, commission)
    from novelvideo.freezone import canvas_store
    from novelvideo.freezone.remix.material_work import prepare_material_work
    from novelvideo.freezone.remix.reference_commission import select_material_work

    work = prepare_material_work(store,
        {key: commission['plan_ref'][key] for key in ('artifact_id', 'revision')},
        [{'id': 'product', 'placement_ids': ['primary'], 'description': '产品特写',
          'media_kind': 'image', 'final_step_id': 'image'}],
        [{'id': 'image', 'label': '产品图', 'media_kind': 'image', 'source': 'generate',
          'recipe_id': 'general-image', 'prompt': '产品特写'}], 'selected-material-work')
    canvas = canvas_store.read_canvas(store.project_dir, 'c')
    for node in canvas['nodes']:
        ref = node.get('data', {}).get('productionPlanV2Ref')
        if ref and ref['artifactId'] == commission['plan_ref']['artifact_id']:
            ref['revision'] = commission['plan_ref']['revision']
    canvas_store.save_canvas(store.project_dir, 'c', base_revision=canvas['revision'],
                             build_payload=lambda _: canvas)
    commission = select_material_work(store, commission['commission_ref'], work['work_ref'],
        'select-material-work', host_evidence=AuthenticatedDecisionEvidence('p', 'c', 'u', 'select-work'),
        canvas=canvas_store.read_canvas(store.project_dir, 'c'))
    new_ref = _edit(store, commission['plan_ref'],
                    payload={'purpose': '新开场'} if creative else {'duration_frames': 96})
    result = reconcile_plan_revision(store, commission_ref=commission['commission_ref'], plan_ref=new_ref)
    assert result['changed'] is True
    current = result['commission']
    assert current['direction'] == commission['direction']
    assert current['decisions'] == commission['decisions']
    if creative:
        assert current['phase'] == 'awaiting_plan_review'
        assert current['plan_ref'] == new_ref
        assert current['review_ref'] is None
        assert current.get('work_ref') is None
    else:
        assert current['phase'] == 'preparing_materials'
        assert current['review_ref'] == commission['review_ref']
        assert current['work_ref'] == commission['work_ref']
        assert current['plan_ref'] == commission['plan_ref']


def test_saved_new_creative_revision_rejects_old_user_confirmation(reviewed_plan):
    from novelvideo.freezone.remix.plan_review_settlement import ensure_reviewed_plan_confirmation
    from novelvideo.freezone.remix.reference_director import confirm_reference_decision

    _, store, commission, _ = reviewed_plan
    decision = ensure_reviewed_plan_confirmation(store, commission_ref=commission['commission_ref'])
    _edit(store, commission['plan_ref'])
    before = _snapshot(store.project_dir)
    with pytest.raises(RemixArtifactConflict, match='Plan content changed'):
        confirm_reference_decision(store, decision['decision_ref'], project_id='p',
                                   canvas_id='c', requester_user_id='u', message_id='old-confirmation')
    assert _snapshot(store.project_dir) == before


def test_plan_edit_cannot_interleave_between_latest_check_and_commission_publication(reviewed_plan, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from novelvideo.freezone.remix import plan_revision_settlement as module

    _, store, commission, _ = reviewed_plan
    new_ref = _edit(store, commission['plan_ref'])
    before_revision = commission['commission_ref']['revision']
    original = module.advance_reference_commission
    started, saved = Event(), Event()
    future = None

    def competing_edit():
        started.set()
        result = _edit(store, new_ref, payload={'purpose': '另一次修改'}, command_id='competing-edit')
        saved.set()
        return result

    with ThreadPoolExecutor(max_workers=1) as executor:
        def advance_with_competing_writer(store_arg, *args, **kwargs):
            nonlocal future
            original_revise = store_arg.revise

            def revise_with_competing_writer(*revision_args, **revision_kwargs):
                nonlocal future
                future = executor.submit(competing_edit)
                assert started.wait(1)
                assert not saved.wait(0.2), 'plan edit interleaved before commission publication'
                return original_revise(*revision_args, **revision_kwargs)

            monkeypatch.setattr(store_arg, 'revise', revise_with_competing_writer)
            return original(store_arg, *args, **kwargs)

        monkeypatch.setattr(module, 'advance_reference_commission', advance_with_competing_writer)
        result = module.reconcile_plan_revision(store, commission_ref=commission['commission_ref'], plan_ref=new_ref)
        assert future is not None
        latest_ref = future.result(timeout=2)
    assert result['commission']['commission_ref']['revision'] == before_revision + 1
    assert result['commission']['plan_ref'] == new_ref
    assert latest_ref['revision'] == new_ref['revision'] + 1


def test_revision_of_bound_foreign_actor_plan_is_rejected(reviewed_plan):
    from novelvideo.freezone.remix.plan_revision_settlement import reconcile_plan_revision
    from novelvideo.freezone.remix.reference_branches import prepare_reference_branches

    _, store, parent, _ = reviewed_plan
    foreign = RemixArtifactStore(store.project_dir, 'p', created_by='other')
    plan = foreign.create('production_plan.v2', store.read_latest(parent['plan_ref']['artifact_id'])['payload'], [])
    ref = {'artifact_id': plan['id'], 'revision': 1, 'project_id': 'p'}
    branch = prepare_reference_branches(store, parent['commission_ref'], [ref], 'foreign-plan')['branches'][0]
    before = _snapshot(store.project_dir)
    with pytest.raises(PermissionError, match='another actor'):
        reconcile_plan_revision(store, commission_ref=branch['commission_ref'], plan_ref=ref)
    assert _snapshot(store.project_dir) == before


def test_branch_group_cannot_receive_one_branch_implicitly(reviewed_plan):
    from novelvideo.freezone.remix.plan_revision_settlement import reconcile_plan_revision
    from novelvideo.freezone.remix.reference_branches import prepare_reference_branches

    _, store, parent, _ = reviewed_plan
    group = prepare_reference_branches(store, parent['commission_ref'],
                                      [parent['plan_ref']], 'plan-group')['commission']
    before = _snapshot(store.project_dir)
    with pytest.raises(ValueError, match='bound'):
        reconcile_plan_revision(store, commission_ref=group['commission_ref'], plan_ref=parent['plan_ref'])
    assert _snapshot(store.project_dir) == before


def test_host_mutation_lease_is_reused_without_recursively_locking(reviewed_plan):
    from novelvideo.freezone.remix.plan_revision_settlement import reconcile_plan_revision
    from novelvideo.freezone.remix.production_advancement import _MutationStore

    _, store, commission, _ = reviewed_plan
    new_ref = _edit(store, commission['plan_ref'])
    with store._locked() as guard:
        result = reconcile_plan_revision(_MutationStore(store, guard),
            commission_ref=commission['commission_ref'], plan_ref=new_ref)
    assert result['commission']['plan_ref'] == new_ref
    assert result['commission']['phase'] == 'awaiting_plan_review'


def test_receiving_new_revision_cannot_recover_or_confirm_old_review(reviewed_plan):
    from novelvideo.freezone.remix.plan_revision_settlement import reconcile_plan_revision
    from novelvideo.freezone.remix.plan_review_settlement import ensure_reviewed_plan_confirmation
    from novelvideo.freezone.remix.reference_director import confirm_reference_decision

    _, store, commission, _ = reviewed_plan
    old = ensure_reviewed_plan_confirmation(store, commission_ref=commission['commission_ref'])
    new_ref = _edit(store, commission['plan_ref'])
    result = reconcile_plan_revision(store, commission_ref=commission['commission_ref'], plan_ref=new_ref)
    before = _snapshot(store.project_dir)
    with pytest.raises(ValueError, match='saved passing review'):
        ensure_reviewed_plan_confirmation(store, commission_ref=result['commission']['commission_ref'])
    with pytest.raises(RemixArtifactConflict, match='Decision is stale'):
        confirm_reference_decision(store, old['decision_ref'], project_id='p', canvas_id='c',
                                   requester_user_id='u', message_id='old-review-message')
    assert _snapshot(store.project_dir) == before
    current = store.read_latest(commission['commission_ref']['artifact_id'])['payload']
    assert current['plan_ref'] == new_ref
    assert current.get('review_ref') is None


def test_saved_timing_revision_rejects_old_user_confirmation_before_receiving_it(reviewed_plan):
    from novelvideo.freezone.remix.plan_review_settlement import ensure_reviewed_plan_confirmation
    from novelvideo.freezone.remix.reference_director import confirm_reference_decision

    _, store, commission, _ = reviewed_plan
    old = ensure_reviewed_plan_confirmation(store, commission_ref=commission['commission_ref'])
    new_ref = _edit(store, commission['plan_ref'], payload={'duration_frames': 96})
    before = _snapshot(store.project_dir)
    with pytest.raises(RemixArtifactConflict, match='Plan.*changed'):
        confirm_reference_decision(store, old['decision_ref'], project_id='p', canvas_id='c',
                                   requester_user_id='u', message_id='old-timing-confirmation')
    assert _snapshot(store.project_dir) == before
    current = store.read_latest(commission['commission_ref']['artifact_id'])['payload']
    assert current['phase'] == 'awaiting_plan_confirmation'
    assert current['plan_ref'] != new_ref


def test_timing_edit_cannot_publish_between_exact_confirmation_check_and_approval(reviewed_plan, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from novelvideo.freezone.remix.plan_revision_settlement import reconcile_plan_revision
    from novelvideo.freezone.remix.production_advancement import _MutationStore

    _, store, commission, _ = reviewed_plan
    plan_ref = commission['plan_ref']
    started, saved = Event(), Event()
    original_latest = store.read_latest
    original_locked_latest = _MutationStore.read_latest
    future = None
    interleaved = None

    def competing_timing_edit():
        started.set()
        result = _edit(store, plan_ref, payload={'duration_frames': 96},
                       command_id='timing-edit-during-confirmation')
        saved.set()
        return result

    with ThreadPoolExecutor(max_workers=1) as executor:
        def checked_latest(result):
            nonlocal future, interleaved
            if result['id'] == plan_ref['artifact_id'] and future is None:
                future = executor.submit(competing_timing_edit)
                assert started.wait(1)
                interleaved = saved.wait(0.2)
            return result

        def read_latest(artifact_id):
            return checked_latest(original_latest(artifact_id))

        def read_locked_latest(locked, artifact_id):
            return checked_latest(original_locked_latest(locked, artifact_id))

        monkeypatch.setattr(store, 'read_latest', read_latest)
        monkeypatch.setattr(_MutationStore, 'read_latest', read_locked_latest)
        approved = _confirm(store, commission)
        assert future is not None
        updated_plan_ref = future.result(timeout=2)
    assert approved['phase'] == 'preparing_materials'
    assert approved['plan_ref'] == plan_ref
    assert interleaved is False, 'timing edit saved after exact check but before old plan approval'
    # Once approval is published, timing refinements retain its creative receipt.
    received = reconcile_plan_revision(store, commission_ref=approved['commission_ref'],
                                       plan_ref=updated_plan_ref)['commission']
    assert received['phase'] == 'preparing_materials'
    assert received['review_ref'] == approved['review_ref']
    assert received['decisions'] == approved['decisions']
