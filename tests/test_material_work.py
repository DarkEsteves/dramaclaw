from pathlib import Path
import hashlib

import pytest
from PIL import Image

from novelvideo.freezone.remix.material_work import (
    MaterialWorkPackageV1, MaterialWorkRequestError, prepare_material_work, ready_material_steps,
    accept_material_candidate, invalidate_material_step, revise_material_strategy,
)


def test_invalid_material_request_has_bounded_agent_diagnostics_and_no_artifact(tmp_path):
    store, ref, _ = setup_work(tmp_path)
    invalid = [{"id": "image", "label": "产品图", "media_kind": "image",
                "source": "generate", "recipe_id": "general-image", "prompt": "产品特写",
                "generation_inputs": {"dialogue": "不应由请求提供"}}]

    with pytest.raises(MaterialWorkRequestError) as exc_info:
        prepare_material_work(
            store, ref,
            [{"id": "final", "placement_ids": ["picture"], "description": "产品特写",
              "media_kind": "image", "final_step_id": "image"}],
            invalid, "invalid-material-request",
        )

    details = exc_info.value.details
    assert details["code"] == "material_work_request_invalid"
    assert details["phase"] == "material_plan_drafting"
    assert details["retryable"] is False
    assert details["next_action"] == "correct_material_request"
    assert details["side_effects"] == "none"
    assert details["errors"]
    artifact_id = hashlib.sha256(b"material-work:invalid-material-request").hexdigest()[:32]
    with pytest.raises(FileNotFoundError):
        store.read_latest(artifact_id)
from novelvideo.freezone.remix.store import RemixArtifactStore
from test_production_audio_sources import planned_video_dialogue


def plan_payload():
    return {"schema_version": 2, "goal": "产品介绍", "output": {"width": 1280, "height": 720, "fps_num": 24, "fps_den": 1},
            "beats": [{"id": "opening", "purpose": "提问", "duration_frames": 72, "turns": []}],
            "resources": [], "placements": [{"id": "picture", "kind": "visual", "role": "primary", "track_id": "main",
            "start": {"kind": "beat", "ref_id": "opening", "edge": "start"},
            "end": {"kind": "beat", "ref_id": "opening", "edge": "end"}}], "provenance": {}}


def setup_work(tmp_path: Path):
    store = RemixArtifactStore(tmp_path, project_id="project", created_by="alice")
    plan = store.create("production_plan.v2", plan_payload(), [])
    ref = {"artifact_id": plan["id"], "revision": 1}
    requirements = [{"id": "final", "placement_ids": ["picture"], "description": "产品特写", "media_kind": "video", "final_step_id": "video"}]
    steps = [{"id": "image", "label": "产品图", "media_kind": "image", "source": "generate", "recipe_id": "general-image", "prompt": "产品特写"},
             {"id": "video", "label": "产品视频", "media_kind": "video", "source": "generate", "recipe_id": "general-video", "prompt": "缓慢推进", "depends_on": ["image"],
              "references": [{"step_id": "image", "role": "first_frame"}]}]
    work = prepare_material_work(store, ref, requirements, steps, "prepare-1")
    return store, ref, work


def test_shared_intermediate_dependency_and_idempotency(tmp_path):
    store, ref, result = setup_work(tmp_path)
    work = store.read(result["work_ref"]["artifact_id"], 1)["payload"]
    assert len(work["requirements"]) == 1 and len(work["steps"]) == 2
    assert ready_material_steps(work) == ["image"]
    replay = prepare_material_work(store, ref, work["requirements"], work["steps"], "prepare-1")
    assert replay["work_ref"] == result["work_ref"]


def test_ready_video_steps_keep_distinct_generation_durations():
    from novelvideo.freezone.remix.material_work import material_workflow_intent

    work = {'requirements': [], 'steps': [
        {'id': f'clip-{index}', 'label': f'clip {index}', 'media_kind': 'video',
         'source': 'generate', 'recipe_id': 'general-video', 'prompt': 'synthetic scene',
         'depends_on': [], 'references': [], 'status': 'planned',
         'generation_inputs': {'model': 'fixture-model', 'durationSec': duration}}
        for index, duration in enumerate([4, 6, 5])
    ]}
    intent = material_workflow_intent(work)
    assert [item['duration_seconds'] for item in intent['items']] == [4, 6, 5]
    assert 'video_duration_seconds' not in intent['inputs']


