"""Director artifacts and submitted analysis are distinct from saved canvas edits."""
# ruff: noqa: F811

import json

import pytest

from novelvideo.chat import hermes_sdk, service
from novelvideo.chat.runtime_event_evidence import _codex_freezone_is_write_event
from novelvideo.chat.runtime_port import ChatBackendEvent
from novelvideo.chat.store import ChatScope
from test_plan_confirmation_contract import reviewed_plan  # noqa: F401, F811
from test_reference_director_api import setup_api  # noqa: F401


def tool(name, payload, *, arguments=None, call_id="call-a"):
    return ChatBackendEvent(type="tool_updated", name="dramaclaw." + name,
                            call_id=call_id, status="completed", input=arguments,
                            structured=payload)


def observed(run):
    return (run.last_result.get('metadata') or {}).get('operation_outcome') or {
        'operations': [], 'blocking_call_ids': []}


def submitted():
    return tool("freezone_start_reference_analysis", {
        "ok": True, "submitted": True, "status": "reference_analysis_submitted",
        "project_id": "project-a", "canvas_id": "canvas-a", "run_id": "run-a",
        "analysis_node_id": "analysis-a", "task_id": "task-a", "task_key": "task-key-a",
        "idempotency_key": "command-a", "run": {
            "status": "analyzing",
            "run_id": "run-a", "project_id": "project-a", "canvas_id": "canvas-a",
            "source_node_id": "source-a",
            "analysis_node_id": "analysis-a", "task_id": "task-a", "task_key": "task-key-a",
        },
    }, arguments={"source_node_id": "source-a", "idempotency_key": "command-a"})


@pytest.mark.anyio
async def test_real_review_settlement_recovers_only_matching_early_plan_failure(
        codex_turn, reviewed_plan, tmp_path):
    from novelvideo.freezone.remix.plan_review_settlement import ensure_reviewed_plan_confirmation
    from novelvideo.chat.store import ChatStore

    run, _ = codex_turn
    _, store, commission, _ = reviewed_plan
    settled = ensure_reviewed_plan_confirmation(store, commission_ref=commission['commission_ref'])
    blocker = {'code': 'plan_review_required', 'project_id': 'p', 'canvas_id': 'c',
               'requester_user_id': 'u',
               'commission_id': commission['commission_ref']['artifact_id'],
               'plan_ref': commission['plan_ref'],
               'direction_revision': commission['direction_revision']}
    early = tool('freezone_prepare_reference_decision',
                 {'ok': False, 'error': 'plan must be reviewed before user confirmation',
                  'blocker': blocker},
                 arguments={'kind': 'plan', 'proposal': {}}, call_id='early')
    reviewed = tool('freezone_review_production_plan',
                    {'ok': True, 'project_id': 'p', 'canvas_id': 'c',
                     'review_ref': settled['review_ref'], 'decision_ref': settled['decision_ref'],
                     'commission': commission}, call_id='reviewed')
    result = await run([early, reviewed], '方案已审核，请确认当前内容。',
                       project_id='p', canvas='c', project_dir=store.project_dir,
                       requester_user_id='u')
    assert result == '方案已审核，请确认当前内容。'
    scope = ChatScope(kind='project', id='p', surface='freezone', canvas_id='c',
                      agent_id='main', state_dir=str(tmp_path / 'state' / 'admin' / 'p'))
    saved = next(row for row in ChatStore().list_messages('admin', scope)
                 if row.get('turn_id') == 'turn-a')
    assert saved['plan_confirmation']['decision_ref'] == settled['decision_ref']

    other = tool('freezone_prepare_reference_decision',
                 {'ok': False, 'error': 'plan must be reviewed before user confirmation',
                  'blocker': {**blocker, 'plan_ref': {**commission['plan_ref'], 'revision': 99}}},
                 arguments={'kind': 'plan', 'proposal': {}}, call_id='other')
    result = await run([other, reviewed], '方案已审核，请确认当前内容。',
                       project_id='p', canvas='c', project_dir=store.project_dir,
                       requester_user_id='u', turn_id='turn-b')
    assert result == '方案已审核，请确认当前内容。'
    assert observed(run)['blocking_call_ids'] == ['other']
    failed_reply = next(row for row in ChatStore().list_messages('admin', scope)
                        if row.get('turn_id') == 'turn-b')
    assert 'plan_confirmation' not in failed_reply


