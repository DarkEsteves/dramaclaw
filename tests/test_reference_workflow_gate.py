from copy import deepcopy

from novelvideo.freezone.remix.reference_commission import (
    AuthenticatedDecisionEvidence,
    advance_reference_commission,
    create_reference_commission,
)
from novelvideo.freezone.agent_workflows.reference_production import compile_reference_production_plan
from novelvideo.freezone.remix.reference_director import prepare_reference_decision
from novelvideo.freezone.remix.store import RemixArtifactStore
from novelvideo.freezone.remix.reference_workflow_gate import reference_workflow_stage_problem
import pytest
from fastapi import HTTPException
from types import SimpleNamespace


def plan(node_id='analysis', skill='reference-video-remix'):
    return {'skill': {'id': skill}, 'external_inputs': [
        {'id': 'reference', 'node_id': node_id, 'media_kind': 'reference_analysis'}]}


def native_plan():
    compiled = compile_reference_production_plan(
        {'external_inputs': [{'id': 'analysis', 'node_id': 'analysis',
                              'media_kind': 'reference_analysis'}]},
        {'id': 'reference-video-remix', 'version': '2.1.0'},
        {'plan_count': 1}, '改成黑咖啡双人对话',
    )
    assert compiled['ok'] is True
    return compiled['plan']


def confirmed_direction(store):
    current = create_reference_commission(
        store, canvas_id='c', requester_user_id='u', analysis_node_id='analysis',
        goal='改成黑咖啡双人对话', analysis_evidence={'task_id': 't', 'result_fingerprint': 'h'},
    )
    return advance_reference_commission(
        store, current['commission_ref'],
        {'type': 'direction_confirmed', 'direction': {'target_goal': '改成黑咖啡双人对话'}},
        current['commission_ref']['revision'],
        host_evidence=AuthenticatedDecisionEvidence('p', 'c', 'u', 'message-1'),
    )


def test_bound_workflow_cannot_skip_pending_direction(tmp_path):
    store = RemixArtifactStore(tmp_path / 'output', 'p', created_by='u')
    commission = create_reference_commission(store, canvas_id='c', requester_user_id='u',
        analysis_node_id='analysis', goal='g', analysis_evidence={'task_id': 't', 'result_fingerprint': 'h'})
    prepare_reference_decision(store, commission['commission_ref'], 'direction',
                               {'direction': 'coffee'}, 'coffee', 'd')
    assert reference_workflow_stage_problem(store, canvas_id='c', requester_user_id='u',
                                            plan=plan()) == 'reference_direction_pending'


def test_confirmed_direction_allows_native_plan_stage(tmp_path):
    store = RemixArtifactStore(tmp_path / 'output', 'p', created_by='u')
    current = confirmed_direction(store)
    assert current['phase'] == 'drafting_plan'
    assert reference_workflow_stage_problem(
        store, canvas_id='c', requester_user_id='u', plan=native_plan(),
    ) is None


def test_confirmed_direction_rejects_extra_generation_node(tmp_path):
    store = RemixArtifactStore(tmp_path / 'output', 'p', created_by='u')
    confirmed_direction(store)
    unsafe = deepcopy(native_plan())
    unsafe['nodes'].append({
        'id': 'image', 'node_type': 'imageGenNode', 'stage': 'materials',
        'data': {'processingAction': 'generate_image'},
    })
    assert reference_workflow_stage_problem(
        store, canvas_id='c', requester_user_id='u', plan=unsafe,
    ) == 'reference_plan_must_use_native_stage'


def test_shared_analysis_cannot_choose_a_commission_by_iteration_order(tmp_path):
    store = RemixArtifactStore(tmp_path / 'output', 'p', created_by='u')
    confirmed_direction(store)
    confirmed_direction(store)
    assert reference_workflow_stage_problem(
        store, canvas_id='c', requester_user_id='u', plan=native_plan(),
    ) == 'reference_commission_ambiguous'


def test_explicit_commission_selects_its_own_confirmed_direction(tmp_path):
    from novelvideo.freezone.remix.reference_workflow_gate import reference_workflow_stage_decision

    store = RemixArtifactStore(tmp_path / 'output', 'p', created_by='u')
    first = confirmed_direction(store)
    confirmed_direction(store)
    problem, binding = reference_workflow_stage_decision(
        store, canvas_id='c', requester_user_id='u', plan=native_plan(),
        commission_id=first['commission_ref']['artifact_id'],
    )
    assert problem is None
    assert binding == {
        'project_id': 'p', 'canvas_id': 'c', 'requester_user_id': 'u',
        'commission_id': first['commission_ref']['artifact_id'],
        'analysis_node_id': 'analysis', 'direction_revision': first['direction_revision'],
        'direction_fingerprint': first['direction_fingerprint'], 'stage': 'production_plans',
    }


def test_workflow_intent_can_select_a_saved_reference_commission():
    from novelvideo.freezone.workflow_schema import workflow_intent_json_schema

    schema = workflow_intent_json_schema()
    assert schema['properties']['reference_commission_id']['type'] == 'string'


@pytest.mark.asyncio
async def test_api_gate_returns_binding_for_the_confirmed_native_stage(tmp_path):
    from novelvideo.api.routes.freezone import _enforce_reference_workflow_stage

    store = RemixArtifactStore(tmp_path / 'output', 'p', created_by='u')
    current = confirmed_direction(store)
    ctx = SimpleNamespace(output_dir=tmp_path / 'output', project_id='p', requester_user_id='u')
    binding = await _enforce_reference_workflow_stage(ctx, 'c', native_plan())
    assert binding['commission_id'] == current['commission_ref']['artifact_id']
    assert binding['direction_revision'] == current['direction_revision']


def test_independent_canvas_workflow_remains_available(tmp_path):
    store = RemixArtifactStore(tmp_path / 'output', 'p', created_by='u')
    create_reference_commission(store, canvas_id='c', requester_user_id='u',
        analysis_node_id='analysis', goal='g', analysis_evidence={'task_id': 't', 'result_fingerprint': 'h'})
    assert reference_workflow_stage_problem(store, canvas_id='c', requester_user_id='u',
                                            plan=plan(node_id='manual-analysis')) is None
    assert reference_workflow_stage_problem(store, canvas_id='c', requester_user_id='u',
                                            plan=plan(skill='other')) is None


@pytest.mark.asyncio
async def test_shared_workflow_api_gate_rejects_bound_draft(tmp_path):
    from novelvideo.api.routes.freezone import _enforce_reference_workflow_stage
    store = RemixArtifactStore(tmp_path / 'output', 'p', created_by='u')
    create_reference_commission(store, canvas_id='c', requester_user_id='u',
        analysis_node_id='analysis', goal='g', analysis_evidence={'task_id': 't', 'result_fingerprint': 'h'})
    ctx = SimpleNamespace(output_dir=tmp_path / 'output', project_id='p', requester_user_id='u')
    with pytest.raises(HTTPException) as error:
        await _enforce_reference_workflow_stage(ctx, 'c', plan())
    assert error.value.status_code == 409
    assert error.value.detail['code'] == 'reference_direction_pending'
    await _enforce_reference_workflow_stage(ctx, 'c', plan(node_id='independent'))
