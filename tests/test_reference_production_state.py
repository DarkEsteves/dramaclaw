from copy import deepcopy

import pytest


def test_progress_counts_current_steps_once():
    from novelvideo.freezone.remix.production_state import summarize_progress
    facts = [{'step_id': str(i), 'available': True, 'accepted': i < 5} for i in range(9)]
    actions = [{'action_id': str(i), 'owner': 'user', 'status': 'pending'} for i in range(4)]
    progress = summarize_progress(facts + facts[:1], actions + actions[:1])
    assert progress == {'materials_total': 9, 'materials_available': 9, 'materials_accepted': 5,
                        'final_materials_total': 0, 'final_materials_accepted': 0, 'user_action_count': 4}


def test_conflicting_duplicate_facts_are_rejected():
    from novelvideo.freezone.remix.production_state import summarize_progress
    with pytest.raises(ValueError, match='conflicting.*step'):
        summarize_progress([{'step_id': 's', 'available': True, 'accepted': False},
                            {'step_id': 's', 'available': False, 'accepted': False}], [])


def test_progress_only_counts_pending_user_actions_and_final_bindings():
    from novelvideo.freezone.remix.production_state import summarize_progress
    facts = [{'step_id': 'reference', 'available': True, 'accepted': True},
             {'step_id': 'final', 'available': True, 'accepted': True, 'final': True}]
    actions = [{'action_id': str(i), 'owner': owner, 'status': status}
               for i, (owner, status) in enumerate([('user', 'pending'), ('user', 'superseded'),
                                                    ('user', 'completed'), ('agent', 'pending')])]
    progress = summarize_progress(facts, actions)
    assert progress['user_action_count'] == 1
    assert progress['final_materials_total'] == progress['final_materials_accepted'] == 1


def inputs():
    def ref(identity):
        return {'artifact_id': identity, 'revision': 1, 'project_id': 'p'}
    return dict(scope={'project_id': 'p', 'canvas_id': 'c', 'agent_id': 'main'},
                commission={'commission_ref': ref('commission'), 'plan_ref': ref('plan'), 'work_ref': ref('work'),
                            'phase': 'preparing_materials'}, plan={'plan_ref': ref('plan')},
                active_work={'work_ref': ref('work'), 'plan_ref': ref('plan'),
                             'requirements': [{'final_step_id': 's'}]},
                material_facts=[{'step_id': 's', 'available': True, 'accepted': False}],
                pending_actions=[{'action_id': 'review', 'kind': 'review_materials', 'owner': 'user', 'step_id': 's',
                                  'basis_ref': ref('work'), 'status': 'pending'}], execution={'status': 'waiting_user'})


def test_projection_is_pure_and_ignores_old_work_actions():
    from novelvideo.freezone.remix.production_state import build_production_state
    args = inputs()
    args['pending_actions'] += [{**args['pending_actions'][0], 'action_id': f'old-{i}',
                                'basis_ref': {'artifact_id': 'old-work', 'revision': 1}} for i in range(13)]
    before = deepcopy(args)
    result = build_production_state(**args)
    assert args == before
    assert result['schema_version'] == 'reference_production_state.v1'
    assert result['progress']['user_action_count'] == 1
    assert result['progress']['final_materials_total'] == 1
    assert result['next_action'] == {'owner': 'user', 'kind': 'review_materials', 'action_id': 'review'}
    assert set(result) == {'schema_version', 'scope', 'commission_ref', 'plan_ref', 'work_ref',
                           'projection_revision', 'phase', 'execution_status', 'progress', 'blockers', 'next_action'}
    assert build_production_state(**args) == result
    args['pending_actions'].reverse()
    assert build_production_state(**args)['projection_revision'] == result['projection_revision']


def test_stopped_projection_never_suggests_automatic_resume():
    from novelvideo.freezone.remix.production_state import build_production_state
    args = inputs()
    args['execution'] = {'status': 'stopped'}
    result = build_production_state(**args)
    assert result['execution_status'] == 'stopped'
    assert result['next_action'] is None