@pytest.mark.anyio
async def test_hermes_real_review_settlement_does_not_append_recovered_error(
        monkeypatch, tmp_path, reviewed_plan):
    from novelvideo.chat import hermes_pool
    from novelvideo.chat.store import ChatStore
    from novelvideo.freezone.remix.plan_review_settlement import ensure_reviewed_plan_confirmation

    _, store, commission, _ = reviewed_plan
    settled = ensure_reviewed_plan_confirmation(store, commission_ref=commission['commission_ref'])
    blocker = {'code': 'plan_review_required', 'project_id': 'p', 'canvas_id': 'c',
               'requester_user_id': 'u',
               'commission_id': commission['commission_ref']['artifact_id'],
               'plan_ref': commission['plan_ref'],
               'direction_revision': commission['direction_revision']}
    early = tool('freezone_prepare_reference_decision',
                 {'ok': False, 'error': 'plan must be reviewed before user confirmation',
                  'blocker': blocker},
                 arguments={'kind': 'plan', 'proposal': {}}, call_id='early')
    early.raw = {'result': early.structured}
    reviewed = tool('freezone_review_production_plan',
                    {'ok': True, 'project_id': 'p', 'canvas_id': 'c',
                     'review_ref': settled['review_ref'], 'decision_ref': settled['decision_ref'],
                     'commission': commission}, call_id='reviewed')
    reviewed.raw = {'result': reviewed.structured}

    class Thread:
        async def stream(self, *_args, **_kwargs):
            yield early
            yield reviewed
            yield ChatBackendEvent(type='complete', text='方案已审核，请确认当前内容。')

    class Pool:
        async def get_for_user(self, *_args, **_kwargs):
            return Thread()

    async def no_auth(**_kwargs):
        return None

    monkeypatch.setattr(hermes_pool, 'pool', Pool())
    monkeypatch.setattr(service, 'authorize_hermes_launch', no_auth)
    monkeypatch.setattr(service, '_write_hermes_tool_mode', lambda *_args, **_kwargs: None)
    scope = ChatScope(kind='project', id='p', surface='freezone', canvas_id='c',
                      agent_id='main', state_dir=str(tmp_path / 'state'))
    async def collect(_event):
        pass
    result = await service._stream_assistant_reply_hermes(
        'admin', 'p', '参考视频换产品', collect, project_dir=store.project_dir,
        project_state_dir=tmp_path / 'state', requester_user_id='u',
        tool_mode='freezone_canvas', surface_context={'freezone_canvas_id': 'c'},
        store_scope=scope, turn_id='hermes-reviewed-plan',
    )
    assert result['content'] == '方案已审核，请确认当前内容。'
    saved = next(row for row in ChatStore().list_messages('admin', scope)
                 if row.get('turn_id') == 'hermes-reviewed-plan')
    assert saved['plan_confirmation']['decision_ref'] == settled['decision_ref']


