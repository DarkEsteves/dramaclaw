import json
import pytest
from novelvideo.chat import dramaclaw_mcp

@pytest.fixture
def plugin(monkeypatch):
    monkeypatch.setenv('DRAMACLAW_PROJECT_ID', 'project')
    monkeypatch.setenv('DRAMACLAW_CANVAS_ID', 'canvas')
    monkeypatch.setenv('DRAMACLAW_CHAT_SURFACE', 'freezone')
    return dramaclaw_mcp._load_plugin('freezone')

def args():
    return {'commission_ref': {'artifact_id': 'commission', 'project_id': 'project', 'artifact_type': 'reference_commission', 'revision': 1}, 'goal': 'A complete goal', 'run_after_create': True}

@pytest.mark.parametrize('status', ['pending', 'applied', 'cancelled'])
def test_native_submission_reuses_confirm_handler(plugin, monkeypatch, status):
    calls = []
    monkeypatch.setattr(plugin, '_request', lambda method, path, **kw: calls.append((method, path, kw)) or {'ok': True, 'data': {'draft_id': 'draft', 'revision': 7, 'status': 'ready'}})
    confirmations = []
    monkeypatch.setattr(plugin, '_handle_confirm_workflow_draft', lambda value: confirmations.append(value) or json.dumps({'ok': True, 'status': status, 'draft_id': 'draft'}))
    result = json.loads(plugin._handle_prepare_production_nodes(args()))
    assert result['status'] == status
    assert len(calls) == 1
    assert calls[0][1].endswith('/director/production-nodes/prepare')
    assert calls[0][2]['body'] == args()
    assert confirmations == [{'project_id': 'project', 'canvas_id': 'canvas', 'draft_id': 'draft', 'revision': 7}]

@pytest.mark.parametrize('status', ['submitted', 'confirming', 'confirmed', 'cancelled'])
def test_native_submission_reuses_terminal_or_pending_draft(plugin, monkeypatch, status):
    monkeypatch.setattr(plugin, '_request', lambda *a, **kw: {'ok': True, 'data': {'draft_id': 'draft', 'revision': 1, 'status': status, 'native_plan_stage': {'state': 'running'}}})
    monkeypatch.setattr(plugin, '_handle_confirm_workflow_draft', lambda *a: pytest.fail('must reuse receipt'))
    assert json.loads(plugin._handle_prepare_production_nodes(args()))['draft_id'] == 'draft'


def test_failed_native_stage_is_not_reported_as_confirmed_submission(plugin, monkeypatch):
    response = {
        'ok': True,
        'data': {
            'draft_id': 'draft',
            'revision': 1,
            'status': 'confirmed',
            'task_id': 'draft-task',
            'native_plan_stage': {
                'state': 'failed',
                'slots': [{
                    'plan_node_id': 'plan-node',
                    'state': 'failed',
                    'run_id': 'old-run',
                    'next_action': {'owner': 'agent', 'kind': 'inspect_failure'},
                }],
                'next_action': {'owner': 'agent', 'kind': 'inspect_failure'},
            },
        },
    }
    monkeypatch.setattr(plugin, '_request', lambda *a, **kw: response)

    result = json.loads(plugin._handle_prepare_production_nodes(args()))

    assert result['ok'] is False
    assert result['status'] == 'production_plan_failed'
    assert result['code'] == 'production_plan_failed'
    assert result['native_plan_stage']['state'] == 'failed'
    assert result['next_action']['kind'] == 'inspect_failure'
    assert '没有重新起草' in result['agent_instruction']
    assert 'freezone_run_node_action' in result['agent_instruction']
    mcp_result = dramaclaw_mcp._structured_tool_result(
        'freezone_prepare_production_nodes',
        json.dumps(result, ensure_ascii=False),
        dramaclaw_mcp._tool_index(plugin),
    )
    assert mcp_result.isError is True
    assert mcp_result.structuredContent['code'] == 'production_plan_failed'


def test_failed_plan_retry_reuses_saved_submission_payload(plugin, monkeypatch):
    saved_payload = {
        'topic': '原始确认目标',
        'requirements': {'dialogue': {'mode': 'rewrite'}},
        'blueprint_ref': {'artifact_id': 'blueprint', 'revision': 1},
    }
    monkeypatch.setattr(plugin, '_reference_analysis_current_state', lambda *a: {
        'ok': True,
        'data': {'phase': 'drafting_plan', 'native_plan_stage': {
            'state': 'failed',
            'slots': [{'plan_node_id': 'plan-node', 'state': 'failed'}],
        }},
    })
    monkeypatch.setattr(plugin, '_request', lambda method, path, **kw: {
        'ok': True,
        'data': {'nodes': [{
            'id': 'plan-node',
            'data': {'productionWorkflowSubmission': {'payload': saved_payload}},
        }]},
    })
    captured = {}
    monkeypatch.setattr(plugin, '_single_write_command', lambda _args, command: captured.update(command) or json.dumps({'ok': True}))

    result = json.loads(plugin._handle_run_node_action({
        'project_id': 'project',
        'canvas_id': 'canvas',
        'node_id': 'plan-node',
        'action': 'generate_production_plan_draft',
    }))

    assert result['ok'] is True
    assert captured['parameters'] == saved_payload


