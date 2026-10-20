import pytest

from novelvideo.freezone.agent_workflows import catalog
from novelvideo.freezone.workflow_schema import workflow_intent_json_schema, workflow_plan_json_schema
from jsonschema import Draft202012Validator


def intent(**inputs):
    return {
        'schema_version': 'freezone_workflow_intent.v1',
        'skill_id': 'reference-video-remix',
        'user_goal': '参考片换成黑咖啡，保持横屏，无台词',
        'inputs': inputs,
        'external_inputs': [{'id': 'analysis', 'node_id': 'existing-analysis', 'media_kind': 'reference_analysis'}],
    }


def test_package_directs_native_planning_after_real_analysis():
    package = catalog.get_workflow_skill({'skill_id': 'reference-video-remix', 'compact': True})
    contract = package['planning_contract']
    assert contract['requires_agent_authored_topology'] is False
    assert contract['native_production']['prepare_tool'] == 'freezone_prepare_production_nodes'
    assert contract['native_production']['analysis_action'] == 'run_video_analyze_story'
    recipe = next(item for item in package['available_recipes'] if item['id'] == 'reference-production-plan')
    assert recipe['node_type'] == 'scriptNode'
    assert recipe['processing_action'] == 'generate_production_plan_draft'


def test_discovered_native_example_matches_the_advertised_tool():
    from novelvideo.chat.dramaclaw_mcp import _load_plugin, _tool_index
    package = catalog.get_workflow_skill({'skill_id': 'reference-video-remix', 'compact': True})
    native = package['planning_contract']['native_production']
    schema, _ = _tool_index(_load_plugin('freezone'))[native['prepare_tool']]
    Draft202012Validator(schema['parameters']).validate(native['request_example'])
    assert native['direction_tool'] is None
    assert 'intent_example' not in native
    assert 'native_production.request_example' in package['planning_contract']['recipe_selection_rule']
    assert 'native_production.request_example' in package['message']


@pytest.mark.parametrize('count', [1, 2])
def test_compact_intent_creates_independent_native_plans(count):
    request = intent(plan_count=count, speech_mode='无台词')
    compiled = catalog.compile_workflow_intent(request)
    assert compiled['ok'], compiled
    plan = compiled['plan']
    Draft202012Validator(workflow_intent_json_schema()).validate(request)
    Draft202012Validator(workflow_plan_json_schema()).validate(plan)
    assert catalog.validate_agent_workflow_plan(plan)['ok']
    plans = [node for node in plan['nodes'] if node['node_type'] == 'scriptNode']
    composers = [node for node in plan['nodes'] if node['node_type'] == 'videoComposeNode']
    assert len(plans) == len(composers) == count
    assert len({node['id'] for node in plans}) == count
    assert {node['node_type'] for node in plan['nodes']} == {'scriptNode', 'videoComposeNode'}
    for node in plans:
        assert node['data']['remixRole'] == 'production_plan'
        assert node['data']['processingAction'] == 'generate_production_plan_draft'
        assert '无台词' in node['data']['processingPayload']['topic']
        assert 'blueprint_ref' not in node['data']['processingPayload']
        assert 'analysis_context' not in node['data']['processingPayload']
        assert any(edge['source'] == 'analysis' and edge['target'] == node['id'] for edge in plan['edges'])
    assert all(node['data']['processingAction'] == 'compile_production_plan' for node in composers)
    assert all('recipeId' not in node['data'].get('workflowCatalog', {}) for node in plan['nodes'])


def test_missing_analysis_binding_requests_current_saved_state():
    request = intent()
    del request['external_inputs']
    result = catalog.compile_workflow_intent(request)
    assert not result['ok']
    assert result['status'] == 'reference_analysis_binding_required'
    assert result['next_action'] == 'read_current_production_state'


def test_plan_only_scope_does_not_create_compose():
    result = catalog.compile_workflow_intent(intent(production_scope='仅作品方案'))
    assert result['ok'], result
    assert [node['node_type'] for node in result['plan']['nodes']] == ['scriptNode']


@pytest.mark.parametrize('count', [0, -1, 1.5, True, 101])
def test_rejects_invalid_plan_count(count):
    assert not catalog.compile_workflow_intent(intent(plan_count=count))['ok']


def test_native_recipe_cannot_fall_back_to_generic_text_items():
    request = intent()
    request['items'] = [{'id': 'plan', 'recipe_id': 'reference-production-plan', 'prompt': '复刻'}]
    result = catalog.compile_workflow_intent(request)
    assert not result['ok']
    assert result['status'] == 'native_production_planner_required'


def test_raw_plan_cannot_run_native_recipe_as_text_generation():
    plan = {
        'schema_version': 'freezone_workflow_plan.v1',
        'skill': {'id': 'reference-video-remix', 'version': '2.1.0'},
        'nodes': [{'id': 'wrong', 'node_type': 'textAnnotationNode', 'stage': 'planning',
                   'data': {'workflowCatalog': {'recipeId': 'reference-production-plan'}}}],
        'edges': [],
    }
    result = catalog.validate_agent_workflow_plan(plan)
    assert not result['ok']
    assert 'native production' in result['error']


def test_named_topic_keeps_other_user_constraints():
    result = catalog.compile_workflow_intent(intent(new_topic='黑咖啡'))
    assert result['ok'], result
    topic = result['plan']['nodes'][0]['data']['processingPayload']['topic']
    assert '保持横屏' in topic
