"""Native drafting executes supplied/node inputs without a second direction gate."""

from copy import deepcopy

import pytest

from novelvideo.api.routes.remix import prepare_registered_action
from novelvideo.freezone.remix.production_requirements import (
    hydrate_native_requirements, resolve_production_requirements,
)
from novelvideo.freezone.remix.reference_commission import (
    AuthenticatedDecisionEvidence, advance_reference_commission,
)
from test_production_requirements import commission_fixture


def _direction_node(tmp_path):
    store, commission, _ = commission_fixture(tmp_path, {'dialogue': {'mode': 'rewrite'}})
    commission = advance_reference_commission(store, commission['commission_ref'],
        {'type': 'direction_confirmed', 'direction': {
            'target_goal': '用户作品目标', 'requirements': {'dialogue': {'mode': 'rewrite'}}}},
        commission['commission_ref']['revision'],
        host_evidence=AuthenticatedDecisionEvidence('p', 'c', 'u', 'direction'))
    payload = {'topic': '用户作品目标\n声音方式：自动判断',
               **resolve_production_requirements(store, commission=commission)}
    node = {'id': 'plan', 'type': 'scriptNode', 'data': {
        'remixRole': 'production_plan', 'processingAction': 'generate_production_plan_draft',
        'processingPayload': deepcopy(payload), 'remixNodeRevision': 1}}
    canvas = {'project_id': 'p', 'canvas_id': 'c', 'nodes': [node,
        {'id': 'analysis', 'type': 'videoStoryNode', 'data': {
            'referenceAnalysisRef': commission['analysis_evidence']['analysis_ref']}}],
        'edges': [{'source': 'analysis', 'target': 'plan'}]}
    return store, commission, canvas, payload


@pytest.mark.parametrize('topic', [None, '用户作品目标', '本次动作提交的新目标'])
def test_native_draft_accepts_current_action_without_comparing_direction(tmp_path, topic):
    store, _, canvas, payload = _direction_node(tmp_path)
    if topic is not None:
        payload['topic'] = topic
    expected = payload['topic']
    hydrate_native_requirements(store, payload, canvas=canvas, node_id='plan', requester_user_id='u')
    prepared = prepare_registered_action(store, 'generate_production_plan_draft', payload,
        idempotency_key='direct', node_revision=1)
    assert prepared['task_payload']['topic'] == expected
    assert prepared['task_payload']['requirements'] == {'dialogue': {'mode': 'rewrite'}}
    assert 'requirements_provenance' not in prepared['normalized_parameters']['payload']


def test_native_draft_ignores_old_direction_receipt_and_uses_current_requirements(tmp_path):
    store, commission, canvas, payload = _direction_node(tmp_path)
    advance_reference_commission(store, commission['commission_ref'],
        {'type': 'direction_confirmed', 'direction': {'target_goal': '另一轮方向'}},
        commission['commission_ref']['revision'],
        host_evidence=AuthenticatedDecisionEvidence('p', 'c', 'u', 'later-direction'))
    payload['requirements'] = {'primary_shots': {'min': 2}}
    hydrate_native_requirements(store, payload, canvas=canvas, node_id='plan', requester_user_id='u')
    prepared = prepare_registered_action(store, 'generate_production_plan_draft', payload,
        idempotency_key='current-inputs', node_revision=1)
    assert prepared['task_payload']['requirements'] == {'primary_shots': {'min': 2}}
    assert 'requirements_provenance' not in prepared['normalized_parameters']['payload']


def test_native_draft_fills_missing_fields_from_node_not_saved_direction(tmp_path):
    store, _, canvas, _ = _direction_node(tmp_path)
    canvas['nodes'][0]['data']['processingPayload'] = {
        'topic': '当前节点目标', 'requirements': {'primary_shots': {'min': 3}}}
    payload = {'analysis_context': {'summary': '原片分析'}}
    hydrate_native_requirements(store, payload, canvas=canvas, node_id='plan', requester_user_id='u')
    assert payload['topic'] == '当前节点目标'
    assert payload['requirements'] == {'primary_shots': {'min': 3}}
    assert 'requirements_provenance' not in payload


def test_direction_receipt_does_not_block_publication_to_unchanged_node(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from novelvideo.freezone.remix import production_processing_settlement as settlement

    store, commission, canvas, payload = _direction_node(tmp_path)
    node = canvas['nodes'][0]
    node['data']['productionWorkflowSubmission'] = {
        'action': 'generate_production_plan_draft', 'payload': deepcopy(payload),
        'nodeData': settlement._node_input(node), 'analysisNodeId': 'analysis',
        'analysisData': settlement._analysis_input(canvas['nodes'][1]),
    }
    hydrate_native_requirements(store, payload, canvas=canvas, node_id='plan', requester_user_id='u')
    advance_reference_commission(store, commission['commission_ref'],
        {'type': 'direction_confirmed', 'direction': {'target_goal': '后续讨论'}},
        commission['commission_ref']['revision'],
        host_evidence=AuthenticatedDecisionEvidence('p', 'c', 'u', 'later-message'))
    monkeypatch.setattr(settlement, '_operation_automation_current', lambda *a, **k: True)
    monkeypatch.setattr(settlement, '_plan_binding_current', lambda *a, **k: True)
    ctx = SimpleNamespace(project_id='p', requester_user_id='u')
    action = {'node_id': 'plan', 'action': 'generate_production_plan_draft',
              'node_revision': 1, 'payload': payload}
    assert settlement._safe_plan_target(ctx, 'c', canvas, node, action,
                                       {'artifactId': 'result', 'revision': 1}, store)
