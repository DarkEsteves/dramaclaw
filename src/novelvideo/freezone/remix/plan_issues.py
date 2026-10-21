"""Stable, locatable codes for cross-object production plan validation failures.

Cross-object rules (a speech carrier spanning beats, a placement pointing at a
missing turn, two takes owning one turn, ...) cannot be located by a schema
path alone: the failure belongs to a relationship, not to a field.  Raising a
``PydanticCustomError`` with the owning entity IDs keeps that information in
``ctx`` so diagnostics, one-shot repair scopes and user notices can address the
exact objects instead of guessing from a root-level ``value_error``.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic_core import PydanticCustomError

from novelvideo.llm_instrumentation import bounded_error_message


_IDENTIFIER_KEYS = ("placement_id", "selection_id", "beat_id", "turn_id",
                    "take_id", "cue_id", "ref_id")
_SAFE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_SAFE_PATH = re.compile(r"/[A-Za-z0-9_/-]{0,95}")
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{2,63}")


def _safe_identifier(value: Any) -> str | None:
    return value if isinstance(value, str) and _SAFE_ID.fullmatch(value) else None


def _safe_path(value: Any) -> str:
    return value if isinstance(value, str) and _SAFE_PATH.fullmatch(value) else "/"


def plan_issue(code: str, message: str, *, path: str | None = None,
               **identifiers: Any) -> PydanticCustomError:
    """Build a plan validation error that survives pydantic as a typed issue.

    ``path`` is only set for cross-object rules that cannot be located by a
    field: leaving it absent lets a field-level rule keep pydantic's ``loc``.
    """
    if not _SAFE_CODE.fullmatch(code):
        raise ValueError(f"plan issue code must be a stable snake_case code: {code}")
    issue: dict[str, str] = {"code": code}
    if path is not None:
        issue["path"] = _safe_path(path)
    for key in _IDENTIFIER_KEYS:
        value = _safe_identifier(identifiers.get(key))
        if value is not None:
            issue[key] = value
    error = PydanticCustomError(code, message, dict(issue))
    # Carried for direct callers that validate an already-parsed plan; pydantic
    # copies the same facts into ``ctx`` when a model validator raises it.
    error.issue = dict(issue)
    return error


def issue_from_pydantic_row(row: dict[str, Any], identity: dict[str, str] | None = None) -> dict[str, str]:
    """Rebuild a locatable issue from one pydantic error row."""
    context = row.get("ctx") if isinstance(row.get("ctx"), dict) else {}
    issue: dict[str, str] = {"code": _safe_identifier(str(row.get("type") or "")) or "validation"}
    path = context.get("path")
    if not isinstance(path, str):
        location = row.get("loc") or ()
        path = "/" + "/".join(str(part) for part in location)
    issue["path"] = _safe_path(path)
    for key in _IDENTIFIER_KEYS:
        value = _safe_identifier(context.get(key))
        if value is not None:
            issue[key] = value
    if len(issue) == 1 and isinstance(identity, dict):
        for key, value in identity.items():
            if key in _IDENTIFIER_KEYS and _safe_identifier(value) is not None:
                issue[key] = value
    return issue


def issue_error_message(row: dict[str, Any]) -> str | None:
    """Extract the bounded, allowlisted hint behind one failed rule."""
    return bounded_error_message(row.get("msg"))