@pytest.mark.anyio
async def test_codex_completed_plan_reply_persists_exact_presentation_metadata(
        codex_turn, reviewed_plan, tmp_path, monkeypatch):
    from novelvideo.chat.plan_confirmation_delivery import (
        metadata_for_delivered_plan, prepare_plan_presentation_target,
    )
    from novelvideo.chat.store import ChatStore

    run, _ = codex_turn
    _, store, commission, _ = reviewed_plan
    target = prepare_plan_presentation_target(store, commission['commission_ref'], agent_id='main')
    body = '当前方案包含三秒产品开场，横屏 24fps，请确认或提出修改意见。'
    assert metadata_for_delivered_plan(store, target, assistant_delivered=True, message=body)
    from novelvideo.chat import plan_confirmation_delivery
    saved_metadata = []
    def capture_metadata(*args, **kwargs):
        value = metadata_for_delivered_plan(*args, **kwargs)
        saved_metadata.append((kwargs, value))
        return value
    monkeypatch.setattr(plan_confirmation_delivery, 'metadata_for_delivered_plan', capture_metadata)
    emitted = []
    result = await run([], body, project_id='p', canvas='c',
                       project_dir=store.project_dir, requester_user_id='u',
                       trusted_plan_presentation_target=target,
                       turn_id='present-reviewed-plan', emitted=emitted)
    assert result == body
    assert next(row for row in emitted if row['type'] == 'done')['assistant_delivered'] is True
    assert saved_metadata and saved_metadata[0][1]
    scope = ChatScope(kind='project', id='p', surface='freezone', canvas_id='c',
        agent_id='main', state_dir=str(tmp_path / 'state' / 'admin' / 'p'))
    messages = ChatStore().list_messages('admin', scope)
    saved = next(row for row in messages if row['turn_id'] == 'present-reviewed-plan')
    assert saved['role'] == 'assistant', saved
    assert saved['content'] == body, saved
    assert saved['plan_confirmation']['plan_ref'] == commission['plan_ref']
    assert saved['plan_confirmation']['decision_ref'] == target['decision_ref']


@pytest.mark.parametrize("name", ["freezone_start_reference_analysis", "freezone_prepare_reference_decision",
    "freezone_review_production_plan", "freezone_prepare_material_work", "freezone_prepare_material_stage",
    "freezone_accept_material_candidate", "freezone_prepare_speech_correction"])
def test_director_artifact_tools_are_not_canvas_mutations(name):
    assert not _codex_freezone_is_write_event(tool(name, {"ok": True}))


