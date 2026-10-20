"""Reviewed-plan proposals retain the exact returned commission reference."""
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from test_freezone_plugin import _load_plugin_module
from test_reference_director_api import BASE, confirm_direction, setup_api  # noqa: F401
from novelvideo.freezone.remix.production_review import REVIEW_CATEGORIES


def _snapshot(project_dir):
    return {str(path.relative_to(project_dir)): path.read_bytes()
            for path in project_dir.rglob('*') if path.is_file()}


@pytest.fixture
def reviewed_plan(setup_api):
    client, store, commission, app = setup_api
    commission = confirm_direction(client, commission)
    response = client.post(BASE + '/direct-plan', json={
        'commission_ref': commission['commission_ref'], 'goal': '产品介绍',
        'output': {'width': 1280, 'height': 720, 'fps_num': 24, 'fps_den': 1},
        'command_id': 'confirmation-contract-plan',
    })
    assert response.status_code == 200, response.text
    ref = response.json()['data']['plan_ref']
    response = client.post(BASE + '/production-batch', json={
        'plan_ref': ref, 'command_id': 'confirmation-contract-content', 'commands': [
            {'kind': 'insert_beat', 'target_id': 'intro', 'payload': {'index': 0, 'beat': {
                'id': 'intro', 'purpose': '产品介绍', 'duration_frames': 72, 'turns': [],
            }}},
            {'kind': 'upsert_placement', 'target_id': 'primary', 'payload': {'placement': {
                'id': 'primary', 'kind': 'visual', 'role': 'primary', 'track_id': 'main',
                'start': {'kind': 'beat', 'ref_id': 'intro', 'edge': 'start'},
                'end': {'kind': 'beat', 'ref_id': 'intro', 'edge': 'end'},
            }}},
        ],
    })
    assert response.status_code == 200, response.text
    ref = response.json()['data']['plan_ref']
    response = client.post(BASE + '/plan-review', json={
        'commission_ref': commission['commission_ref'], 'plan_ref': ref,
        'findings': [{'category': category, 'entity_kind': 'plan', 'entity_id': 'plan',
                     'verdict': 'pass', 'reason': '核对已确认目标与实际方案',
                     'evidence': ['方案无台词，产品画面三秒，横屏 24fps 设置符合确认方向']}
                    for category in REVIEW_CATEGORIES],
    })
    assert response.status_code == 200, response.text
    commission = response.json()['data']['commission']
    assert commission['phase'] == 'awaiting_plan_confirmation'
    assert set(commission['plan_ref']) == {'artifact_id', 'revision', 'project_id'}
    return client, store, commission, app


def test_passed_review_prepares_one_internal_plan_decision(reviewed_plan):
    from novelvideo.freezone.remix.reference_director import list_director_artifacts

    _, store, commission, _ = reviewed_plan
    state = list_director_artifacts(
        store, commission['canvas_id'], commission['requester_user_id'], limit=None,
    )
    pending = [row for row in state['decisions'] if row['kind'] == 'plan' and row['status'] == 'pending']
    assert len(pending) == 1
    assert pending[0]['proposal']['plan_ref'] == commission['plan_ref']
    assert pending[0]['commission_ref'] == commission['commission_ref']
    assert not pending[0].get('receipt_id')


def test_manual_plan_prepare_reuses_reviewed_plan_decision(reviewed_plan):
    from novelvideo.freezone.remix.reference_director import list_director_artifacts

    client, store, commission, _ = reviewed_plan
    original = list_director_artifacts(
        store, commission['canvas_id'], commission['requester_user_id'], limit=None,
    )['decisions'][0]
    response = client.post(BASE + '/decisions', json={
        'commission_ref': commission['commission_ref'], 'command_id': 'manual-plan-attempt',
        'kind': 'plan', 'proposal': {'plan_ref': commission['plan_ref'], 'notes': '重复说明'},
        'summary': '同一份待确认作品方案',
    })
    assert response.status_code == 200, response.text
    assert response.json()['data']['decision_ref'] == original['decision_ref']
    pending = list_director_artifacts(
        store, commission['canvas_id'], commission['requester_user_id'], limit=None,
    )['decisions']
    assert len([row for row in pending if row['kind'] == 'plan']) == 1


@pytest.mark.parametrize('change', ['missing_project', 'wrong_project', 'stale_revision'])
def test_inexact_plan_proposal_is_rejected_with_complete_ref_guidance(reviewed_plan, change):
    client, store, commission, _ = reviewed_plan
    proposal = {'plan_ref': deepcopy(commission['plan_ref'])}
    if change == 'missing_project':
        proposal['plan_ref'].pop('project_id')
    elif change == 'wrong_project':
        proposal['plan_ref']['project_id'] = 'UNTRUSTED_PROJECT_IDENTIFIER'
    else:
        proposal['plan_ref']['revision'] -= 1
    before = _snapshot(store.project_dir)
    response = client.post(BASE + '/decisions', json={
        'commission_ref': commission['commission_ref'], 'command_id': 'wrong-plan-ref',
        'kind': 'plan', 'proposal': proposal, 'summary': '待确认实际审核方案',
    })
    assert response.status_code == 422, response.text
    assert _snapshot(store.project_dir) == before
    error = response.json()['detail']
    assert 'proposal must match reviewed plan' in error
    assert 'commission.plan_ref' in error and 'project_id' in error and 'verbatim' in error
    assert 'UNTRUSTED_PROJECT_IDENTIFIER' not in error


