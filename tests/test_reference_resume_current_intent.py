"""Canonical scoped user content is business input, never a confirmation."""
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

from test_reference_remix_continuation import context, ready_run, service
from novelvideo.chat import reference_remix_continuation as continuation
from novelvideo.chat.store import ChatScope, ChatStore


SINCE = '2026-10-10T00:01:00.123456Z'


def scope(tmp_path, canvas='canvas', agent='main', project='project'):
    return ChatScope(kind='project', id=project, surface='freezone', canvas_id=canvas,
                     agent_id=agent, state_dir=str(tmp_path))


def seed(store, current_scope, role, content, *, created_at='2026-10-10T00:02:00+00:00',
         metadata=None, media=None, username='actor'):
    value = store.append_message(username, current_scope, role, content, turn_id='human-turn',
                                 metadata=metadata, media=media)
    with store.connect(username, current_scope) as conn:
        conn.execute('UPDATE chat_messages SET created_at=? WHERE id=?', (created_at, value['id']))
    return value['id']


def test_reader_uses_canonical_user_role_and_visible_content_without_metadata_or_ui(tmp_path):
    store = ChatStore()
    current = scope(tmp_path)
    seed(store, current, 'user', 'before run', created_at='2026-10-10T00:00:59+00:00')
    seed(store, current, 'assistant', 'assistant must stay excluded',
         metadata={'role': 'user', 'content': 'FORGED_USER_INTENT'})
    for role in ('trace', 'tool', 'agent_notification', 'system'):
        seed(store, current, role, 'excluded ' + role)
    original = seed(store, current, 'user', '复用已生成的两张人物图，中文声音、口型对上',
                    metadata={'role': 'assistant', 'content': 'FORGED_CONTENT', 'credential': 'SECRET_METADATA'},
                    media=[{'url': 'SECRET_MEDIA_URL'}])
    store.append_ui_event('actor', current, 'human-turn', {'type': 'assistant.clarification.request',
                          'submitted': True, 'answers': {'model': 'SECRET_UI_EVENT'}})
    messages = store.recent_user_messages('actor', current, created_after=SINCE)
    assert [row['message_id'] for row in messages] == [original]
    assert messages[0]['content'] == '复用已生成的两张人物图，中文声音、口型对上'
    assert messages[0]['turn_id'] == 'human-turn'
    assert not messages[0]['content_truncated']
    assert 'FORGED' not in json.dumps(messages)
    assert 'SECRET' not in json.dumps(messages)


def test_reader_compares_utc_instants_for_z_offset_and_microsecond_cutoff(tmp_path):
    store = ChatStore()
    current = scope(tmp_path)
    seed(store, current, 'user', 'older by microsecond', created_at='2026-10-10T00:01:00.123455+00:00')
    equal = seed(store, current, 'user', 'same instant', created_at='2026-10-10T00:01:00.123456+00:00')
    later = seed(store, current, 'user', 'later local offset', created_at='2026-10-10T08:01:00.123457+08:00')
    seed(store, current, 'user', 'invalid timestamp', created_at='invalid')
    seed(store, current, 'user', 'naive timestamp', created_at='2026-10-10T00:02:00')
    messages = store.recent_user_messages('actor', current, created_after=SINCE)
    assert [row['message_id'] for row in messages] == [equal, later]


def test_reader_never_enumerates_peer_canvas_agent_or_project_paths(tmp_path):
    store = ChatStore()
    current = scope(tmp_path/'actual-project')
    wanted = seed(store, current, 'user', 'current checked chat input')
    for peer in (scope(tmp_path/'actual-project', canvas='peer-canvas'),
                 scope(tmp_path/'actual-project', agent='peer-agent'),
                 scope(tmp_path/'peer-project', project='peer-project')):
        seed(store, peer, 'user', 'PEER_INPUT')
    messages = store.recent_user_messages('actor', current, created_after=SINCE)
    assert [row['message_id'] for row in messages] == [wanted]
    assert 'PEER_INPUT' not in json.dumps(messages)


