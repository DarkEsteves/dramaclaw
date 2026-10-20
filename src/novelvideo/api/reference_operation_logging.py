"""Bounded backend diagnostics for reference production request failures."""

from __future__ import annotations

import json
import logging
import re
import traceback
from contextvars import ContextVar
from pathlib import PurePath
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute


logger = logging.getLogger(__name__)
_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9_-]")
_request_id: ContextVar[str | None] = ContextVar('reference_operation_request_id', default=None)


def current_request_id() -> str | None:
    """Return the request ID assigned by ReferenceOperationRoute."""
    return _request_id.get()


def _bounded(value: Any, limit: int = 80) -> str:
    return _SAFE_SEGMENT.sub("_", str("" if value is None else value)[:limit])


def validation_failure_record(request: Any, errors: list[dict[str, Any]]) -> dict[str, Any]:
    params = request.scope.get("path_params") or {}
    endpoint = request.scope.get("endpoint")
    location = errors[0].get("loc") if errors and isinstance(errors[0], dict) else ()
    field_path = ".".join(_bounded(part, 40) for part in tuple(location or ())[:12])
    trace = {key: _bounded(params[key]) for key in
             ("artifact_id", "run_id", "task_id", "draft_id", "operation_id")
             if params.get(key) is not None}
    request_id = _request_id.get()
    return {"event": "reference_operation_failed",
            **({'request_id': request_id} if request_id else {}),
            "project_id": _bounded(params.get("project")),
            "canvas_id": _bounded(params.get("canvas_id")),
            "tool": _bounded(getattr(endpoint, "__name__", "reference_route")),
            **trace, "error_code": "request_validation_failed",
            "field_path": field_path, "retryable": False}


def log_plan_review_failure(*, project_id: str, canvas_id: str, commission_ref: dict,
                            plan_ref: dict, phase: str, error: Exception) -> None:
    def safe_ref(ref: dict) -> dict:
        return {key: _bounded(ref[key]) for key in ('artifact_id', 'revision') if key in ref}

    request_id = _request_id.get()
    record = {'event': 'plan_review_unexpected_failure',
              **({'request_id': request_id} if request_id else {}),
              'project_id': _bounded(project_id), 'canvas_id': _bounded(canvas_id),
              'commission_ref': safe_ref(commission_ref), 'current_plan_ref': safe_ref(plan_ref),
              'phase': _bounded(phase), 'error_code': type(error).__name__,
              'stack': [f"{PurePath(frame.filename).name}:{frame.lineno}:{frame.name[:64]}"
                        for frame in traceback.extract_tb(error.__traceback__)[-6:]]}
    logger.error("%s", json.dumps(record, ensure_ascii=False, sort_keys=True))


class ReferenceOperationRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def logged(request: Request):
            request_token = _request_id.set(uuid4().hex)
            try:
                return await handler(request)
            except RequestValidationError as exc:
                logger.warning("%s", json.dumps(validation_failure_record(request, exc.errors()),
                                                 ensure_ascii=False, sort_keys=True))
                raise
            except HTTPException as exc:
                record = validation_failure_record(request, [])
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                record["error_code"] = _bounded(detail.get("code") or f"reference_route_{exc.status_code}")
                record["http_status"] = exc.status_code
                record["retryable"] = exc.status_code in {408, 409, 429, 503}
                logger.warning("%s", json.dumps(record, ensure_ascii=False, sort_keys=True))
                raise
            except Exception as exc:
                record = validation_failure_record(request, [])
                record['error_code'] = type(exc).__name__
                record['http_status'] = 500
                record['stack'] = [
                    f"{PurePath(frame.filename).name}:{frame.lineno}:{frame.name[:64]}"
                    for frame in traceback.extract_tb(exc.__traceback__)[-6:]
                ]
                logger.error("%s", json.dumps(record, ensure_ascii=False, sort_keys=True))
                raise
            finally:
                _request_id.reset(request_token)

        return logged