@pytest.fixture
def codex_turn(monkeypatch, tmp_path):
    monkeypatch.setenv("NOVELVIDEO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("NOVELVIDEO_RUNTIME_DIR", str(tmp_path / "runtime"))
    prompts = []

    async def authorize(**kwargs):
        return None

    async def token(*args, **kwargs):
        return "test-token"

    class Auth:
        async def revoke_agent_session(self, token):
            pass

    monkeypatch.setattr(service, "authorize_hermes_launch", authorize)
    monkeypatch.setattr(service, "_create_page_agent_session_token", token)
    monkeypatch.setattr(service, "get_auth_session_port", lambda: Auth())
    monkeypatch.setattr(hermes_sdk, "_issue_turn_capability", lambda **kwargs: None)

    async def run(events, message, mode="read_only", receipts=None, *,
                  project_id="project-a", canvas="canvas-a", project_dir=None,
                  requester_user_id=None, turn_id="turn-a",
                  trusted_plan_presentation_target=None, emitted=None,
                  before_complete=None, plain=True):
        class Thread:
            async def stream(self, prompt):
                prompts.append(prompt)
                for event in events:
                    yield event
                if before_complete is not None:
                    before_complete()
                final_text = message if plain else json.dumps({
                    "message": message, "mode": mode, "canvas_receipts": receipts or []})
                yield ChatBackendEvent(type="complete", text=final_text)

        monkeypatch.setattr(service, "_build_codex_thread", lambda *args, **kwargs: Thread())
        state = tmp_path / "state" / "admin" / project_id
        async def collect(event):
            if emitted is not None:
                emitted.append(event)
        result = await service._stream_assistant_reply_codex(
            "admin", project_id, "参考视频换产品", collect, project_dir=project_dir,
            project_state_dir=state, requester_user_id=requester_user_id,
            trusted_plan_presentation_target=trusted_plan_presentation_target,
            tool_mode="freezone_canvas", surface_context={"freezone_canvas_id": canvas},
            store_scope=ChatScope(kind="project", id=project_id, surface="freezone",
                canvas_id=canvas, agent_id="main", state_dir=str(state)), turn_id=turn_id)
        run.last_result = result
        return result["content"]

    return run, prompts


@pytest.mark.anyio
async def test_canvas_prose_is_visible_before_turn_completion(codex_turn):
    run, _ = codex_turn
    emitted = []

    def before_complete():
        assert [event["text"] for event in emitted if event["type"] == "assistant_delta"] == [
            "先保留叙事结构。", "先保留叙事结构。人物可以重新设计。",
        ]

    result = await run([
        ChatBackendEvent(type="assistant_delta", text="先保留叙事结构。"),
        ChatBackendEvent(type="assistant_delta", text="先保留叙事结构。人物可以重新设计。"),
    ], "先保留叙事结构。人物可以重新设计。", emitted=emitted,
       before_complete=before_complete, plain=True)
    assert result == "先保留叙事结构。人物可以重新设计。"


@pytest.mark.anyio
async def test_codex_submitted_analysis_ack_does_not_claim_canvas_completion(codex_turn):
    run, prompts = codex_turn
    result = await run([submitted()], "分析已经完成。", mode="mutation")
    assert result == "分析已经完成。"
    assert observed(run)['operations'][0]['status'] == 'accepted'
    assert len(prompts) == 1


@pytest.mark.anyio
async def test_codex_transcribing_admission_is_acknowledged(codex_turn):
    run, _ = codex_turn
    event = submitted()
    event.structured["run"]["status"] = "transcribing"
    result = await run([event], "分析已经完成。", mode="mutation")
    assert result == "分析已经完成。"
    assert observed(run)['operations'][0]['status'] == 'accepted'


@pytest.mark.anyio
async def test_codex_transcribing_without_bound_task_is_not_acknowledged(codex_turn):
    run, _ = codex_turn
    event = submitted()
    event.structured["run"]["status"] = "transcribing"
    for field in ("task_id", "task_key"):
        event.structured[field] = None
        event.structured["run"][field] = None
    result = await run([event], "分析已经完成。", mode="mutation")
    assert result == "分析已经完成。"
    assert observed(run)['blocking_call_ids']


@pytest.mark.anyio
async def test_rejected_analysis_key_followed_by_verified_same_request_reuses_analysis(codex_turn):
    run, _ = codex_turn
    failed = tool("freezone_start_reference_analysis", {
        "ok": False, "status": "reference_analysis_not_submitted",
        "error": "原片分析请求已变化，请重新确认",
    }, arguments={"source_node_id": "source-a", "user_goal": "黑咖啡", "inputs": {"remix_direction": "黑咖啡"},
                  "idempotency_key": "old-key"}, call_id="old")
    recovered = submitted()
    recovered.call_id = "new"
    recovered.input = {"source_node_id": "source-a", "user_goal": "黑咖啡",
                       "inputs": {"remix_direction": "黑咖啡"}, "idempotency_key": "command-a"}
    recovered.structured["run"]["status"] = "analysis_ready"
    recovered.structured["run"]["task_id"] = None
    recovered.structured["run"]["task_key"] = None
    recovered.structured["task_id"] = None
    recovered.structured["task_key"] = None
    result = await run([failed, recovered], "已复用分析。")
    assert result == "已复用分析。"
    assert not observed(run)['blocking_call_ids']


@pytest.mark.anyio
async def test_existing_analysis_noop_allows_same_turn_plan_work(codex_turn):
    run, _ = codex_turn
    reused = tool('freezone_start_reference_analysis', {
        'ok': True, 'status': 'reference_analysis_reused', 'submitted': False,
        'project_id': 'project-a', 'canvas_id': 'canvas-a', 'source_node_id': 'source-a',
        'run_id': 'run-a', 'analysis_node_id': 'analysis-a',
        'commission_ref': {'artifact_id': 'commission-a', 'revision': 1, 'project_id': 'project-a'},
    }, arguments={'source_node_id': 'source-a', 'user_goal': '黑咖啡',
                  'inputs': {'remix_direction': '黑咖啡'}, 'idempotency_key': 'new-direction'})
    result = await run([reused], '已读取原片分析，正在根据黑咖啡方向处理。')
    assert result == '已读取原片分析，正在根据黑咖啡方向处理。'


@pytest.mark.anyio
async def test_failed_different_analysis_request_is_not_covered_by_later_submission(codex_turn):
    run, _ = codex_turn
    failed = tool("freezone_start_reference_analysis", {
        "ok": False, "status": "reference_analysis_not_submitted", "error": "原片不可用",
    }, arguments={"source_node_id": "other-source", "user_goal": "另一支视频", "inputs": {},
                  "idempotency_key": "old-key"}, call_id="old")
    result = await run([failed, submitted()], "已提交。")
    assert result == "已提交。"
    assert 'old' in observed(run)['blocking_call_ids']


@pytest.mark.anyio
async def test_legacy_direct_plan_receipt_cannot_hide_later_edit_failure(codex_turn):
    run, _ = codex_turn
    saved = tool("freezone_create_production_plan", {
        "ok": True, "project_id": "project-a", "canvas_id": "canvas-a",
        "canvas_apply_status": "direct_applied", "applied": True, "revision": 19,
        "plan_ref": {"artifact_id": "plan-a", "revision": 1}, "node_id": "plan-node",
    }, call_id="saved-plan")
    rejected = tool("freezone_prepare_production_edit", {
        "ok": False, "error": "tool_arguments_invalid", "project_id": "project-a",
        "canvas_id": "canvas-a",
    }, call_id="bad-edit")
    result = await run([saved, rejected], "方案已完成。", mode="mutation",
                       receipts=[{"bridge_key": None, "revision": 19}])
    assert result == "方案已完成。"
    assert observed(run)['blocking_call_ids'] == ['bad-edit']


@pytest.mark.anyio
async def test_codex_saved_decision_has_no_canvas_receipt_requirement(codex_turn):
    run, _ = codex_turn
    result = await run([tool("freezone_prepare_reference_decision", {
        "ok": True, "project_id": "project-a", "canvas_id": "canvas-a",
        "decision_ref": {"artifact_id": "decision-a", "revision": 1},
        "decision": {"status": "pending", "kind": "direction"},
    })], "方向建议已保存，等待你确认。")
    assert result == "方向建议已保存，等待你确认。"


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["missing_identity", "wrong_scope", "wrong_request", "wrong_source", "unidentified_call", "failed"])
async def test_codex_unverified_analysis_cannot_report_submission(codex_turn, change):
    run, _ = codex_turn
    event = submitted()
    if change == "missing_identity":
        event.structured.pop("run_id")
    elif change == "wrong_scope":
        event.structured["canvas_id"] = "other"
    elif change == "wrong_request":
        event.structured["idempotency_key"] = "other"
    elif change == "wrong_source":
        event.input["source_node_id"] = "other"
    elif change == "unidentified_call":
        event.call_id = None
    else:
        event.structured = {"ok": False, "error": "source unavailable"}
    result = await run([event], "分析已提交。")
    assert result == "分析已提交。"
    assert not any(row['status'] == 'completed' for row in observed(run)['operations'])