def test_unselected_work_is_blocked_without_adopting_a_candidate():
    from novelvideo.freezone.remix.production_state import build_production_state
    args = inputs()
    args['commission']['work_ref'] = None
    args['active_work'] = None
    result = build_production_state(**args)
    assert result['work_ref'] is None
    assert result['progress']['materials_total'] == 0
    assert result['next_action']['kind'] == 'select_material_work'
    assert result['blockers'][0]['kind'] == 'work_selection_required'


def test_projection_rejects_unselected_identity_and_secret_scope():
    from novelvideo.freezone.remix.production_state import build_production_state
    args = inputs()
    args['active_work']['work_ref']['artifact_id'] = 'other'
    with pytest.raises(ValueError, match='selected work'):
        build_production_state(**args)
    args = inputs()
    args['scope']['token'] = 'secret'
    with pytest.raises(ValueError, match='scope'):
        build_production_state(**args)


def test_projection_requires_facts_for_every_current_step():
    from novelvideo.freezone.remix.production_state import build_production_state
    args = inputs()
    args['active_work']['steps'] = [{'id': 's'}, {'id': 'reference'}]
    with pytest.raises(ValueError, match='all current steps'):
        build_production_state(**args)


def test_unavailable_selected_work_requests_system_recovery():
    from novelvideo.freezone.remix.production_state import build_production_state
    args = inputs()
    args['active_work'] = None
    result = build_production_state(**args)
    assert result['work_ref'] == args['commission']['work_ref']
    assert result['next_action'] == {'owner': 'system', 'kind': 'selected_work_unavailable', 'action_id': None}


def test_action_reference_normalization_keeps_same_revision():
    from novelvideo.freezone.remix.production_state import build_production_state
    args = inputs()
    result = build_production_state(**args)
    del args['pending_actions'][0]['basis_ref']['project_id']
    assert build_production_state(**args) == result


def test_projection_preserves_both_system_and_user_blockers():
    from novelvideo.freezone.remix.production_state import build_production_state
    args = inputs()
    args['execution']['blockers'] = [{'owner': 'system', 'kind': 'register_result', 'action_id': 'settle'}]
    result = build_production_state(**args)
    assert len(result['blockers']) == 2
    assert result['next_action'] == {'owner': 'system', 'kind': 'register_result', 'action_id': 'settle'}


@pytest.mark.parametrize('phase,basis,kind', [('awaiting_direction', 'commission_ref', 'confirm_direction'),
                                            ('awaiting_plan_confirmation', 'plan_ref', 'confirm_plan'),
                                            ('reviewing_output', 'commission_ref', 'confirm_output')])
def test_nonmaterial_confirmations_count_without_active_work(phase, basis, kind):
    from novelvideo.freezone.remix.production_state import build_production_state
    args = inputs()
    args['commission'].update(phase=phase, work_ref=None)
    args['active_work'] = None
    args['pending_actions'] = [{'action_id': 'confirm', 'owner': 'user', 'kind': kind, 'step_id': None,
                               'basis_ref': args['commission'][basis], 'status': 'pending'}]
    result = build_production_state(**args)
    assert result['progress']['user_action_count'] == 1
    assert result['next_action'] == {'owner': 'user', 'kind': kind, 'action_id': 'confirm'}
    args['execution'] = {'status': 'stopped'}
    assert build_production_state(**args)['next_action'] is None


def test_actions_on_old_commission_or_plan_head_do_not_pollute_current_confirmation():
    from novelvideo.freezone.remix.production_state import build_production_state
    args = inputs()
    args['pending_actions'] = [{'action_id': 'confirm', 'owner': 'user', 'kind': 'confirm_plan', 'step_id': None,
                               'basis_ref': args['commission']['plan_ref'], 'status': 'pending'}]
    args['pending_actions'] += [{**args['pending_actions'][0], 'basis_ref': {**args['commission']['plan_ref'], 'revision': 9}}]
    result = build_production_state(**args)
    assert result['progress']['user_action_count'] == 1
    assert result['next_action']['kind'] == 'confirm_plan'


def test_completed_commission_is_terminal_even_without_a_continuation_row():
    from novelvideo.freezone.remix.production_state import build_production_state
    args = inputs()
    args['commission']['phase'] = 'completed'
    args['execution'] = {'status': 'idle'}
    args['pending_actions'] = []
    state = build_production_state(**args)
    assert state['execution_status'] == 'completed'
    assert state['next_action'] is None