def test_program_length_in_material_prompt_does_not_set_source_clip_duration():
    from novelvideo.freezone.remix.material_work import material_workflow_intent
    from novelvideo.freezone.agent_workflows.catalog import compile_workflow_intent

    work = {'requirements': [], 'steps': [{
        'id': 'clip', 'label': '画面', 'media_kind': 'video', 'source': 'generate',
        'recipe_id': 'general-video', 'prompt': '成片约18秒，这个镜头展示开场',
        'depends_on': [], 'references': [], 'status': 'planned',
        'generation_inputs': {'model': 'fixture-model'},
    }]}
    intent = material_workflow_intent(work)
    assert 'step_id' not in intent['items'][0]
    compiled = compile_workflow_intent(intent)
    assert compiled['ok'] is True
    clip = next(node for node in compiled['plan']['nodes'] if node['node_type'] == 'videoNode')
    assert 'durationSec' not in clip['data']


def test_old_shared_clip_preference_does_not_set_new_material_step_duration():
    from novelvideo.freezone.remix.material_work import material_workflow_intent
    from novelvideo.freezone.agent_workflows.catalog import compile_workflow_intent

    work = {'requirements': [], 'steps': [{
        'id': 'clip', 'label': '画面', 'media_kind': 'video', 'source': 'generate',
        'recipe_id': 'general-video', 'prompt': '根据当前方案选择生成长度',
        'depends_on': [], 'references': [], 'status': 'planned',
        'generation_inputs': {'model': 'fixture-model'},
    }]}
    intent = material_workflow_intent(work)
    intent['inputs']['video_duration_seconds'] = 5
    compiled = compile_workflow_intent(intent)
    assert compiled['ok'] is True
    clip = next(node for node in compiled['plan']['nodes'] if node['node_type'] == 'videoNode')
    assert 'durationSec' not in clip['data']


def test_exact_plan_backfill_keeps_material_duration_independent():
    from novelvideo.freezone.agent_workflows.catalog import _backfill_plan_runtime_fields

    nodes = [
        {'id': 'material', 'node_type': 'videoNode', 'data': {
            'workflowCatalog': {'skillId': 'reference-video-remix',
                                'stepId': 'material', 'recipeId': 'general-video'}}},
        {'id': 'ordinary', 'node_type': 'videoNode', 'data': {}},
    ]
    _backfill_plan_runtime_fields(nodes, {'video_duration_seconds': 5})
    assert 'durationSec' not in nodes[0]['data']
    assert nodes[1]['data']['durationSec'] == 5


def test_legacy_work_without_empty_preservation_field_keeps_plan_fingerprint(tmp_path):
    from copy import deepcopy
    from novelvideo.freezone.remix.material_work import validate_material_work_lineage

    store, _, result = setup_work(tmp_path)
    saved = store.read(result['work_ref']['artifact_id'], result['work_ref']['revision'])
    legacy = deepcopy(saved['payload'])
    for requirement in legacy['requirements']:
        requirement.pop('preservation_bindings', None)
    validate_material_work_lineage(store, legacy, saved['upstream_refs'])
    for requirement in legacy['requirements']:
        requirement['preservation_bindings'] = []
    validate_material_work_lineage(store, legacy, saved['upstream_refs'])


def test_cycle_and_wrong_target_rejected(tmp_path):
    store, ref, result = setup_work(tmp_path)
    work = store.read(result["work_ref"]["artifact_id"], 1)["payload"]
    work["steps"][0]["depends_on"] = ["video"]
    with pytest.raises(ValueError, match="cycle"):
        MaterialWorkPackageV1.model_validate(work)
    with pytest.raises(ValueError, match="placement"):
        prepare_material_work(store, ref, [{"id": "bad", "placement_ids": ["foreign"], "description": "missing", "media_kind": "image", "final_step_id": "image"}], work["steps"][1:], "prepare-2")