@pytest.mark.anyio
async def test_valid_analysis_ack_does_not_mask_failed_canvas_write(codex_turn):
    run, _ = codex_turn
    result = await run([submitted(), tool("freezone_create_node", {"ok": False, "error": "denied"}, call_id="canvas-call")], "已提交。")
    assert result == "已提交。"
    assert 'canvas-call' in observed(run)['blocking_call_ids']


@pytest.mark.anyio
@pytest.mark.parametrize("payload", [
    {"ok": True},
    {"ok": False, "error": "revision conflict"},
    {"ok": True, "project_id": "project-a", "canvas_id": "other", "decision_ref": {"artifact_id": "d", "revision": 1}},
    {"ok": True, "project_id": "project-a", "canvas_id": "canvas-a", "decision_ref": {"artifact_id": "d", "revision": True}},
])
async def test_unverified_director_artifact_cannot_report_success(codex_turn, payload):
    run, _ = codex_turn
    result = await run([tool("freezone_prepare_reference_decision", payload)], "建议已保存。")
    assert result == "建议已保存。"
    assert not any(row['status'] == 'completed' for row in observed(run)['operations'])


@pytest.mark.anyio
async def test_material_draft_stays_pending_user_approval(codex_turn):
    run, _ = codex_turn
    result = await run([tool("freezone_prepare_material_work", {
        "ok": True, "status": "workflow_draft_ready", "project_id": "project-a", "canvas_id": "canvas-a",
        "work_ref": {"artifact_id": "work-a", "revision": 2}, "draft_id": "draft-a",
        "revision": 1, "preview": {}, "ready_step_ids": ["first-frame"],
    })], "媒体已经生成。", mode="mutation")
    assert result == "媒体已经生成。"
    assert not any(row['status'] == 'completed' and row['category'] == 'canvas'
                   for row in observed(run)['operations'])


