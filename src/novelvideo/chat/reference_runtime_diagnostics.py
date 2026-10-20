"""Bounded, scoped diagnostics for reference background work.

This module only emits logs. It must never participate in state transitions.
"""

from __future__ import annotations

from collections import OrderedDict
import json
import logging
from pathlib import PurePath
import re
import threading
import time
import traceback

from novelvideo.utils.error_redaction import safe_exception_message


logger = logging.getLogger(__name__)
_clock = time.monotonic
_recent: OrderedDict[tuple, float] = OrderedDict()
_lock = threading.Lock()
_MAX_KEYS = 1024
_INTERVAL_SECONDS = 60
_MAX_CAUSE_SCAN = 4096
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_URL = re.compile(r"(?i)\b(?:https?://|s3://|file://)[^\s\]})>,;]+")
_SENSITIVE_QUERY = re.compile(r"(?i)\b(?:x-amz-[\w-]+|signature|sig|access_token|token|api_key|key)\s*[=:]\s*[^\s,;]+")
_SENSITIVE_HEADER_NAME = re.compile(
    r"(?i)(?:authorization|cookie|set-cookie|api[-_]?key|"
    r"x-[a-z0-9_-]*(?:token|secret|api[-_]?key)[a-z0-9_-]*)"
)
_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")
_HEX_ESCAPE = re.compile(r"\\x([0-9a-fA-F]{2})")
_SCOPE_KEYS = (
    "project_id", "requester_user_id", "canvas_id", "run_id", "commission_ref",
    "business_turn_id", "runtime_turn_id", "operation_id", "draft_id",
    "attempt_id", "workflow_run_id", "task_id", "clarification_id",
    "result_revision",
)
_KEY_KEYS = ("project_id", "requester_user_id", "canvas_id", "run_id")
_REF_KEYS = ("commission_ref", "bound_plan_ref", "current_plan_ref")


def _identifier(value: object) -> str | None:
    if isinstance(value, (str, int)):
        result = str(value)
        if _IDENTIFIER.fullmatch(result):
            return result
    return None


def _ref(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    result = {key: checked for key in ("artifact_id", "project_id")
              if (checked := _identifier(value.get(key))) is not None}
    revision = value.get("revision")
    if isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0:
        result["revision"] = revision
    return result or None


def _fields(scope: dict, refs: dict | None = None) -> dict:
    result = {}
    for key in _SCOPE_KEYS:
        value = scope.get(key)
        checked = _ref(value) if key.endswith("_ref") else _identifier(value)
        if checked is not None:
            result[key] = checked
    for key in _REF_KEYS:
        checked = _ref((refs or {}).get(key))
        if checked is not None:
            result[key] = checked
    return result


def _scope_key(scope: dict) -> tuple:
    # The commission revision may advance during recovery. It stays in the
    # emitted record but cannot determine whether a run has recovered.
    return tuple((key, value) for key in _KEY_KEYS
                 if (value := _identifier(scope.get(key))) is not None)


def _cause(error: Exception) -> str:
    message = safe_exception_message(error)
    if len(message) > _MAX_CAUSE_SCAN:
        return "[redacted-oversized-cause]"
    # Header values may contain quoted and escaped delimiters, including JSON
    # nested inside JSON. A partial regex replacement can expose the tail.
    # Lose the cause text whenever a sensitive header name appears, even after
    # common character escapes; error_code, scope and refs remain available.
    probe = message
    for _ in range(3):
        if _SENSITIVE_HEADER_NAME.search(probe):
            return "[redacted-sensitive-headers]"
        decoded = _UNICODE_ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), probe)
        decoded = _HEX_ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), decoded)
        decoded = decoded.replace("\\\\", "\\")
        if decoded == probe:
            break
        probe = decoded
    message = _URL.sub("[redacted-url]", message)
    message = _SENSITIVE_QUERY.sub("[redacted]", message)
    return " ".join(message.split())[:240]


def _stack(error: Exception) -> list[str]:
    # Structured frame locations avoid logging exception args, source lines,
    # user paths, or an unbounded traceback.
    return [f"{PurePath(frame.filename).name}:{frame.lineno}:{frame.name[:64]}"
            for frame in traceback.extract_tb(error.__traceback__)[-6:]]


def log_reference_runtime_failure(*, component: str, scope: dict, stage: str,
                                  error: Exception, refs: dict | None = None) -> None:
    try:
        component_id = _identifier(component) or "reference_runtime"
        safe_scope = _fields(scope, refs)
        code = type(error).__name__
        key = (component_id, _scope_key(scope), code)
        now = _clock()
        with _lock:
            previous = _recent.get(key)
            if previous is not None and now - previous < _INTERVAL_SECONDS:
                return
            _recent[key] = now
            _recent.move_to_end(key)
            while len(_recent) > _MAX_KEYS:
                _recent.popitem(last=False)
        record = {"event": "reference_runtime_failure", "component": component_id,
                  **safe_scope, "stage": _identifier(stage) or "unknown",
                  "error_code": code, "cause": _cause(error)}
        if code not in {"ValueError", "RuntimeError", "StaleReference", "PermissionError"}:
            stack = _stack(error)
            if stack:
                record["stack"] = stack
        logger.warning("%s", json.dumps(record, ensure_ascii=False, sort_keys=True))
    except Exception:
        # Logging can fail under pressure; diagnostics cannot change task state.
        return


def log_reference_runtime_recovered(*, component: str, scope: dict, stage: str) -> None:
    try:
        component_id = _identifier(component) or "reference_runtime"
        scoped = _scope_key(scope)
        with _lock:
            matching = [key for key in _recent if key[:2] == (component_id, scoped)]
            if not matching:
                return
            for key in matching:
                del _recent[key]
        logger.warning("%s", json.dumps({"event": "reference_runtime_recovered",
            "component": component_id, **_fields(scope), "stage": _identifier(stage) or "unknown"},
            ensure_ascii=False, sort_keys=True))
    except Exception:
        return