def test_candidate_requires_real_resource_and_content_review(tmp_path):
    store, _, result = setup_work(tmp_path)
    path = tmp_path / "asset.png"
    path.write_bytes(b"actual image")
    resource = {"id": "asset", "node_id": "image-node", "media_kind": "image", "media_path": "asset.png", "media_hash": hashlib.sha256(path.read_bytes()).hexdigest(), "duration_us": 0}
    with pytest.raises(ValueError, match="review"):
        accept_material_candidate(store, result["work_ref"], "image", resource, None, "accept-1")
    review = store.create("material_review.v1", {"resource": resource, "step_id": "image", "work_ref": result["work_ref"],
        "technical": {"status": "passed", "checks": ["project_file_hash"]},
        "content": {"status": "unavailable", "findings": ["未查看"]}, "actor_kind": "agent", "evidence_refs": []}, [result["work_ref"]])
    # Unobserved content stays advisory: a technical pass can be accepted and
    # bound, and the look is confirmed later instead of blocking the pipeline.
    accepted = accept_material_candidate(store, result["work_ref"], "image", resource, {"artifact_id": review["id"], "revision": 1}, "accept-1")
    assert accepted["ready_step_ids"] == ["video"]


def test_invalidating_one_branch_keeps_independent_steps():
    work = {"steps": [{"id": "person", "depends_on": [], "status": "accepted"},
                      {"id": "talk", "depends_on": ["person"], "status": "accepted"},
                      {"id": "product", "depends_on": [], "status": "accepted"}]}
    updated = invalidate_material_step(work, "person", "人物变化")
    assert [step["status"] for step in updated["steps"]] == ["stale", "stale", "accepted"]


def test_revise_strategy_changes_source_and_preserves_independent_acceptance(tmp_path):
    store, _, prepared = setup_work(tmp_path)
    Image.new('RGB', (8, 8), 'red').save(tmp_path / 'frame.png')
    ref = prepared['work_ref']
    original = store.read(ref['artifact_id'], 1)
    work = original['payload']
    work['steps'].append({'id': 'independent', 'label': 'Logo', 'media_kind': 'image',
        'source': 'existing', 'node_id': 'logo-node', 'status': 'accepted', 'accepted_resource': {
            'id': 'logo', 'node_id': 'logo-node', 'media_kind': 'image',
            'media_path': 'logo.png', 'media_hash': 'a' * 64, 'duration_us': 0},
        'review_ref': {'artifact_id': 'logo-review', 'revision': 1},
        'candidates': [{'note': 'already accepted'}]})
    saved = store.revise(ref['artifact_id'], 1, work, original['upstream_refs'])
    ref = {'artifact_id': saved['id'], 'revision': saved['revision']}
    replacement = {'id': 'image', 'label': 'Original frame', 'media_kind': 'image',
                   'source': 'existing', 'node_id': 'frame-node'}
    result = revise_material_strategy(store, ref, [replacement], 'strategy-1',
        canvas={'nodes': [{'id': 'frame-node', 'type': 'uploadNode',
                           'data': {'imageUrl': '/static/projects/project/frame.png'}}]})
    steps = {row['id']: row for row in result['work']['steps']}
    assert result['changed_step_ids'] == ['image']
    assert result['affected_step_ids'] == ['image', 'video']
    assert steps['image']['source'] == 'existing' and steps['image']['node_id'] == 'frame-node'
    assert steps['image']['status'] == 'planned' and steps['video']['status'] == 'stale'
    assert steps['independent'] == saved['payload']['steps'][-1]
    assert result == revise_material_strategy(store, ref, [replacement], 'strategy-1',
        canvas={'nodes': [{'id': 'frame-node', 'type': 'uploadNode',
                           'data': {'imageUrl': '/static/projects/project/frame.png'}}]})