def test_reader_is_bounded_newest_first_selection_and_preserves_chronological_source_order(tmp_path):
    store = ChatStore()
    current = scope(tmp_path)
    identities = [seed(store, current, 'user', f'visible request {index}') for index in range(20)]
    messages = store.recent_user_messages('actor', current, created_after=SINCE, limit=10_000)
    assert [row['message_id'] for row in messages] == identities[-8:]
    assert len(messages) == 8
    for index in range(10):
        seed(store, current, 'user', '界' * 3000 + f' tail{index}')
    messages = store.recent_user_messages('actor', current, created_after=SINCE)
    assert sum(len(row['content'].encode('utf-8')) for row in messages) <= 8192
    assert all(len(row['content'].encode('utf-8')) <= 2048 for row in messages)
    assert all(row['content_truncated'] for row in messages)
    assert [row['message_id'] for row in messages] == sorted(row['message_id'] for row in messages)
    assert len(json.dumps(messages, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) <= 8192
    for _ in range(8):
        seed(store, current, 'user', '"\n\x00' * 2000)
    escaped = store.recent_user_messages('actor', current, created_after=SINCE)
    assert len(json.dumps(escaped, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) <= 8192


def test_reader_missing_database_and_invalid_cutoff_do_not_create_or_migrate_state(tmp_path):
    store = ChatStore()
    current = scope(tmp_path)
    assert store.recent_user_messages('actor', current, created_after=SINCE) == []
    assert not tmp_path.exists() or list(tmp_path.iterdir()) == []
    seed(store, current, 'user', 'stored content')
    before = {str(path): path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    assert store.recent_user_messages('actor', current, created_after='invalid') == []
    assert store.recent_user_messages('actor', current, created_after='2026-10-10T00:00:00') == []
    assert before == {str(path): path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}


@pytest.mark.asyncio
async def test_saved_resume_carries_latest_scoped_visible_input_and_labels_old_receipt_historical(service, tmp_path, monkeypatch):
    from novelvideo.api import egress_binding
    from novelvideo.freezone import canvas_store

    ctx = context(tmp_path)
    run, task = await ready_run(service, ctx, monkeypatch)
    task.result['video_story']['summary'] = '实际采访、日常快切和结尾反转'
    saved = await service._save_analysis(ctx, run, result=task.result)
    run['result_fingerprint'] = continuation._node_fingerprint(saved['nodes'][1])
    run['user_interaction_receipt'] = {'turn_id': 'old-round', 'result': {
        'status': 'clarification_frontend_result', 'ok': True,
        'answers': {'video_model': {'option_ids': ['seedance-2.0-fast']}}}}
    assert service.store.update_owned(run, service.owner_token)
    store = ChatStore()
    current = service.scope(run)
    seed(store, current, 'user', 'OLD_BEFORE_RUN', created_at='2000-01-01T00:00:00Z')
    seed(store, current, 'assistant', 'ASSISTANT_NOT_USER', metadata={'role': 'user'})
    earlier = seed(store, current, 'user', '先做参考图', created_at=datetime.now(timezone.utc).isoformat())
    latest_text = '复用画布上的两张图，用 mini、480p、中文声音且口型对应，每镜头一个候选'
    latest = seed(store, current, 'user', latest_text, created_at=datetime.now(timezone.utc).isoformat(),
                  metadata={'content': 'FORGED_CONTENT'}, media=[{'url': 'SECRET_URL'}])
    peer = replace(current, agent_id='peer')
    seed(store, peer, 'user', 'PEER_MUST_NOT_LEAK', created_at=datetime.now(timezone.utc).isoformat())
    observed = []
    observed_kwargs = []
    @asynccontextmanager
    async def egress(**kwargs): yield None
    monkeypatch.setattr(egress_binding, 'request_egress_scope', egress)
    async def reply(*args, **kwargs):
        observed.append(args[2])
        observed_kwargs.append(kwargs)
        await args[3]({'type': 'assistant_message', 'message': {'text': '实际采访摘要，等待用户确认方向'}})
    monkeypatch.setattr(continuation.chat_service, 'stream_assistant_reply', reply)
    await service._resume_agent(run, ctx)
    assert len(observed) == 1
    prompt = observed[0]
    assert '[REFERENCE_REMIX_CURRENT_CREATIVE_INPUT]' in prompt
    block = prompt.split('[REFERENCE_REMIX_CURRENT_CREATIVE_INPUT]\n', 1)[1].split('\n[/REFERENCE_REMIX_CURRENT_CREATIVE_INPUT]', 1)[0]
    packet = json.loads(block)
    assert [row['message_id'] for row in packet['messages']] == [earlier, latest]
    assert packet['messages'][-1]['content'] == latest_text
    assert packet['scope'] == {'project_id': ctx.project_id, 'canvas_id': 'canvas', 'agent_id': 'main'}
    assert packet['authority'] == 'creative_input_only'
    assert '历史回答' in prompt and '仅可预填' in prompt and '不是本轮参数确认' in prompt
    assert 'canonical role=user' in prompt and '共享画布' in prompt
    assert '较新的创作输入' in prompt and '本轮' in prompt and '原生审批' in prompt
    for forbidden in ('OLD_BEFORE_RUN', 'ASSISTANT_NOT_USER', 'FORGED_CONTENT', 'SECRET_URL', 'PEER_MUST_NOT_LEAK'):
        assert forbidden not in prompt
    assert run['user_interaction_receipt']['turn_id'] == 'old-round'
    assert run['result_fingerprint'] == continuation._node_fingerprint(canvas_store.read_canvas(ctx.state_dir, 'canvas')['nodes'][1])

    # A later, genuinely submitted generation card for the same persisted
    # commission is passed to the next Agent turn without becoming approval.
    assert run.get('commission_ref') and run.get('generation_requirements_digest')
    run['user_interaction_receipt'] = {
        'bridge_key': 'bridge-answer', 'clarification_id': 'generation-card',
        'scope': {'project_id': ctx.project_id, 'canvas_id': 'canvas',
                  'agent_id': 'main', 'requester_user_id': ctx.requester_user_id,
                  'commission_id': run['commission_ref']['artifact_id']},
        'generation_requirements_digest': run['generation_requirements_digest'],
        'event': {'questions': [{'id': key} for key in (
            'video_model', 'video_aspect_ratio', 'video_resolution',
            'video_duration_seconds', 'video_generate_audio', 'video_variants_per_node',
        )]},
        'result': {'ok': True, 'status': 'clarification_frontend_result',
                   'answers': {
                       'video_model': {'option_ids': ['seedance']},
                       'video_aspect_ratio': {'option_ids': ['9:16']},
                       'video_resolution': {'option_ids': ['480p']},
                       'video_duration_seconds': {'option_ids': ['4']},
                       'video_generate_audio': {'option_ids': ['true']},
                       'video_variants_per_node': {'option_ids': ['1']},
                   }},
    }
    await service._resume_agent(run, ctx)
    assert observed_kwargs[-1]['trusted_reference_parameter_answers'] == run['user_interaction_receipt']['result']['answers']
    assert '历史回答' not in observed[-1]


def test_reference_continuation_does_not_force_a_second_parameter_card():
    policy = continuation.chat_service._prompt_with_user_context('actor', 'project', 'current request',
        tool_mode='freezone_canvas', surface_context={'freezone_canvas_id': 'canvas'},
        turn_id='new-round', require_generation_parameter_preflight=True)
    assert 'Do not ask a preliminary image/video parameter clarification' in policy
    assert 'may only prefill recommended choices' not in policy


def test_trusted_same_task_parameter_receipt_reuses_values_but_keeps_canvas_approval():
    policy = continuation.chat_service._prompt_with_user_context('actor', 'project', 'resume',
        tool_mode='freezone_canvas', surface_context={'freezone_canvas_id': 'canvas'},
        turn_id='resumed-round', require_generation_parameter_preflight=True,
        trusted_reference_parameter_answers={'video_generate_audio': True})
    assert 'video_generate_audio' in policy
    assert 'do not ask the same parameter questions again' in policy
    assert 'approval card' in policy
    assert 'never count as confirmation for this round' not in policy
