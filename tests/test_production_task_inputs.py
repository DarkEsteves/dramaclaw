"""A plan draft's submitted inputs survive signed task submission."""

import pytest
from types import SimpleNamespace

from novelvideo.api.routes.remix import prepare_registered_action
from novelvideo.freezone.remix.production_requirements import resolve_production_requirements
from test_production_requirements import commission_fixture
from test_task_envelope_producer import _producer


def _bound_operation(tmp_path):
    from novelvideo.freezone.remix.operations import RemixOperationStore

    store, commission, _ = commission_fixture(
        tmp_path, {"duration_us": {"max": 10_000_000}}
    )
    resolved = resolve_production_requirements(store, commission=commission)
    prepared = prepare_registered_action(
        store, "generate_production_plan_draft", {"topic": "测试作品", **resolved},
        idempotency_key="bound-run", node_revision=1,
    )
    operation = RemixOperationStore(tmp_path, "p").start(
        idempotency_key="bound-run", input_refs=prepared["input_refs"],
        producer_version=prepared["producer_version"],
        normalized_parameters=prepared["normalized_parameters"], node_revision=1,
        canvas_id='c', node_id='plan',
    )
    payload = {**prepared["task_payload"], "operation_id": operation["operation_id"]}
    ctx = SimpleNamespace(
        output_dir=str(tmp_path), project_id="p", requester_username="alice",
        requester_user_id="u",
    )
    return store, resolved, operation, payload, ctx


@pytest.mark.asyncio
async def test_bound_plan_payload_can_be_signed_without_direction_receipt(tmp_path):
    store, commission, _ = commission_fixture(
        tmp_path, {"duration_us": {"max": 10_000_000}}
    )
    resolved = resolve_production_requirements(store, commission=commission)
    prepared = prepare_registered_action(
        store, "generate_production_plan_draft",
        {"topic": "测试作品", **resolved},
        idempotency_key="signed-plan", node_revision=1,
    )

    signed = await _producer().sign_top_level(
        user_id="u", root_task_id="task-plan", project_id="p",
        task_type="remix_generate_production_plan_draft",
        payload={"payload": prepared["task_payload"]},
    )

    assert signed.admission.requester_user_id == "u"
    assert "requirements_provenance" not in prepared["task_payload"]
    assert 'requirements_provenance' not in prepared['normalized_parameters']['payload']
    assert prepared['task_payload']['requirements'] == resolved['requirements']