@pytest.mark.anyio
@pytest.mark.parametrize("status,expected", [
    ("cancelled", "操作已取消，未完成全部操作。"),
    ("timeout", "操作等待超时：未收到已保存结果，请重试。"),
    ("awaiting_approval", "操作等待确认或已保存结果，尚未完成。"),
])
async def test_non_canvas_outcomes_preserve_cancellation_timeout_and_approval(codex_turn, status, expected):
    run, _ = codex_turn
    event = tool("freezone_prepare_reference_decision", {"ok": False, "status": status})
    event.status = status
    assert await run([event], "成功。") == "成功。"
    assert observed(run)['operations'][0]['status'] == status


@pytest.mark.anyio
async def test_legacy_direct_plan_cannot_claim_canvas_revision(codex_turn):
    run, _ = codex_turn
    result = await run([tool("freezone_create_production_plan", {
        "ok": True, "project_id": "project-a", "canvas_id": "canvas-a",
        "canvas_apply_status": "direct_applied", "applied": True, "revision": 19,
        "plan_ref": {"artifact_id": "plan-a", "revision": 1}, "node_id": "plan-node",
    })], "作品方案节点已创建。", mode="mutation", receipts=[{"bridge_key": None, "revision": 19}])
    assert result == "作品方案节点已创建。"
    assert observed(run)['blocking_call_ids'] == []
    assert observed(run)['operations'] == []


@pytest.mark.anyio
async def test_saved_artifact_cannot_satisfy_canvas_mutation_claim(codex_turn):
    run, _ = codex_turn
    result = await run([tool("freezone_prepare_reference_decision", {
        "ok": True, "project_id": "project-a", "canvas_id": "canvas-a",
        "decision_ref": {"artifact_id": "decision-a", "revision": 9},
    })], "节点已创建。", mode="mutation", receipts=[{"bridge_key": None, "revision": 9}])
    assert result == "节点已创建。"
    assert not any(row['category'] == 'canvas' and row['status'] == 'completed'
                   for row in observed(run)['operations'])


@pytest.mark.anyio
async def test_codex_submission_receipt_from_mcp_text_content(codex_turn):
    run, _ = codex_turn
    event = submitted()
    event.output = {"content": [{"type": "text", "text": json.dumps(event.structured)}]}
    event.structured = None
    assert await run([event], "处理中。") == "处理中。"
    assert observed(run)['operations'][0]['status'] == 'accepted'


@pytest.mark.anyio
async def test_artifact_failure_is_not_masked_by_legacy_canvas_receipt(codex_turn):
    run, _ = codex_turn
    result = await run([
        tool("freezone_create_production_plan", {"ok": True, "project_id": "project-a",
            "canvas_id": "canvas-a", "canvas_apply_status": "direct_applied", "applied": True, "revision": 2}),
        tool("freezone_review_production_plan", {"ok": False, "error": "review revision changed"}, call_id="review-call"),
    ], "全部完成。", mode="mutation", receipts=[{"bridge_key": None, "revision": 2}])
    assert result == "全部完成。"
    assert 'review-call' in observed(run)['blocking_call_ids']