def test_revise_strategy_is_atomic_and_revision_guarded(tmp_path):
    store, _, prepared = setup_work(tmp_path)
    Image.new('RGB', (8, 8), 'red').save(tmp_path / 'frame.png')
    ref = prepared['work_ref']
    canvas = {'nodes': [{'id': 'frame-node', 'type': 'uploadNode',
                         'data': {'imageUrl': '/static/projects/project/frame.png'}}]}
    first = {'id': 'image', 'label': 'Original frame', 'media_kind': 'image',
             'source': 'existing', 'node_id': 'frame-node'}
    bad = {'id': 'video', 'label': 'Other clip', 'media_kind': 'video',
           'source': 'existing', 'node_id': 'missing'}
    with pytest.raises(ValueError, match='current canvas'):
        revise_material_strategy(store, ref, [first, bad], 'invalid-batch', canvas=canvas)
    assert store.read_latest(ref['artifact_id'])['revision'] == 1
    done = revise_material_strategy(store, ref, [first], 'valid-batch', canvas=canvas)
    assert done['work_ref']['revision'] == 2
    with pytest.raises(ValueError, match='reused'):
        revise_material_strategy(store, ref, [bad], 'valid-batch', canvas=canvas)
    with pytest.raises(ValueError, match='cannot claim'):
        revise_material_strategy(store, done['work_ref'], [{'id': 'video', 'label': 'New video',
            'media_kind': 'video', 'source': 'generate', 'recipe_id': 'general-video',
            'prompt': 'New motion', 'node_id': 'fake-output'}], 'fake-output', canvas=canvas)
    assert store.read_latest(ref['artifact_id'])['revision'] == 2
    with pytest.raises(Exception) as conflict:
        revise_material_strategy(store, ref, [{'id': 'image', 'label': 'Alternative',
            'media_kind': 'image', 'source': 'generate', 'recipe_id': 'general-image',
            'prompt': 'New image'}], 'conflict-batch', canvas=canvas)
    assert getattr(conflict.value, 'code', None) == 'artifact_revision_conflict'
    assert conflict.value.latest_ref == done['work_ref']
    assert conflict.value.changed_step_ids == ['image']


