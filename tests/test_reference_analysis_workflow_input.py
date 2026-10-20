from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from novelvideo.freezone.agent_workflows.graph import build_workflow_graph_commands
from novelvideo.freezone.workflow_external_inputs import resolve_external_image_inputs
from novelvideo.freezone.workflow_plan import validate_workflow_plan
from novelvideo.freezone.workflow_schema import workflow_intent_json_schema, workflow_plan_json_schema


def plan():
    return {
        'schema_version': 'freezone_workflow_plan.v1',
        'skill': {'id': 'reference-video-remix', 'version': '2.0.0'},
        'external_inputs': [{'id': 'analysis', 'node_id': 'existing-analysis', 'media_kind': 'reference_analysis'}],
        'nodes': [
            {'id': 'plan', 'node_type': 'scriptNode', 'data': {'remixRole': 'production_plan', 'processingAction': 'generate_production_plan_draft', 'processingPayload': {'topic': '复刻参考片'}, 'remixNodeRevision': 1}},
            {'id': 'compose', 'node_type': 'videoComposeNode', 'data': {'processingAction': 'compile_production_plan', 'remixNodeRevision': 1}},
        ],
        'edges': [
            {'source': 'analysis', 'target': 'plan', 'link_type': 'media_input_for'},
            {'source': 'plan', 'target': 'compose', 'link_type': 'composition_input_for'},
        ],
    }


def canvas():
    return {'project_id': 'project-a', 'canvas_id': 'default', 'revision': 3, 'nodes': [
        {'id': 'existing-analysis', 'type': 'videoStoryNode', 'data': {
            'displayName': '原片拆解', 'sourceVideoUrl': '/static/projects/project-a/reference.mp4',
            'rawResult': {'video_story': {'summary': '先提问，再反转'}},
        }},
    ]}


def test_reference_analysis_builds_native_plan_without_copy_or_premature_compose():
    payload = plan()
    assert validate_workflow_plan(payload)['ok']
    assert not list(Draft202012Validator(workflow_plan_json_schema()).iter_errors(payload))
    intent = {'schema_version': 'freezone_workflow_intent.v1', 'skill_id': 'reference-video-remix', 'user_goal': '复刻', 'external_inputs': payload['external_inputs']}
    assert not list(Draft202012Validator(workflow_intent_json_schema()).iter_errors(intent))
    result = build_workflow_graph_commands({'plan': payload, 'run_after_create': True, 'external_media_urls': {'analysis': '/static/projects/project-a/reference.mp4'}})
    assert result['ok'], result
    creates = [c for c in result['commands'] if c['type'] == 'create_node']
    assert len(creates) == 2
    edges = [c for c in result['commands'] if c['type'] == 'create_edge']
    assert edges[0]['source'] == 'existing-analysis'
    assert 'expected_source_image_url' not in edges[0]
    run = next(c for c in result['commands'] if c['type'] == 'run_workflow')
    assert run['node_ids'] == [creates[0]['client_id']]
    assert run['direction'] == 'node'
    assert [command['type'] for command in result['commands']].count('run_workflow') == 1
    assert not any(command['type'] == 'run_node_action' for command in result['commands'])


def test_analysis_snapshot_rejects_changed_content_even_at_same_canvas_revision():
    original = canvas()
    binding = resolve_external_image_inputs(plan(), original, project_id='project-a', canvas_id='default')
    assert binding['analysis']['node_id'] == 'existing-analysis'
    changed = deepcopy(original)
    changed['nodes'][0]['data']['rawResult']['video_story']['summary'] = '不同分析'
    with pytest.raises(ValueError, match='changed'):
        resolve_external_image_inputs(plan(), changed, project_id='project-a', canvas_id='default', expected=binding)


@pytest.mark.parametrize('change', [
    {'rawResult': {}}, {'isAnalyzing': True}, {'analysisError': 'failed'},
    {'sourceVideoUrl': '/static/projects/project-b/reference.mp4'},
])
def test_analysis_requires_completed_in_project_evidence(change):
    snapshot = canvas()
    snapshot['nodes'][0]['data'].update(change)
    with pytest.raises(ValueError):
        resolve_external_image_inputs(plan(), snapshot, project_id='project-a', canvas_id='default')


def test_other_skills_cannot_claim_reference_analysis_input():
    payload = plan()
    payload['skill']['id'] = 'ecommerce-ad'
    assert not validate_workflow_plan(payload)['ok']
    with pytest.raises(ValueError):
        resolve_external_image_inputs(payload, canvas(), project_id='project-a')


def test_analysis_cannot_be_consumed_as_generated_media():
    payload = plan()
    payload['edges'][0]['target'] = 'compose'
    assert not validate_workflow_plan(payload)['ok']


def test_analysis_snapshot_is_forwarded_to_edge_guard():
    binding = resolve_external_image_inputs(plan(), canvas(), project_id='project-a')
    snapshot = binding['analysis']['analysis_snapshot']
    graph = build_workflow_graph_commands({'plan': plan(), 'external_analysis_snapshots': {'analysis': snapshot}})
    assert graph['ok'], graph
    edge = next(c for c in graph['commands'] if c['type'] == 'create_edge')
    assert edge['expected_source_analysis_snapshot'] == snapshot


def test_current_blueprint_reference_is_an_alternative_to_legacy_analysis():
    snapshot = canvas()
    data = snapshot['nodes'][0]['data']
    data['rawResult'] = None
    data['referenceBlueprintRef'] = {'artifactId': 'blueprint-id', 'revision': 2}
    binding = resolve_external_image_inputs(plan(), snapshot, project_id='project-a')
    assert binding['analysis']['analysis_snapshot']['referenceBlueprintRef']['revision'] == 2
    data['blueprintDirty'] = True
    with pytest.raises(ValueError, match='not completed'):
        resolve_external_image_inputs(plan(), snapshot, project_id='project-a')