def test_plan_retry_without_saved_payload_does_not_create_confirmation(plugin, monkeypatch):
    monkeypatch.setattr(plugin, '_reference_analysis_current_state', lambda *a: {
        'ok': True,
        'data': {'phase': 'drafting_plan', 'native_plan_stage': {
            'state': 'failed',
            'slots': [{'plan_node_id': 'plan-node', 'state': 'failed'}],
        }},
    })
    monkeypatch.setattr(plugin, '_request', lambda *a, **kw: {'ok': True, 'data': {'nodes': []}})
    monkeypatch.setattr(plugin, '_single_write_command', lambda *a: pytest.fail('must not create a retry card'))

    result = json.loads(plugin._handle_run_node_action({
        'project_id': 'project',
        'canvas_id': 'canvas',
        'node_id': 'plan-node',
        'action': 'generate_production_plan_draft',
    }))

    assert result['ok'] is False
    assert result['status'] == 'native_plan_parameters_missing'

@pytest.mark.parametrize('field,value', [('run_after_create', None), ('run_after_create', 'true'), ('commission_ref', {}), ('goal', '')])
def test_native_submission_validates_before_write(plugin, monkeypatch, field, value):
    monkeypatch.setattr(plugin, '_request', lambda *a, **kw: pytest.fail('must validate before write'))
    request = args()
    request[field] = value
    result = json.loads(plugin._handle_prepare_production_nodes(request))
    assert result['ok'] is False
    assert result['field_path'] == field

def test_codex_loads_same_native_tool(plugin, monkeypatch):
    index = dramaclaw_mcp._tool_index(plugin)
    schema, handler = index['freezone_prepare_production_nodes']
    assert schema['parameters']['required'] == ['commission_ref', 'goal', 'run_after_create']
    assert schema['parameters']['additionalProperties'] is False
    from jsonschema import validate
    receipt = {'draft_id': 'draft', 'revision': 1, 'status': 'submitted',
               'native_plan_stage': {'state': 'running'}, 'next_action': {'kind': 'await_completion_event'}}
    monkeypatch.setattr(plugin, '_request', lambda *a, **kw: {'ok': True, 'data': receipt})
    result = json.loads(handler(args()))
    assert result == {'ok': True, **receipt}
    validate(result, schema['output_schema'])
    assert dramaclaw_mcp._adapt_external_agent_tool_result('freezone_prepare_production_nodes', json.dumps(result)) == json.dumps(result)

def test_native_confirm_receipt_keeps_existing_canvas_contract(plugin):
    schema, _ = dramaclaw_mcp._tool_index(plugin)['freezone_prepare_production_nodes']
    confirm_schema = dramaclaw_mcp._tool_index(plugin)['freezone_confirm_workflow_draft'][0]
    assert schema['output_schema']['allOf'][0]['then'] == confirm_schema['output_schema']['allOf'][0]['then']

def test_missing_scope_rejected_before_prepare(plugin, monkeypatch):
    monkeypatch.setattr(plugin, '_default_project_id', lambda: '')
    monkeypatch.setattr(plugin, '_default_canvas_id', lambda: '')
    monkeypatch.setattr(plugin, '_request', lambda *a, **kw: pytest.fail('must validate scope'))
    assert json.loads(plugin._handle_prepare_production_nodes(args()))['status'] == 'workflow_draft_scope_required'


def test_native_prepare_timeout_requires_inspection_without_resubmitting(plugin, monkeypatch):
    from jsonschema import validate
    calls = []
    def timeout(method, path, **kwargs):
        calls.append((method, path))
        raise TimeoutError('response lost')
    monkeypatch.setattr(plugin, '_request', timeout)
    monkeypatch.setattr(plugin, '_handle_confirm_workflow_draft', lambda *a: pytest.fail('unknown draft must not be confirmed'))
    result = json.loads(plugin._handle_prepare_production_nodes(args()))
    assert len(calls) == 1
    assert result['ok'] is False
    assert result['status'] == 'production_nodes_outcome_unknown'
    assert result['next_action']['tool'] == 'freezone_get_production_progress'
    assert result['next_action']['commission_ref'] == args()['commission_ref']
    assert result['next_action']['kind'] == 'inspect_before_retry'
    validate(result, dramaclaw_mcp._tool_index(plugin)['freezone_prepare_production_nodes'][0]['output_schema'])


