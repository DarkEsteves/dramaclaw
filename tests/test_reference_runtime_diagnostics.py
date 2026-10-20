import logging
import json

from novelvideo.chat import reference_runtime_diagnostics as diagnostics


def test_scoped_failures_are_bounded_and_recovery_is_once(caplog, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(diagnostics, "_clock", lambda: now[0])
    diagnostics._recent.clear()
    caplog.set_level(logging.WARNING, logger=diagnostics.__name__)
    scope = {"project_id": "project-a", "canvas_id": "canvas-a", "run_id": "run-a"}
    refs = {"bound_plan_ref": {"artifact_id": "old-plan", "revision": 2},
            "current_plan_ref": {"artifact_id": "new-plan", "revision": 3}}
    error = ValueError("stale plan revision; token=secret-value https://media.example/a?X-Amz-Signature=secret")
    diagnostics.log_reference_runtime_failure(component="reference_production_observer", scope=scope,
                                              stage="awaiting_plan_review", error=error, refs=refs)
    diagnostics.log_reference_runtime_failure(component="reference_production_observer", scope=scope,
                                              stage="awaiting_plan_review", error=error, refs=refs)
    assert len(caplog.records) == 1
    message = caplog.records[0].message
    assert "stale plan revision" in message
    assert "old-plan" in message and "new-plan" in message
    assert "secret-value" not in message and "media.example" not in message
    diagnostics.log_reference_runtime_failure(component="reference_production_observer",
                                              scope={**scope, "run_id": "run-b"},
                                              stage="awaiting_plan_review", error=error, refs=refs)
    assert len(caplog.records) == 2
    now[0] += 60
    diagnostics.log_reference_runtime_failure(component="reference_production_observer", scope=scope,
                                              stage="awaiting_plan_review", error=error, refs=refs)
    assert len(caplog.records) == 3
    diagnostics.log_reference_runtime_recovered(component="reference_production_observer",
                                                scope={**scope, "commission_ref": {"artifact_id": "advanced", "revision": 4}},
                                                stage="awaiting_plan_review")
    assert len(caplog.records) == 4
    diagnostics.log_reference_runtime_recovered(component="reference_production_observer", scope=scope,
                                                stage="awaiting_plan_review")
    assert len(caplog.records) == 4
    assert "recovered" in caplog.records[-1].message
    diagnostics._recent.clear()


def test_cache_and_untrusted_fields_are_bounded(caplog, monkeypatch):
    diagnostics._recent.clear()
    monkeypatch.setattr(diagnostics, "_clock", lambda: 1.0)
    caplog.set_level(logging.WARNING, logger=diagnostics.__name__)
    for index in range(1030):
        diagnostics.log_reference_runtime_failure(component="reference_production_observer",
            scope={"run_id": f"run-{index}", "project_id": "p", "canvas_id": "c",
                   "prompt": "private prompt"}, stage="scan", error=ValueError("bad"),
            refs={"bound_plan_ref": {"artifact_id": "https://private.example/a", "revision": 1}})
    assert len(diagnostics._recent) <= 1024
    assert "private prompt" not in caplog.text
    assert "private.example" not in caplog.text
    diagnostics._recent.clear()


def test_production_identity_chain_is_logged_without_user_content(caplog):
    diagnostics._recent.clear()
    caplog.set_level(logging.WARNING, logger=diagnostics.__name__)
    diagnostics.log_reference_runtime_failure(
        component='reference_production_observer', stage='material_resume',
        scope={'project_id': 'p', 'canvas_id': 'c', 'run_id': 'run-a',
               'business_turn_id': 'business-a', 'runtime_turn_id': 'runtime-a',
               'operation_id': 'operation-a', 'draft_id': 'draft-a',
               'attempt_id': 'attempt-a', 'workflow_run_id': 'workflow-a',
               'task_id': 'task-a', 'clarification_id': 'clarify-a',
               'result_revision': 3, 'prompt': 'private request'},
        error=ValueError('source unavailable'),
    )
    record = json.loads(caplog.records[-1].message)
    assert record['business_turn_id'] == 'business-a'
    assert record['runtime_turn_id'] == 'runtime-a'
    assert record['task_id'] == 'task-a'
    assert record['result_revision'] == '3'
    assert 'private request' not in caplog.text
    diagnostics._recent.clear()


def test_diagnostic_sink_failure_is_nonfatal_and_unknown_stack_is_bounded(caplog, monkeypatch):
    diagnostics._recent.clear()
    def fail_sink(*_args, **_kwargs):
        raise OSError("disk full")
    original = diagnostics.logger.warning
    monkeypatch.setattr(diagnostics.logger, "warning", fail_sink)
    diagnostics.log_reference_runtime_failure(component="reference_remix_continuation",
        scope={"run_id": "failed-sink"}, stage="scan", error=OSError("disk full"))
    diagnostics.log_reference_runtime_recovered(component="reference_remix_continuation",
        scope={"run_id": "failed-sink"}, stage="scan")
    monkeypatch.setattr(diagnostics.logger, "warning", original)
    caplog.set_level(logging.WARNING, logger=diagnostics.__name__)
    try:
        raise ConnectionError("lost upstream")
    except ConnectionError as error:
        diagnostics.log_reference_runtime_failure(component="reference_remix_continuation",
            scope={"run_id": "stack-run"}, stage="agent_resume", error=error)
    assert len(caplog.records) == 1
    assert '"stack"' in caplog.records[0].message
    assert "test_reference_runtime_diagnostics.py" in caplog.records[0].message
    diagnostics._recent.clear()


def test_quoted_authorization_and_secret_headers_never_reach_logs(caplog):
    diagnostics._recent.clear()
    caplog.set_level(logging.WARNING, logger=diagnostics.__name__)
    diagnostics.log_reference_runtime_failure(component="reference_production_observer",
        scope={"run_id": "quoted-headers"}, stage="observe",
        error=ValueError("headers={'Authorization': 'Bearer private-bearer-token', "
                         "'X-Api-Key': 'private-api-key', 'Cookie': 'session=private-cookie'}"))
    diagnostics.log_reference_runtime_failure(component="reference_production_observer",
        scope={"run_id": "json-headers"}, stage="observe",
        error=ValueError('headers={"Authorization":"Bearer json-bearer-token",'
                         '"X-Auth-Token":"json-auth-token"}'))
    assert len(caplog.records) == 2
    assert "private-bearer-token" not in caplog.text
    assert "private-api-key" not in caplog.text
    assert "private-cookie" not in caplog.text
    assert "json-bearer-token" not in caplog.text
    assert "json-auth-token" not in caplog.text
    assert all(json.loads(record.message)["cause"] == "[redacted-sensitive-headers]"
               for record in caplog.records)
    diagnostics._recent.clear()


def test_escaped_cookie_and_double_encoded_authorization_fail_closed(caplog):
    diagnostics._recent.clear()
    caplog.set_level(logging.WARNING, logger=diagnostics.__name__)
    examples = (
        r'headers={"Cookie": "session=\"private-cookie-a\"; other=private-cookie-b"}',
        json.dumps(json.dumps({"Authorization": "Bearer private-double-json"})),
        r'{"\u0041uthorization": "Bearer private-unicode-escape"}',
        r'{"X-Auth-Token": "prefix\\\"private-unusual-escape"}',
    )
    for index, message in enumerate(examples):
        diagnostics.log_reference_runtime_failure(component="reference_production_observer",
            scope={"run_id": f"escaped-header-{index}"}, stage="observe",
            error=ValueError(message),
            refs={"bound_plan_ref": {"artifact_id": "old-plan", "revision": 1}})
    assert len(caplog.records) == len(examples)
    assert all(json.loads(record.message)["cause"] == "[redacted-sensitive-headers]"
               for record in caplog.records)
    assert all(json.loads(record.message)["error_code"] == "ValueError"
               for record in caplog.records)
    assert all(json.loads(record.message)["bound_plan_ref"]["artifact_id"] == "old-plan"
               for record in caplog.records)
    for secret in ("private-cookie-a", "private-cookie-b", "private-double-json",
                   "private-unicode-escape", "private-unusual-escape"):
        assert secret not in caplog.text
    diagnostics._recent.clear()


def test_long_cause_with_late_sensitive_header_is_hidden(caplog):
    diagnostics._recent.clear()
    caplog.set_level(logging.WARNING, logger=diagnostics.__name__)
    diagnostics.log_reference_runtime_failure(component="reference_production_observer",
        scope={"run_id": "long-header"}, stage="observe",
        error=ValueError("private-prefix " + "x" * 4200 + " Authorization: Bearer private-late"))
    assert len(caplog.records) == 1
    assert json.loads(caplog.records[0].message)["cause"] == "[redacted-oversized-cause]"
    assert "private-prefix" not in caplog.text
    assert "private-late" not in caplog.text
    diagnostics._recent.clear()
