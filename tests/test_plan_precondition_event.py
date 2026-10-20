from types import SimpleNamespace


def test_only_structured_plan_review_blocker_is_extracted():
    from novelvideo.chat.runtime_event_evidence import plan_review_precondition_blocker

    scope = {'code': 'plan_review_required', 'project_id': 'p', 'canvas_id': 'c',
             'requester_user_id': 'u', 'commission_id': 'commission',
             'plan_ref': {'artifact_id': 'plan', 'revision': 1, 'project_id': 'p'},
             'direction_revision': 1}
    event = SimpleNamespace(name='dramaclaw.freezone_prepare_reference_decision',
                            input={'kind': 'plan'}, structured={'ok': False, 'blocker': scope},
                            output=None)
    assert plan_review_precondition_blocker(event) == scope
    event.structured = {'ok': False, 'error': 'plan must be reviewed before user confirmation'}
    assert plan_review_precondition_blocker(event) is None
    event.structured = {'ok': True, 'blocker': scope}
    assert plan_review_precondition_blocker(event) is None