def test_complete_returned_plan_ref_produces_pending_decision_without_auto_confirmation(reviewed_plan):
    from novelvideo.api.auth import get_api_user

    client, store, commission, app = reviewed_plan
    proposal = {'plan_ref': deepcopy(commission['plan_ref']),
                'notes': {'creative_summary': '真实产品画面', 'unchanged_extra_context': ['one', 'two']}}
    response = client.post(BASE + '/decisions', json={
        'commission_ref': commission['commission_ref'], 'command_id': 'complete-plan-ref',
        'kind': 'plan', 'proposal': proposal, 'summary': '用户确认前保持待确认',
    })
    assert response.status_code == 200, response.text
    decision = response.json()['data']
    assert decision['decision']['proposal']['plan_ref'] == proposal['plan_ref']
    assert decision['decision']['status'] == 'pending'
    assert 'receipt_id' not in decision['decision']
    current = store.read_latest(commission['commission_ref']['artifact_id'])
    assert current['revision'] == commission['commission_ref']['revision']
    assert current['payload']['phase'] == 'awaiting_plan_confirmation'
    app.dependency_overrides[get_api_user] = lambda: {
        'credential_kind': 'agent_session', 'username': 'alice', 'user_id': 'u', 'role': 'owner',
    }
    before = _snapshot(store.project_dir)
    confirm_url = BASE + '/decisions/' + decision['decision_ref']['artifact_id'] + '/confirm'
    assert client.post(confirm_url).status_code == 403
    assert _snapshot(store.project_dir) == before
    app.dependency_overrides[get_api_user] = lambda: {'username': 'alice', 'user_id': 'u', 'role': 'owner'}
    confirmed = client.post(confirm_url)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()['data']['commission']['phase'] == 'preparing_materials'


def test_plan_proposal_keeps_commission_revision_guard(reviewed_plan):
    client, store, commission, _ = reviewed_plan
    stale = {**commission['commission_ref'], 'revision': commission['commission_ref']['revision'] - 1}
    before = _snapshot(store.project_dir)
    response = client.post(BASE + '/decisions', json={
        'commission_ref': stale, 'command_id': 'stale-commission', 'kind': 'plan',
        'proposal': {'plan_ref': commission['plan_ref']}, 'summary': '旧状态不得确认',
    })
    assert response.status_code == 422, response.text
    assert 'commission changed before preparing decision' in response.text
    assert _snapshot(store.project_dir) == before


def test_plan_proposal_keeps_review_phase_guard(setup_api):
    client, store, commission, _ = setup_api
    before = _snapshot(store.project_dir)
    response = client.post(BASE + '/decisions', json={
        'commission_ref': commission['commission_ref'], 'command_id': 'unreviewed-plan',
        'kind': 'plan', 'proposal': {}, 'summary': '没有审核的方案不得确认',
    })
    assert response.status_code == 422, response.text
    assert 'plan must be reviewed before user confirmation' in response.text
    assert _snapshot(store.project_dir) == before


def test_early_plan_proposal_returns_scoped_review_prerequisite(setup_api):
    from test_reference_director_api import confirm_direction
    from novelvideo.freezone.remix.reference_commission import advance_reference_commission

    client, store, commission, _ = setup_api
    commission = confirm_direction(client, commission)
    plan = client.post(BASE + '/direct-plan', json={
        'commission_ref': commission['commission_ref'], 'goal': '产品介绍',
        'output': {'width': 1280, 'height': 720, 'fps_num': 24, 'fps_den': 1},
        'command_id': 'early-plan-blocker',
    }).json()['data']
    commission = advance_reference_commission(store, commission['commission_ref'],
        {'type': 'plan_ready', 'plan_ref': plan['plan_ref']},
        commission['commission_ref']['revision'])
    response = client.post(BASE + '/decisions', json={
        'commission_ref': commission['commission_ref'], 'command_id': 'too-early',
        'kind': 'plan', 'proposal': {'audit_notes': '尚未审核'}, 'summary': '方案审核',
    })
    assert response.status_code == 422
    detail = response.json()['detail']
    assert detail['error'] == 'plan must be reviewed before user confirmation'
    assert detail['blocker'] == {
        'code': 'plan_review_required', 'project_id': store.project_id,
        'canvas_id': commission['canvas_id'],
        'requester_user_id': commission['requester_user_id'],
        'commission_id': commission['commission_ref']['artifact_id'],
        'plan_ref': commission['plan_ref'],
        'direction_revision': commission['direction_revision'],
        'next_tool': 'freezone_review_production_plan',
    }


