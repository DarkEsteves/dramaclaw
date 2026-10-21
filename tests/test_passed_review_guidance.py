"""Saved review guidance never repairs, approves or retries a native accept call."""
from copy import deepcopy
import json
import hashlib
from pathlib import Path

import pytest

from test_freezone_plugin import _load_plugin_module, _restore_tools_registry_modules as _restore_tools_registry_modules
from test_material_review_discovery import fixture as material_fixture
from novelvideo.freezone.remix.material_work import accept_material_candidate


NATIVE_ERROR = 'material technical/content review has not passed'

# Derived once from the captured post-Task28 director tool baseline. These
# checked-in values keep the contract test usable in an ordinary checkout.
DIRECTOR_TOOL_NAMES = (
    'freezone_accept_material_candidate',
    'freezone_associate_material_upload',
    'freezone_confirm_reference_plan',
    'freezone_get_material_work',
    'freezone_get_reference_director_state',
    'freezone_get_reference_evidence',
    'freezone_get_speech_review',
    'freezone_list_reference_media',
    'freezone_prepare_material_stage',
    'freezone_prepare_material_work',
    'freezone_prepare_reference_branches',
    'freezone_prepare_reference_decision',
    'freezone_prepare_speech_correction',
    'freezone_retry_material_step',
    'freezone_review_material_candidate',
    'freezone_review_production_output',
    'freezone_review_production_plan',
    'freezone_revise_material_strategy',
)
# Re-baselined for the deliberate chat-confirmed acceptance parameter
# (freezone_accept_material_candidate.user_confirmed); every other legacy
# parameter contract is unchanged.
DIRECTOR_PARAMETER_SHA256 = '2db7a53ca89cea60985240c0d177f9665cab1ddf4aa712310db416ebba6b5794'


def adapter(monkeypatch, response, *, tool='freezone_accept_material_candidate', packed=False):
    plugin = _load_plugin_module()
    monkeypatch.setenv('DRAMACLAW_PROJECT_ID', 'project')
    monkeypatch.setenv('DRAMACLAW_CANVAS_ID', 'canvas')
    calls = []
    def request(method, path, **kwargs):
        calls.append((method, path, deepcopy(kwargs)))
        return deepcopy(response)
    monkeypatch.setattr(plugin, '_request', request)
    monkeypatch.setattr(plugin, 'tool_result', lambda value: json.dumps(value) if packed else value)
    _, schema, handler = next(entry for entry in plugin.TOOLS if entry[0] == tool)
    return plugin, schema, handler, calls


def arguments():
    return {'work_ref': {'artifact_id': 'saved-work', 'revision': 1}, 'step_id': 'guest-image',
            'resource': {'id': 'actual-resource', 'node_id': 'actual-node', 'media_hash': 'a' * 64},
            'review_ref': {'artifact_id': 'cached-review', 'revision': 2}, 'command_id': 'accept-image'}


@pytest.mark.parametrize('packed', [False, True])
@pytest.mark.parametrize('error_field', ['error', 'detail'])
def test_native_content_rejection_has_bounded_static_saved_review_guidance(tmp_path, monkeypatch, packed, error_field):
    store, ref, _, review = material_fixture(tmp_path)
    saved = store.read(review['review_ref']['artifact_id'], review['review_ref']['revision'])
    payload = deepcopy(saved['payload'])
    payload['content'] = {'status': 'rejected', 'checks': ['visible mismatch'], 'findings': ['wrong product']}
    rejected_review = store.revise(saved['id'], saved['revision'], payload, saved['upstream_refs'])
    with pytest.raises(ValueError, match=NATIVE_ERROR) as rejected:
        accept_material_candidate(store, ref, 'image', review['review']['resource'],
                                  {'artifact_id': rejected_review['id'], 'revision': rejected_review['revision']},
                                  'accept-cached')
    response = {'ok': False, 'status_code': 422, error_field: str(rejected.value)}
    plugin, _, handler, calls = adapter(monkeypatch, response, packed=packed)
    args = arguments()
    original = deepcopy(args)
    native_before = store.read_latest(ref['artifact_id'])
    result = handler(args)
    if packed:
        result = json.loads(result)
    assert result['ok'] is False and result['status'] == 'director_operation_failed'
    assert result['error'] == NATIVE_ERROR
    assert result['project_id'] == 'project' and result['canvas_id'] == 'canvas'
    instruction = result['agent_instruction']
    for required in ('freezone_get_material_work', 'work_ref', 'matches_current_work_revision=true',
                     'technical/content', 'passed', 'review_ref', 'exact resource',
                     'Do not guess revisions', 'another work or review', 'chat', 'reference strategy'):
        assert required in instruction
    assert len(instruction) < 1600
    assert 'cached-review' not in instruction and 'actual-node' not in instruction
    assert calls == [('POST', '/api/v1/projects/project/freezone/canvases/canvas/director/material-accept', {'query': None, 'body': original})]
    assert args == original
    assert store.read_latest(ref['artifact_id']) == native_before