def test_plan_runner_uses_submitted_requirements_without_direction_receipt(tmp_path, monkeypatch):
    from novelvideo.task_backend.runners import remix as runner

    store, resolved, _operation, payload, ctx = _bound_operation(tmp_path)
    calls = []

    async def generate(**kwargs):
        calls.append(kwargs)
        return {
            "schema_version": 2, "goal": kwargs["topic"],
            "output": {"width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1},
            "beats": [{"id": "hook", "purpose": "开场", "duration_frames": 90,
                       "turns": []}],
            "resources": [], "placements": [], "provenance": {},
        }

    monkeypatch.setattr(runner, "generate_production_plan_draft", generate)
    result = runner.run_remix_generate_production_plan_draft({"payload": payload}, ctx)
    artifact = store.read(result["artifact_ref"]["artifact_id"], 1)

    assert len(calls) == 1
    assert calls[0]["requirements"] == resolved["requirements"]
    assert 'requirements_provenance' not in artifact['payload']['provenance']
    assert artifact['payload']['provenance']['requirements'] == resolved['requirements']
    assert artifact['payload']['provenance']['node_target'] == {
        'canvas_id': 'c', 'node_id': 'plan', 'node_revision': 1, 'goal': '测试作品',
    }


@pytest.mark.parametrize("field,value", [
    ("topic", "另一种作品"),
    ("requirements", {"duration_us": {"max": 20_000_000}}),
    ("requirements_fingerprint", "forged"),
    ("analysis_context", {"summary": "篡改分析"}),
    ("blueprint_ref", {"artifact_id": "other", "revision": 1}),
    ("locked_turns", [{"speaker": "旁白", "text": "新增台词"}]),
    ("output_preferences", {"aspect_ratio": "1:1"}),
    ("node_revision", 2),
])
def test_plan_runner_rejects_changed_signed_inputs_before_model(
    tmp_path, monkeypatch, field, value,
):
    from novelvideo.task_backend.runners import remix as runner

    _store, _resolved, _operation, payload, ctx = _bound_operation(tmp_path)
    called = []

    async def generate(**kwargs):
        called.append(kwargs)
        raise AssertionError("model must not be called")

    monkeypatch.setattr(runner, "generate_production_plan_draft", generate)
    payload[field] = value
    with pytest.raises(ValueError, match="operation.*payload|task.*operation"):
        runner.run_remix_generate_production_plan_draft({"payload": payload}, ctx)
    assert called == []


def test_manual_source_plan_restores_original_analysis_and_actor(tmp_path):
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.freezone.remix.production_requirements import hydrate_native_requirements
    from novelvideo.freezone.remix.production_task_inputs import restore_production_plan_inputs

    store, commission, _ = commission_fixture(tmp_path, {})
    canvas = {
        "project_id": "p", "canvas_id": "manual",
        "nodes": [
            {"id": "source", "type": "videoStoryNode", "data": {
                "referenceAnalysisRef": commission["analysis_evidence"]["analysis_ref"]}},
            {"id": "plan", "type": "scriptNode"},
        ],
        "edges": [{"source": "source", "target": "plan"}],
    }
    input_payload = {"topic": "按原片台词", "requirements": {
        "dialogue": {"mode": "preserve_source"}}}
    hydrate_native_requirements(
        store, input_payload, canvas=canvas, node_id="plan", requester_user_id="u",
    )
    prepared = prepare_registered_action(
        store, "generate_production_plan_draft", input_payload,
        idempotency_key="manual-source", node_revision=1,
    )
    operation = RemixOperationStore(tmp_path, "p").start(
        idempotency_key="manual-source", input_refs=prepared["input_refs"],
        producer_version=prepared["producer_version"],
        normalized_parameters=prepared["normalized_parameters"], node_revision=1,
    )
    payload = {**prepared["task_payload"], "operation_id": operation["operation_id"]}

    assert "requirements_provenance" not in payload
    restored = restore_production_plan_inputs(store, payload, operation)
    assert restored["requirements_provenance"]["analysis_node_id"] == "source"
    assert restored["locked_turns"] == [{"speaker": "", "text": "Actual speech."}]
    with pytest.raises(PermissionError, match="actor"):
        restore_production_plan_inputs(
            type(store)(tmp_path, "p", created_by="another-user"), payload, operation,
        )


def test_revised_native_node_goal_does_not_inherit_withdrawn_direction_requirements(tmp_path):
    from novelvideo.freezone.remix.production_requirements import hydrate_native_requirements
    from novelvideo.freezone.remix.reference_commission import (
        AuthenticatedDecisionEvidence, advance_reference_commission,
    )
    store, commission, _ = commission_fixture(tmp_path, {'dialogue': {'mode': 'preserve_source'}})
    commission = advance_reference_commission(store, commission['commission_ref'],
        {'type': 'direction_confirmed', 'direction': {
            'target_goal': '保留原片台词', 'requirements': {'dialogue': {'mode': 'preserve_source'}}}},
        commission['commission_ref']['revision'],
        host_evidence=AuthenticatedDecisionEvidence('p', 'c', 'u', 'initial-goal'))
    canvas = {'project_id': 'p', 'canvas_id': 'c', 'nodes': [
        {'id': 'analysis', 'type': 'videoStoryNode', 'data': {
            'referenceAnalysisRef': commission['analysis_evidence']['analysis_ref']}},
        {'id': 'plan', 'type': 'scriptNode', 'data': {'remixRole': 'production_plan',
            'processingPayload': {'topic': '改写台词，保留节奏'}, 'remixNodeRevision': 2}},
    ], 'edges': [{'source': 'analysis', 'target': 'plan'}]}
    payload = {'topic': '改写台词，保留节奏'}
    hydrate_native_requirements(store, payload, canvas=canvas, node_id='plan', requester_user_id='u')
    assert 'requirements' not in payload
    assert 'locked_turns' not in payload
    assert 'requirements_provenance' not in payload


def test_same_goal_confirmed_node_constraint_edit_uses_current_node(tmp_path):
    from novelvideo.freezone.remix.production_requirements import hydrate_native_requirements
    from novelvideo.freezone.remix.reference_commission import (
        AuthenticatedDecisionEvidence, advance_reference_commission,
    )
    store, commission, _ = commission_fixture(tmp_path, {'dialogue': {'mode': 'preserve_source'}})
    commission = advance_reference_commission(store, commission['commission_ref'],
        {'type': 'direction_confirmed', 'direction': {
            'target_goal': '保留产品主题', 'requirements': {'dialogue': {'mode': 'preserve_source'}}}},
        commission['commission_ref']['revision'],
        host_evidence=AuthenticatedDecisionEvidence('p', 'c', 'u', 'initial-constraints'))
    canvas = {'project_id': 'p', 'canvas_id': 'c', 'nodes': [
        {'id': 'analysis', 'type': 'videoStoryNode', 'data': {
            'referenceAnalysisRef': commission['analysis_evidence']['analysis_ref']}},
        {'id': 'plan', 'type': 'scriptNode', 'data': {
            'remixRole': 'production_plan', 'remixDirectionEdited': True,
            'processingPayload': {'topic': '保留产品主题',
                                  'requirements': {'dialogue': {'mode': 'rewrite'}}},
            'remixNodeRevision': 2}},
    ], 'edges': [{'source': 'analysis', 'target': 'plan'}]}
    payload = {'topic': '保留产品主题', 'requirements': {'dialogue': {'mode': 'rewrite'}}}
    hydrate_native_requirements(store, payload, canvas=canvas, node_id='plan', requester_user_id='u')
    assert payload['requirements']['dialogue']['mode'] == 'rewrite'
    assert 'locked_turns' not in payload
    assert 'requirements_provenance' not in payload
    incomplete_payload = {'topic': '保留产品主题'}
    hydrate_native_requirements(store, incomplete_payload, canvas=canvas,
                                node_id='plan', requester_user_id='u')
    assert incomplete_payload['requirements']['dialogue']['mode'] == 'rewrite'
    override = {'topic': '保留产品主题', 'requirements': {'dialogue': {'mode': 'unspecified'}}}
    hydrate_native_requirements(store, override, canvas=canvas, node_id='plan', requester_user_id='u')
    assert override['requirements']['dialogue']['mode'] == 'unspecified'


def test_direction_change_does_not_gate_admission_or_change_signed_task_inputs(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.reference_commission import (
        AuthenticatedDecisionEvidence, advance_reference_commission,
    )
    from novelvideo.task_backend.runners import remix as runner

    _store, _resolved, operation, payload, ctx = _bound_operation(tmp_path)
    store = runner.production_store(ctx)
    provenance = _resolved['requirements_provenance']
    advance_reference_commission(
        store, provenance["commission_ref"],
        {"type": "direction_confirmed", "direction": {"direction": "新的方向"}},
        provenance["commission_ref"]["revision"],
        host_evidence=AuthenticatedDecisionEvidence("p", "c", "u", "new-message"),
    )
    prepared = prepare_registered_action(store, 'generate_production_plan_draft',
        operation['normalized_parameters']['payload'],
        idempotency_key='another-admission', node_revision=1)
    assert prepared['task_payload']['requirements'] == _resolved['requirements']
    called = []
    async def generate(**kwargs):
        called.append(kwargs)
        return {
            "schema_version": 2, "goal": kwargs["topic"],
            "output": {"width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1},
            "beats": [{"id": "hook", "purpose": "开场", "duration_frames": 90,
                       "turns": []}],
            "resources": [], "placements": [], "provenance": {},
        }
    monkeypatch.setattr(runner, "generate_production_plan_draft", generate)
    result = runner.run_remix_generate_production_plan_draft({"payload": payload}, ctx)
    assert result["artifact_ref"]
    assert len(called) == 1