def test_plan_decision_metadata_explains_nested_complete_ref_without_changing_material_refs():
    plugin = _load_plugin_module()
    schema = next(schema for name, schema, _ in plugin.TOOLS if name == 'freezone_prepare_reference_decision')
    description = schema['description']
    proposal = schema['parameters']['properties']['proposal']
    assert 'automatically registers' in description
    assert 'ordinary chat' in description
    assert 'without manually preparing another plan decision' in description
    assert 'project_id' in proposal.get('description', '')
    assert {key: value for key, value in proposal.items() if key != 'description'} == {'type': 'object'}
    material = next(schema for name, schema, _ in plugin.TOOLS if name == 'freezone_prepare_material_work')
    assert set(material['parameters']['properties']['plan_ref']['properties']) == {'artifact_id', 'revision'}
    assert 'two-key' in material['description']


@pytest.mark.parametrize('kind', ['plan', 'material', 'composition'])
def test_portable_decision_handler_forwards_general_proposal_without_repair(monkeypatch, kind):
    plugin = _load_plugin_module()
    monkeypatch.setenv('DRAMACLAW_PROJECT_ID', 'p')
    monkeypatch.setenv('DRAMACLAW_CANVAS_ID', 'c')
    captured = []
    monkeypatch.setattr(plugin, '_request', lambda method, path, **kwargs: captured.append(kwargs['body']) or {
        'ok': True, 'data': {'decision_ref': {'artifact_id': 'decision', 'revision': 1}, 'decision': {}},
    })
    schema, handler = next((schema, handler) for name, schema, handler in plugin.TOOLS
                           if name == 'freezone_prepare_reference_decision')
    args = {'commission_ref': {'artifact_id': 'commission', 'revision': 1, 'project_id': 'p'},
            'command_id': 'forward-only', 'kind': kind, 'summary': '实际方案',
            'proposal': {'plan_ref': {'artifact_id': 'plan', 'revision': 2},
                         'general_context': {'preserve': ['问答'], 'arbitrary_fields_remain_allowed': True}}}
    before = deepcopy(args)
    Draft202012Validator(schema['parameters']).validate(args)
    assert handler(args)['ok'] is True
    assert captured == [before]
    assert args == before
    assert 'project_id' not in captured[0]['proposal']['plan_ref']


def test_mirrored_guide_uses_ordinary_plan_confirmation_after_shared_review():
    root = Path(__file__).resolve().parents[1]
    paths = [root / name / 'references/reference-video-remix.md' for name in (
        'src/novelvideo/agent_skills/dramaclaw-workflows',
        'agent-kit/skills/dramaclaw-workflows', '.hermes/skills/workflows',
    )]
    assert len({path.read_bytes() for path in paths}) == 1
    guide = paths[0].read_text()
    section = guide.split('Fix changes_required findings', 1)[1].split('Plan how media is made', 1)[0]
    assert 'registers the exact pending plan decision internally' in section
    assert 'ordinary chat' in section
    assert 'Do not call `freezone_prepare_reference_decision`' in section
    assert 'authenticated user confirms' in section


def test_explicit_reference_plan_confirmation_tool_has_only_exact_refs(monkeypatch):
    plugin = _load_plugin_module()
    schema, handler = next((schema, handler) for name, schema, handler in plugin.TOOLS
                           if name == 'freezone_confirm_reference_plan')
    properties = schema['parameters']['properties']
    assert set(properties) >= {'project_id', 'canvas_id', 'commission_ref', 'plan_ref'}
    assert 'intent' not in properties
    assert 'approved' not in properties
    assert schema['parameters']['required'] == ['commission_ref', 'plan_ref']
    monkeypatch.setenv('DRAMACLAW_PROJECT_ID', 'p')
    monkeypatch.setenv('DRAMACLAW_CANVAS_ID', 'c')
    captured = []
    monkeypatch.setattr(plugin, '_request', lambda method, path, **kwargs: captured.append((method, path, kwargs)) or {
        'ok': True,
        'data': {'commission': {'commission_ref': {'artifact_id': 'c', 'revision': 2}},
                 'decision_ref': {'artifact_id': 'd', 'revision': 2}, 'message_id': 'chat-message:x'},
    })
    result = handler({'commission_ref': {'artifact_id': 'c', 'revision': 1},
                      'plan_ref': {'artifact_id': 'p', 'revision': 4}})
    assert result['ok'] is True
    assert captured[0][0] == 'POST'
    assert captured[0][1].endswith('/director/decisions/confirm-reference-plan')
    assert captured[0][2]['query'] == {'agent_id': 'main'}
    assert captured[0][2]['body'] == {
        'commission_ref': {'artifact_id': 'c', 'revision': 1},
        'plan_ref': {'artifact_id': 'p', 'revision': 4},
    }