@pytest.mark.parametrize('response', [
    {'ok': False, 'error': 'Invalid agent session', 'status_code': 401},
    {'ok': False, 'error': 'network_error: connection failed'},
    {'ok': False, 'error': 'material work changed; reload current revision'},
    {'ok': False, 'error': 'material changed after review'},
    {'ok': False, 'error': NATIVE_ERROR + '; unknown cause'},
    {'ok': False, 'error': NATIVE_ERROR, 'status_code': 403},
    {'ok': False, 'error': NATIVE_ERROR, 'status_code': 500},
    {'error': NATIVE_ERROR},
    {'ok': 0, 'error': NATIVE_ERROR},
    {'ok': True, 'data': {'work_ref': {'artifact_id': 'saved-work', 'revision': 2}, 'binding_candidates': []}},
])
def test_other_results_preserve_existing_result_without_review_retry_advice(monkeypatch, response):
    original = deepcopy(response)
    _, _, handler, calls = adapter(monkeypatch, response)
    result = handler(arguments())
    assert 'agent_instruction' not in result
    assert result['ok'] is bool(response.get('ok'))
    assert len(calls) == 1
    assert response == original


@pytest.mark.parametrize('tool', ['freezone_review_material_candidate', 'freezone_retry_material_step',
                                'freezone_get_material_work', 'freezone_get_reference_director_state'])
def test_other_tools_do_not_receive_acceptance_recovery(monkeypatch, tool):
    _, _, handler, calls = adapter(monkeypatch, {'ok': False, 'error': NATIVE_ERROR, 'status_code': 422}, tool=tool)
    result = handler(arguments())
    assert result['ok'] is False and result['error'] == NATIVE_ERROR
    assert 'agent_instruction' not in result and len(calls) == 1


@pytest.mark.parametrize('override', [{'project_id': 'peer'}, {'canvas_id': 'peer'}])
def test_bound_scope_is_checked_before_any_http_or_retry_guidance(monkeypatch, override):
    _, _, handler, calls = adapter(monkeypatch, {'ok': False, 'error': NATIVE_ERROR})
    result = handler({**arguments(), **override})
    assert result['ok'] is False and not calls
    assert result['status'] == 'scope_mismatch'
    assert 'freezone_get_material_work' not in result['agent_instruction']
    assert 'Retry the same call' in result['agent_instruction']


def test_metadata_distinguishes_pending_summary_from_saved_reviews(monkeypatch):
    plugin, _, _, _ = adapter(monkeypatch, {'ok': False, 'error': NATIVE_ERROR})
    descriptions = {name: schema['description'] for name, schema, _ in plugin.TOOLS}
    summary = descriptions['freezone_get_reference_director_state']
    assert 'pending-decision summary' in summary
    assert 'freezone_get_material_work' in summary and 'passed' in summary
    accept = descriptions['freezone_accept_material_candidate']
    assert 'cached pre-confirmation review_ref' in accept
    assert 'browser confirmation' in accept and 'freezone_get_material_work' in accept


def test_tool_parameter_schemas_are_identical_to_post_task28_baseline(monkeypatch):
    original_read = Path.read_text
    def without_local_cache(path, *args, **kwargs):
        if '.cache' in path.parts:
            pytest.fail('Parameter contract test must not read an untracked local baseline')
        return original_read(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', without_local_cache)
    plugin, _, _, _ = adapter(monkeypatch, {'ok': False, 'error': NATIVE_ERROR})
    host = dict(plugin.__dict__)
    host['_RESULT_FIELDS'] = deepcopy(host['_RESULT_FIELDS'])
    host['_RESULT_SUCCESS_REQUIRED'] = deepcopy(host['_RESULT_SUCCESS_REQUIRED'])
    current = {name: schema['parameters'] for name, schema, _ in plugin._director_module.build_director_tools(host)}
    assert set(current) == set(DIRECTOR_TOOL_NAMES) | {
        'freezone_get_production_progress', 'freezone_advance_production',
        'freezone_select_material_work', 'freezone_get_material_speech_review',
    }
    # Additive tools do not change any legacy parameter contract.
    legacy = {name: current[name] for name in DIRECTOR_TOOL_NAMES}
    encoded = json.dumps(legacy, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('utf-8')
    assert hashlib.sha256(encoded).hexdigest() == DIRECTOR_PARAMETER_SHA256