def test_native_local_validation_matches_output_schema(plugin, monkeypatch):
    from jsonschema import validate
    monkeypatch.setattr(plugin, '_request', lambda *a, **kw: pytest.fail('invalid request must not write'))
    result = json.loads(plugin._handle_prepare_production_nodes({**args(), 'run_after_create': 'true'}))
    validate(result, dramaclaw_mcp._tool_index(plugin)['freezone_prepare_production_nodes'][0]['output_schema'])


@pytest.mark.parametrize('verdict', ['clarify', 'not_authorized'])
@pytest.mark.parametrize('shared_errors', [False, True])
def test_direction_rejection_reaches_agent_with_reason_and_no_confirmation(plugin, monkeypatch, verdict, shared_errors):
    if not shared_errors:
        monkeypatch.setattr(plugin, '_SHARED_API_ERRORS', None)
    reason = '目标加入了用户未要求的新场景；请保留用户要求不变的内容。'
    response = plugin._http_error_result(409, json.dumps({'detail': {
        'code': 'direction_needs_discussion', 'verdict': verdict, 'reason': reason,
        'next_action': 'revise_goal_or_discuss',
        'agent_instruction': 'Keep ordinary dialogue; do not create a direction decision.',
    }}), 'Conflict')
    calls = []
    monkeypatch.setattr(plugin, '_request', lambda *a, **kw: calls.append(a) or response)
    monkeypatch.setattr(plugin, '_handle_confirm_workflow_draft', lambda *a: pytest.fail('rejected direction must not reach approval'))
    raw = plugin._handle_prepare_production_nodes(args())
    receipt = dramaclaw_mcp._structured_tool_result(
        'freezone_prepare_production_nodes', raw, dramaclaw_mcp._tool_index(plugin))
    assert receipt.isError is True
    assert receipt.structuredContent['code'] == 'direction_needs_discussion'
    assert receipt.structuredContent['verdict'] == verdict
    assert receipt.structuredContent['reason'] == reason
    assert receipt.structuredContent['error'] == reason
    assert receipt.structuredContent['next_action'] == 'revise_goal_or_discuss'
    assert len(calls) == 1


@pytest.mark.parametrize('reason', [
    'see https://private.example/path?token=private',
    'Authorization: Bearer secret-value',
    {'raw': 'unbounded detail'},
])
def test_direction_rejection_does_not_forward_unsafe_reason(plugin, reason):
    result = plugin._http_error_result(409, json.dumps({'detail': {
        'code': 'direction_needs_discussion', 'verdict': 'clarify', 'reason': reason,
    }}), 'Conflict')
    assert 'reason' not in result
    assert result['error'] == 'Conflict'


def test_material_work_internal_error_keeps_bounded_agent_diagnostics(plugin):
    result = plugin._http_error_result(500, json.dumps({"detail": {
        "code": "material_work_internal_error",
        "phase": "material_plan_drafting",
        "error": "素材工作包保存失败",
        "errors": [{"path": "steps[0].source", "message": "必须使用已保存来源"}],
        "next_action": "correct_material_request",
        "retryable": False,
        "side_effects": "none",
        "request_id": "req-123",
    }}), "Internal Server Error")
    assert result["code"] == "material_work_internal_error"
    assert result["phase"] == "material_plan_drafting"
    assert result["errors"] == [{"path": "steps[0].source", "message": "必须使用已保存来源"}]
    assert result["next_action"] == "correct_material_request"
    assert result["retryable"] is False
    assert result["side_effects"] == "none"
    assert result["request_id"] == "req-123"


def test_obsolete_direction_tool_cannot_write_another_decision(plugin, monkeypatch):
    schema, handler = dramaclaw_mcp._tool_index(plugin)['freezone_prepare_reference_decision']
    monkeypatch.setattr(plugin, '_request', lambda *a, **kw: pytest.fail('direction is conversation, no decision write'))
    result = json.loads(handler({'commission_ref': args()['commission_ref'], 'kind': 'direction',
                                 'command_id': 'old-client', 'proposal': {}, 'summary': 'agreed direction'}))
    assert result['ok'] is False
    assert result['code'] == 'direction_discussion_only'
    assert result['next_action'] == 'freezone_prepare_production_nodes'
    assert 'direction' not in schema['parameters']['properties']['kind']['enum']
