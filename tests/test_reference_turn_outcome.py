from novelvideo.chat.reference_turn_outcome import ReferenceTurnOutcome


def outcome():
    return ReferenceTurnOutcome(
        turn_id="turn",
        scope={"project_id": "p", "canvas_id": "c", "agent_id": "a"},
        revision=2,
    )


def test_normal_finish_without_content_does_not_prove_context_delivery():
    result = outcome().finish(current_revision=2)
    assert result["execution_blocker"]["code"] == "context_delivery_failed"


def test_failed_read_then_current_host_read_and_assistant_recovers():
    turn = outcome()
    turn.failure("context_delivery_failed")
    turn.context(source="saved_canvas", revision=2, content={"summary": "实际摘要"})
    turn.assistant("原片在三秒处切换镜头；建议保留节奏。")
    result = turn.finish(current_revision=2)
    assert result["execution_blocker"] is None
    assert result["last_successful_context_receipt"]["turn_id"] == "turn"


def test_read_alone_or_model_claim_cannot_make_summary_ready():
    turn = outcome()
    turn.context(source="saved_canvas", revision=2, content={"summary": "实际摘要"})
    assert (
        turn.finish(current_revision=2)["execution_blocker"]["code"]
        == "context_delivery_failed"
    )
    turn = outcome()
    turn.assistant("我已经读取了所有分析。")
    assert (
        turn.finish(current_revision=2)["execution_blocker"]["code"]
        == "context_delivery_failed"
    )


def test_revision_change_blocks_success_and_preserves_creation_phase():
    turn = outcome()
    turn.context(source="saved_canvas", revision=2, content={"summary": "实际摘要"})
    turn.assistant("分析建议")
    assert (
        turn.finish(current_revision=3, context_current=False)["execution_blocker"][
            "code"
        ]
        == "canvas_conflict"
    )


def test_optional_capability_failure_does_not_overrule_successful_evidence():
    turn = outcome()
    turn.failure("capability_unavailable")
    turn.context(source="saved_canvas", revision=2, content={"summary": "实际摘要"})
    turn.assistant("分析建议")
    assert turn.finish(current_revision=2)["execution_blocker"] is None


def test_browser_absence_has_a_specific_next_action():
    turn = outcome()
    turn.failure("browser_required")
    assert (
        turn.finish(current_revision=2)["execution_blocker"]["next_action"]
        == "open_canvas"
    )


def test_saved_api_receipt_is_generated_from_real_host_result_and_scoped(tmp_path):
    from novelvideo.chat.reference_turn_outcome import (
        record_context_receipt,
        read_context_receipts,
    )

    binding = {
        "turn_id": "turn",
        "project_id": "p",
        "canvas_id": "c",
        "agent_id": "a",
        "requester_user_id": "user",
    }
    record_context_receipt(
        tmp_path,
        binding,
        {
            "source": "saved_canvas",
            "canvas_revision": 2,
            "responses": [
                {
                    "type": "node_detail",
                    "data": {
                        "nodes": [
                            {
                                "id": "analysis",
                                "reference_analysis": {"summary": "real content"},
                            }
                        ]
                    },
                }
            ],
        },
    )
    rows = read_context_receipts(tmp_path, binding)
    assert rows[0]["revision"] == 2
    assert rows[0]["source"] == "saved_canvas"
    assert rows[0]["content_digest"]
    assert rows[0]["analysis_reads"][0]["analysis_node_id"] == "analysis"
    assert rows[0]["analysis_reads"][0]["summary_digest"]
    assert (
        read_context_receipts(tmp_path, {**binding, "requester_user_id": "other"}) == []
    )


def test_native_artifact_evidence_receipt_records_real_items_and_refs(tmp_path):
    from novelvideo.chat.reference_turn_outcome import (
        record_context_receipt,
        read_context_receipts,
    )

    binding = {
        "turn_id": "turn",
        "project_id": "p",
        "canvas_id": "c",
        "agent_id": "a",
        "requester_user_id": "user",
    }
    result = {
        "source": "reference_evidence_api",
        "canvas_revision": 2,
        "analysis_ref": {"artifact_id": "analysis", "revision": 4},
        "items": [{"kind": "cut", "time_us": 400000}],
        "coverage": {"complete": True},
    }
    record_context_receipt(tmp_path, binding, result)
    assert (
        read_context_receipts(tmp_path, binding)[0]["artifact_ref"]
        == result["analysis_ref"]
    )


def test_unrelated_canvas_revision_change_does_not_invalidate_current_analysis():
    turn = outcome()
    turn.context(source="saved_canvas", revision=2, content={"summary": "实际摘要"})
    turn.assistant("实际分析建议")
    assert turn.finish(current_revision=3)["execution_blocker"] is None


def test_historical_recovery_requires_real_failure_and_no_direction_or_success():
    from novelvideo.chat.reference_turn_outcome import historical_false_wait

    run = {"status": "awaiting_direction", "resume_turn_id": "turn"}
    event = {
        "turn_id": "turn",
        "type": "agent.tool.updated",
        "name": "freezone_request_canvas_context",
        "result_json": {"ok": False, "error": "delivery failed"},
    }
    assert historical_false_wait(run, [event], {"decisions": [], "direction": None})
    assert not historical_false_wait(run, [], {"decisions": []})
    assert not historical_false_wait(
        run, [event], {"decisions": [{"kind": "direction"}]}
    )
    assert not historical_false_wait(
        run, [{**event, "turn_id": "other"}], {"decisions": []}
    )
    success = {
        **event,
        "result_json": {"ok": True, "data": {"nodes": [{"id": "analysis"}]}},
    }
    assert not historical_false_wait(run, [event, success], {"decisions": []})


def test_permission_expiry_conflict_have_distinct_actions():
    from novelvideo.chat.reference_turn_outcome import context_failure_code

    assert (
        context_failure_code({"ok": False, "status_code": 403}) == "permission_denied"
    )
    assert context_failure_code({"ok": False, "status_code": 410}) == "request_expired"
    assert context_failure_code({"ok": False, "status_code": 409}) == "canvas_conflict"


def test_assistant_delivery_does_not_clear_production_execution_failure():
    turn = outcome()
    turn.context(source='saved_canvas', revision=2, content={'summary': 'real analysis'})
    turn.failure('production_execution_failed')
    turn.assistant('方案已经生成。')
    assert turn.finish(current_revision=2)['execution_blocker']['code'] == 'production_execution_failed'