@pytest.mark.anyio
async def test_verified_retry_of_same_decision_does_not_leave_old_failure(codex_turn):
    run, _ = codex_turn
    target = {"artifact_id": "commission-a", "revision": 4}
    original = {"commission_ref": target, "command_id": "attempt-1", "kind": "direction"}
    corrected = {**original, "command_id": "attempt-2"}
    result = await run([
        tool("freezone_prepare_reference_decision", {"ok": False, "error": "invalid command id"},
             arguments=original, call_id="old"),
        tool("freezone_prepare_reference_decision", {"ok": True, "project_id": "project-a",
             "canvas_id": "canvas-a", "decision_ref": {"artifact_id": "decision-a", "revision": 1}},
             arguments=corrected, call_id="new"),
    ], "方向已保存。")
    assert result == "方向已保存。"


@pytest.mark.anyio
async def test_retry_keeps_started_input_when_completion_omits_it(codex_turn):
    run, _ = codex_turn
    arguments = {"commission_ref": {"artifact_id": "commission-a", "revision": 4},
                 "command_id": "retry", "kind": "direction"}
    started = ChatBackendEvent(type="tool_started",
                               name="dramaclaw.freezone_prepare_reference_decision",
                               call_id="new", input=arguments)
    completed = tool("freezone_prepare_reference_decision", {
        "ok": True, "project_id": "project-a", "canvas_id": "canvas-a",
        "decision_ref": {"artifact_id": "decision-a", "revision": 1},
    }, call_id="new")
    result = await run([
        tool("freezone_prepare_reference_decision", {"ok": False, "error": "old failure"},
             arguments={**arguments, "command_id": "first"}, call_id="old"),
        started, completed,
    ], "方向已保存。")
    assert result == "方向已保存。"


@pytest.mark.anyio
async def test_verified_retry_does_not_clear_failure_for_other_target(codex_turn):
    run, _ = codex_turn
    result = await run([
        tool("freezone_prepare_reference_decision", {"ok": False, "error": "other decision denied"},
             arguments={"commission_ref": {"artifact_id": "commission-other", "revision": 4},
                        "command_id": "attempt-1", "kind": "direction"}, call_id="old"),
        tool("freezone_prepare_reference_decision", {"ok": True, "project_id": "project-a",
             "canvas_id": "canvas-a", "decision_ref": {"artifact_id": "decision-a", "revision": 1}},
             arguments={"commission_ref": {"artifact_id": "commission-a", "revision": 4},
                        "command_id": "attempt-2", "kind": "direction"}, call_id="new"),
    ], "方向已保存。")
    assert result == "方向已保存。"
    assert 'old' in observed(run)['blocking_call_ids']


@pytest.mark.anyio
async def test_workflow_receipt_and_native_stage_failure_are_reported_separately(codex_turn):
    run, _ = codex_turn
    emitted = []
    result = await run([
        tool("freezone_advance_reference_production", {
            "ok": False, "error": "Unsupported commission event",
        }, call_id="bad-stage"),
        tool("freezone_confirm_workflow_draft", {
            "ok": True, "project_id": "project-a", "canvas_id": "canvas-a",
            "canvas_apply_status": "accepted", "applied": True,
            "bridge_key": "bridge-workflow",
        }, call_id="workflow"),
    ], "全部完成。", emitted=emitted, plain=True)
    assert result == "全部完成。"
    assert any(event.get("type") == "tool_updated"
               and event.get("call_id") == "bad-stage"
               and event.get("status") == "failed"
               for event in emitted)
    assert any(event.get("type") == "tool_updated"
               and event.get("call_id") == "workflow"
               and event.get("status") == "accepted"
               for event in emitted)
    final_message = next(event["message"] for event in emitted if event["type"] == "done")
    assert final_message["metadata"]["operation_outcome"]["blocking_call_ids"] == ["bad-stage"]
    assert any(event.get("call_id") == "bad-stage"
               and event.get("error") == "Unsupported commission event"
               for event in emitted)


