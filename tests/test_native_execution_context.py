from types import SimpleNamespace

import pytest


@pytest.mark.parametrize(('credential_kind', 'verified_bridge', 'expected'), [
    ('browser', False, 'canvas_user'),
    ('agent_session', False, 'agent_command'),
    ('local_trusted_agent', False, 'agent_command'),
    ('browser', True, 'agent_command'),
])
def test_native_execution_source_uses_host_identity(credential_kind, verified_bridge, expected):
    from novelvideo.freezone.remix.native_execution_context import resolve_native_execution_source

    assert resolve_native_execution_source(
        credential_kind=credential_kind, has_verified_bridge=verified_bridge,
    ) == expected


def test_manual_action_can_publish_after_automation_stops_without_resuming_it(monkeypatch):
    from novelvideo.freezone.remix import production_processing_settlement as settlement
    from novelvideo.freezone.remix import production_automation

    stopped = {'active': False, 'generation': 1}
    monkeypatch.setattr(settlement, 'native_action_automation_control', lambda *args, **kwargs: stopped)
    monkeypatch.setattr(production_automation, 'assert_operation_automation_current',
                        lambda *args, **kwargs: None)
    ctx = SimpleNamespace(project_id='project', requester_user_id='user')
    action = {'processing_operation_id': 'a' * 32, '_host_execution_source': 'canvas_user'}

    assert settlement._operation_automation_current(ctx, action, canvas_id='canvas', canvas={'nodes': []})
    assert stopped['active'] is False
    assert not settlement._operation_automation_current(
        ctx, {**action, '_host_execution_source': 'agent_command'}, canvas_id='canvas', canvas={'nodes': []})


def test_manual_run_requires_matching_saved_host_scope():
    from novelvideo.freezone.remix.native_execution_context import is_manual_native_run

    run = {'run_id': 'run-one', 'project_id': 'project', 'canvas_id': 'canvas', 'actor_id': 'user',
           'metadata': {'native_execution_source': 'canvas_user', 'native_execution_context': {
               'source': 'canvas_user', 'run_id': 'run-one', 'project_id': 'project',
               'canvas_id': 'canvas', 'requester_user_id': 'user',
           }}}
    assert is_manual_native_run(run)
    assert not is_manual_native_run({**run, 'actor_id': 'other-user'})
    assert not is_manual_native_run({**run, 'run_id': 'other-run'})
    assert not is_manual_native_run({**run, 'metadata': {'native_execution_source': 'canvas_user'}})