@pytest.mark.asyncio
async def test_revise_strategy_route_reports_structured_conflict(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fastapi import HTTPException
    from novelvideo.api.routes import reference_material as routes, reference_director
    store, _, prepared = setup_work(tmp_path)
    Image.new('RGB', (8, 8), 'red').save(tmp_path / 'frame.png')
    canvas = {'nodes': [{'id': 'frame-node', 'type': 'uploadNode',
                         'data': {'imageUrl': '/static/projects/project/frame.png'}}]}
    async def context(*args):
        return SimpleNamespace(state_dir=tmp_path), canvas, store
    async def plan_scope(*args):
        pass
    async def notify(*args, **kwargs):
        pass
    monkeypatch.setattr(reference_director, '_context', context)
    monkeypatch.setattr(reference_director, '_plan_in_canvas', plan_scope)
    monkeypatch.setattr(routes, '_notify_material', notify)
    replacement = {'id': 'image', 'label': 'Original frame', 'media_kind': 'image',
                   'source': 'existing', 'node_id': 'frame-node'}
    body = routes.StrategyBody(work_ref=prepared['work_ref'], replacements=[replacement], command_id='route-1')
    result = await routes.material_strategy('project', 'canvas', body, {})
    assert result['data']['changed_step_ids'] == ['image']
    with pytest.raises(HTTPException) as conflict:
        await routes.material_strategy('project', 'canvas', routes.StrategyBody(
            work_ref=prepared['work_ref'], replacements=[{'id': 'image', 'label': 'Other',
                'media_kind': 'image', 'source': 'generate', 'recipe_id': 'general-image',
                'prompt': 'Other'}], command_id='route-2'), {})
    assert conflict.value.status_code == 409
    assert conflict.value.detail['code'] == 'artifact_revision_conflict'
    assert conflict.value.detail['latest_ref'] == result['data']['work_ref']


def test_three_final_videos_share_intermediate_without_marking_final_ready(tmp_path):
    store, plan_ref, result = setup_work(tmp_path)
    plan = store.read(plan_ref['artifact_id'], plan_ref['revision'])
    plan['payload']['placements'] = [
        {**plan['payload']['placements'][0], 'id': f'picture_{index}'}
        for index in range(3)
    ]
    saved_plan = store.revise(plan['id'], plan['revision'], plan['payload'], [])
    plan_ref = {'artifact_id': saved_plan['id'], 'revision': saved_plan['revision']}
    work = store.read(result['work_ref']['artifact_id'],1)['payload']
    requirements = []
    steps = [work['steps'][0]]
    for index in range(3):
        step = {**work['steps'][1], 'id':f'video_{index}'}
        steps.append(step)
        requirements.append({**work['requirements'][0], 'id':f'final_{index}',
                             'placement_ids':[f'picture_{index}'], 'final_step_id':step['id']})
    result = prepare_material_work(store,plan_ref,requirements,steps,'three-finals')
    payload = store.read(result['work_ref']['artifact_id'],1)['payload']
    assert len(payload['requirements']) == 3 and len(payload['steps']) == 4
    assert ready_material_steps(payload) == ['image']
    assert all(r['final_step_id'] != 'image' for r in payload['requirements'])


def test_one_video_step_owns_two_planned_speech_turns(tmp_path):
    from novelvideo.freezone.remix.material_work import material_workflow_intent

    store = RemixArtifactStore(tmp_path, project_id='project', created_by='alice')
    saved = store.create('production_plan.v2', planned_video_dialogue(), [])
    ref = {'artifact_id': saved['id'], 'revision': saved['revision']}
    requirements = [{'id': 'dialogue-video', 'placement_ids': ['demo-video'],
                     'turn_ids': ['ask', 'answer'], 'description': '两人交谈',
                     'media_kind': 'video', 'final_step_id': 'clip'}]
    steps = [{'id': 'clip', 'label': '交谈视频', 'media_kind': 'video', 'source': 'generate',
              'recipe_id': 'general-video', 'prompt': '两人交谈',
              'dialogue_turn_ids': ['ask', 'answer']}]
    result = prepare_material_work(store, ref, requirements, steps, 'one-voiced-video')
    work = store.read(result['work_ref']['artifact_id'], 1)['payload']
    assert len(work['steps']) == 1
    assert material_workflow_intent(work, store=store)['items'][0]['requires_generated_audio'] is True
    assert material_workflow_intent(work, store=store)['inputs']['video_generate_audio'] is True


def test_dialogue_requirement_must_use_its_planned_final_source(tmp_path):
    store = RemixArtifactStore(tmp_path, project_id='project', created_by='alice')
    plan = planned_video_dialogue()
    plan['placements'].append({**plan['placements'][0], 'id': 'other-video', 'source_audio': None})
    saved = store.create('production_plan.v2', plan, [])
    ref = {'artifact_id': saved['id'], 'revision': saved['revision']}
    requirements = [{'id': 'wrong', 'placement_ids': ['other-video'], 'turn_ids': ['ask'],
                     'description': '配错画面', 'media_kind': 'video', 'final_step_id': 'other'}]
    steps = [{'id': 'other', 'label': '其他画面', 'media_kind': 'video', 'source': 'generate',
              'recipe_id': 'general-video', 'prompt': '其他画面', 'dialogue_turn_ids': ['ask']}]
    with pytest.raises(ValueError, match='ask.*demo-video.*other') as conflict:
        prepare_material_work(store, ref, requirements, steps, 'wrong-source')
    assert conflict.value.details == {'code': 'material_speech_source_conflict',
                                      'placement_id': 'demo-video', 'turn_ids': ['ask'],
                                      'step_ids': ['other'], 'message': str(conflict.value)}


def test_generated_video_cannot_omit_turns_owned_by_its_planned_source(tmp_path):
    store = RemixArtifactStore(tmp_path, project_id='project', created_by='alice')
    saved = store.create('production_plan.v2', planned_video_dialogue(), [])
    ref = {'artifact_id': saved['id'], 'revision': 1}
    requirements = [{'id': 'video', 'placement_ids': ['demo-video'],
                     'description': '两人交谈', 'media_kind': 'video', 'final_step_id': 'clip'}]
    steps = [{'id': 'clip', 'label': '交谈视频', 'media_kind': 'video', 'source': 'generate',
              'recipe_id': 'general-video', 'prompt': '两人交谈'}]
    with pytest.raises(ValueError, match='demo-video.*ask.*answer.*clip'):
        prepare_material_work(store, ref, requirements, steps, 'missing-dialogue')


def test_step_dialogue_mismatch_names_the_field_and_allowed_turns(tmp_path):
    """A generate step cannot adopt a turn no requirement hands it.

    The old message ("dialogue turns differ from its requirements") carried no
    step, turn or field, so the Agent re-submitted the same steps unchanged.
    """
    store = RemixArtifactStore(tmp_path, project_id='project', created_by='alice')
    plan = planned_video_dialogue()
    plan['beats'][0]['turns'].append({
        'id': 'aside', 'speaker': '丙', 'text': '顺便看一下包装。',
        'speech_placement_id': 'other-video', 'timing_basis': 'estimated'})
    plan['placements'].append({**plan['placements'][0], 'id': 'other-video'})
    saved = store.create('production_plan.v2', plan, [])
    ref = {'artifact_id': saved['id'], 'revision': saved['revision']}
    requirements = [{'id': 'video', 'placement_ids': ['demo-video'],
                     'turn_ids': ['ask', 'answer'], 'description': '两人交谈',
                     'media_kind': 'video', 'final_step_id': 'clip'}]
    steps = [{'id': 'clip', 'label': '交谈视频', 'media_kind': 'video', 'source': 'generate',
              'recipe_id': 'general-video', 'prompt': '两人交谈',
              'dialogue_turn_ids': ['ask', 'answer', 'aside']}]

    with pytest.raises(MaterialWorkRequestError) as exc_info:
        prepare_material_work(store, ref, requirements, steps, 'stolen-turn')

    details = exc_info.value.details
    assert details['code'] == 'material_step_dialogue_turns_mismatch'
    assert details['next_action'] == 'correct_material_step_dialogue_turns'
    assert details['errors'][0]['path'] == 'steps[clip].dialogue_turn_ids'
    assert 'aside' in details['message']
    assert 'allowed here: ask, answer' in details['errors'][0]['message']


def test_reference_shot_defaults_to_a_mode_that_consumes_media(tmp_path):
    from novelvideo.freezone.remix.material_work import material_workflow_intent
    from novelvideo.freezone.agent_workflows.catalog import compile_workflow_intent
    from novelvideo.freezone.workflow_preflight import evaluate_workflow_preflight

    store = RemixArtifactStore(tmp_path, project_id='project', created_by='alice')
    saved = store.create('production_plan.v2', planned_video_dialogue(), [])
    ref = {'artifact_id': saved['id'], 'revision': saved['revision']}
    requirements = [{'id': 'dialogue-video', 'placement_ids': ['demo-video'],
                     'turn_ids': ['ask', 'answer'], 'description': '两人交谈',
                     'media_kind': 'video', 'final_step_id': 'clip'}]
    steps = [
        {'id': 'look', 'label': '场景参考', 'media_kind': 'image', 'source': 'generate',
         'recipe_id': 'general-image', 'prompt': '场景'},
        {'id': 'clip', 'label': '交谈视频', 'media_kind': 'video', 'source': 'generate',
         'recipe_id': 'general-video', 'prompt': '两人交谈',
         'dialogue_turn_ids': ['ask', 'answer'],
         'references': [{'step_id': 'look', 'role': 'setting'}],
         'generation_inputs': {'durationSec': 5}},
    ]
    result = prepare_material_work(store, ref, requirements, steps, 'reference-shot')
    work = store.read(result['work_ref']['artifact_id'], 1)['payload']
    intent = material_workflow_intent(work, store=store)
    assert [item['id'] for item in intent['items']] == ['look']
    work['steps'][0].update(status='accepted', node_id='look-node', accepted_resource={
        'node_id': 'look-node', 'media_kind': 'image'})
    intent = material_workflow_intent(work, store=store)
    # The accepted first-stage image is consumed by the second-stage shot.
    assert intent['inputs']['video_generation_mode'] == 'imageReference'
    assert intent['inputs']['video_generate_audio'] is True
    compiled = compile_workflow_intent(intent)
    assert compiled['ok'] is True
    preflight = evaluate_workflow_preflight(
        compiled, model_responses={}, limits={}, runtime_available=False)
    assert [blocker['code'] for blocker in preflight['blockers']] == []


def test_revise_strategy_names_the_fields_it_rejects(tmp_path):
    store, _, prepared = setup_work(tmp_path)
    ref = prepared['work_ref']
    saved = store.read(ref['artifact_id'], ref['revision'])['payload']
    image_step = next(step for step in saved['steps'] if step['media_kind'] == 'image')
    with pytest.raises(ValueError, match='remove candidates, status'):
        revise_material_strategy(store, ref, [{**image_step, 'status': 'planned'}],
                                 'server-field', canvas={})
    with pytest.raises(ValueError, match='audio_v1.*existing steps'):
        revise_material_strategy(store, ref, [{'id': 'audio_v1', 'label': '旁白',
                                               'media_kind': 'audio', 'source': 'generate',
                                               'recipe_id': 'general-audio', 'prompt': '旁白'}],
                                 'invented-step', canvas={})


def test_explicit_silent_video_choice_stays_a_native_preflight_conflict(tmp_path):
    from novelvideo.freezone.remix.material_work import material_workflow_intent
    from novelvideo.freezone.agent_workflows.catalog import compile_workflow_intent
    from novelvideo.freezone.workflow_preflight import evaluate_workflow_preflight

    store = RemixArtifactStore(tmp_path, project_id='project', created_by='alice')
    saved = store.create('production_plan.v2', planned_video_dialogue(), [])
    ref = {'artifact_id': saved['id'], 'revision': saved['revision']}
    requirements = [{'id': 'dialogue-video', 'placement_ids': ['demo-video'],
                     'turn_ids': ['ask', 'answer'], 'description': '两人交谈',
                     'media_kind': 'video', 'final_step_id': 'clip'}]
    steps = [{'id': 'clip', 'label': '交谈视频', 'media_kind': 'video', 'source': 'generate',
              'recipe_id': 'general-video', 'prompt': '两人交谈',
              'dialogue_turn_ids': ['ask', 'answer'],
              'generation_inputs': {'generateAudio': False}}]
    result = prepare_material_work(store, ref, requirements, steps, 'explicit-silent')
    work = store.read(result['work_ref']['artifact_id'], 1)['payload']
    intent = material_workflow_intent(work, store=store)
    assert intent['items'][0]['requires_generated_audio'] is True
    assert intent['inputs']['video_generate_audio'] is False
    compiled = compile_workflow_intent(intent)
    assert compiled['ok'] is True
    preflight = evaluate_workflow_preflight(compiled, model_responses={}, limits={}, runtime_available=False)
    assert any(blocker['code'] == 'generation_parameter_conflict' for blocker in preflight['blockers'])


def test_source_audio_change_invalidates_saved_material_fingerprint(tmp_path):
    from novelvideo.freezone.remix.material_work import validate_material_work_lineage

    store = RemixArtifactStore(tmp_path, project_id='project', created_by='alice')
    plan = planned_video_dialogue()
    saved = store.create('production_plan.v2', plan, [])
    ref = {'artifact_id': saved['id'], 'revision': 1}
    requirements = [{'id': 'video', 'placement_ids': ['demo-video'],
                     'turn_ids': ['ask', 'answer'], 'description': '两人交谈',
                     'media_kind': 'video', 'final_step_id': 'clip'}]
    steps = [{'id': 'clip', 'label': '交谈视频', 'media_kind': 'video', 'source': 'generate',
              'recipe_id': 'general-video', 'prompt': '两人交谈',
              'dialogue_turn_ids': ['ask', 'answer']}]
    prepared = prepare_material_work(store, ref, requirements, steps, 'fingerprinted-source')
    work = store.read(prepared['work_ref']['artifact_id'], 1)
    changed = {**plan, 'placements': [{**plan['placements'][0], 'source_audio': None}]}
    revised = store.revise(saved['id'], 1, changed, [])
    stale = {**work['payload'], 'plan_ref': {'artifact_id': revised['id'], 'revision': revised['revision']}}
    with pytest.raises(ValueError, match='input differs'):
        validate_material_work_lineage(store, stale, [stale['plan_ref']])


def test_sound_only_video_requires_generated_audio_without_speech_turns(tmp_path):
    from novelvideo.freezone.remix.material_work import material_workflow_intent

    store = RemixArtifactStore(tmp_path, project_id='project', created_by='alice')
    plan = planned_video_dialogue()
    plan['beats'][0]['turns'] = []
    saved = store.create('production_plan.v2', plan, [])
    ref = {'artifact_id': saved['id'], 'revision': 1}
    requirements = [{'id': 'video', 'placement_ids': ['demo-video'],
                     'description': '现场声', 'media_kind': 'video', 'final_step_id': 'clip'}]
    steps = [{'id': 'clip', 'label': '现场视频', 'media_kind': 'video', 'source': 'generate',
              'recipe_id': 'general-video', 'prompt': '门打开伴随声音'}]
    prepared = prepare_material_work(store, ref, requirements, steps, 'sound-only')
    work = store.read(prepared['work_ref']['artifact_id'], 1)['payload']
    intent = material_workflow_intent(work, store=store)
    assert intent['items'][0]['requires_generated_audio'] is True
    assert intent['inputs']['video_generate_audio'] is True
    assert not work['steps'][0]['dialogue_turn_ids']


def test_legacy_supplied_work_fingerprint_detects_speech_owner_removed(tmp_path):
    from copy import deepcopy
    from novelvideo.freezone.remix.material_work import _fingerprint, validate_material_work_lineage

    store = RemixArtifactStore(tmp_path, project_id='project', created_by='alice')
    plan = planned_video_dialogue()
    saved = store.create('production_plan.v2', plan, [])
    requirement = {'id': 'supplied', 'placement_ids': ['demo-video'], 'turn_ids': [],
                   'description': '已有对话视频', 'media_kind': 'video', 'final_step_id': 'clip'}
    old_fingerprint = _fingerprint(store, saved['payload'], [requirement])
    changed = deepcopy(saved['payload'])
    for turn in changed['beats'][0]['turns']:
        turn['speech_placement_id'] = None
    revised = store.revise(saved['id'], 1, changed, [])
    ref = {'artifact_id': revised['id'], 'revision': revised['revision']}
    legacy_work = {'plan_ref': ref, 'requirements': [requirement],
                   'steps': [{'id': 'clip', 'source': 'existing', 'media_kind': 'video',
                              'dialogue_turn_ids': []}],
                   'input_fingerprint': old_fingerprint}
    with pytest.raises(ValueError, match='input differs'):
        validate_material_work_lineage(store, legacy_work, [ref])


@pytest.mark.parametrize('source', ['existing', 'upload'])
def test_supplied_speech_video_requires_turn_coverage_for_candidate_asr(tmp_path, source):
    store = RemixArtifactStore(tmp_path, project_id='project', created_by='alice')
    saved = store.create('production_plan.v2', planned_video_dialogue(), [])
    ref = {'artifact_id': saved['id'], 'revision': 1}
    requirements = [{'id': 'supplied', 'placement_ids': ['demo-video'],
                     'description': '已有对话视频', 'media_kind': 'video', 'final_step_id': 'clip'}]
    steps = [{'id': 'clip', 'label': '对话视频', 'media_kind': 'video', 'source': source}]
    with pytest.raises(ValueError, match='demo-video.*ask.*answer.*clip') as conflict:
        prepare_material_work(store, ref, requirements, steps, f'missing-{source}-turns')
    assert 'generate' not in str(conflict.value)