@pytest.mark.anyio
@pytest.mark.parametrize("status,expected", [
    ("analysis_ready", "已复用保存的原片分析，接下来总结分析并确认复刻方向。"),
    ("submitting", "原片分析请求已保存，正在等待任务提交，确认后会继续处理。"),
])
async def test_codex_acknowledges_saved_adoption_and_pending_admission(codex_turn, status, expected):
    run, _ = codex_turn
    event = submitted()
    event.structured["run"]["status"] = status
    for key in ("task_id", "task_key"):
        event.structured[key] = None
        event.structured["run"][key] = None
    assert await run([event], "分析已完成。", mode="mutation") == "分析已完成。"
    assert not observed(run)['blocking_call_ids']


def progressed(status="awaiting_direction"):
    event = submitted()
    event.structured["run"].update(status=status, commission_phase=status,
        commission_ref={"artifact_id": "commission-a", "revision": 4, "project_id": "project-a"})
    return event


@pytest.mark.anyio
@pytest.mark.parametrize("status,expected", [
    ("awaiting_direction", "原片分析已保留，正在等待你确认复刻方向。"),
    ("drafting_plan", "原片分析已保留，正在准备作品方案。"),
    ("awaiting_plan_review", "原片分析已保留，作品方案等待审核。"),
    ("awaiting_plan_confirmation", "原片分析已保留，作品方案等待你确认。"),
    ("preparing_materials", "原片分析已保留，正在准备已确认方案的素材。"),
    ("aligning_speech", "原片分析已保留，正在核对台词与素材时间。"),
    ("composing", "原片分析已保留，正在准备合成。"),
    ("reviewing_output", "原片分析已保留，成片等待审核。"),
    ("completed", "本次制作进度已保存，请查看已交付结果。"),
])
async def test_codex_idempotent_analysis_replay_acknowledges_saved_progress(codex_turn, status, expected):
    run, prompts = codex_turn
    assert await run([progressed(status)], "分析已重新提交。", mode="mutation") == "分析已重新提交。"
    assert not observed(run)['blocking_call_ids']
    assert len(prompts) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["missing_commission", "wrong_commission_project", "invalid_revision", "wrong_phase", "changed_task", "run_error"])
async def test_progress_replay_requires_current_commission_and_task_identity(codex_turn, change):
    run, _ = codex_turn
    event = progressed()
    saved = event.structured["run"]
    if change == "missing_commission":
        saved.pop("commission_ref")
    elif change == "wrong_commission_project":
        saved["commission_ref"]["project_id"] = "other"
    elif change == "invalid_revision":
        saved["commission_ref"]["revision"] = True
    elif change == "wrong_phase":
        saved["commission_phase"] = "completed"
    elif change == "changed_task":
        saved["task_id"] = "other"
    else:
        saved["error"] = "saved source was replaced"
    result = await run([event], "进度已保存。")
    assert result == "进度已保存。"
    assert observed(run)['blocking_call_ids']


@pytest.mark.anyio
@pytest.mark.parametrize("status,expected", [
    ("stale", "原片分析关联已失效：source changed"),
    ("analysis_failed", "操作未完成：source changed"),
    ("resume_failed", "操作未完成：source changed"),
    ("cancelled", "操作已取消，未完成全部操作。"),
])
async def test_analysis_replay_keeps_unsuccessful_run_states_distinct(codex_turn, status, expected):
    run, _ = codex_turn
    event = submitted()
    event.structured["run"].update(status=status, error="source changed")
    assert await run([event], "成功。") == "成功。"
    assert observed(run)['blocking_call_ids']
