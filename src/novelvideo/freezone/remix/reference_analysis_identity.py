"""Stable admission keys for native reference analysis requests."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


_VALID_KEY = re.compile(r"[A-Za-z0-9._:-]{1,160}\Z")


def analysis_admission_key(
    *, project_id: str, canvas_id: str, source_node_id: str,
    user_goal: str, inputs: dict[str, Any], operation_id: str,
) -> str:
    """Convert a display-language operation ID to the API's ASCII key format.

    A valid key stays unchanged for replay of existing admissions. Invalid keys
    are hashed with their trusted scope and request inputs, so retrying the same
    intent is stable while a different source or intent cannot reuse it.
    """
    if _VALID_KEY.fullmatch(operation_id):
        return operation_id
    if not operation_id.strip():
        raise ValueError("reference analysis requires a stable operation ID")
    fingerprint = json.dumps({
        "project_id": project_id, "canvas_id": canvas_id,
        "source_node_id": source_node_id, "user_goal": user_goal,
        "inputs": inputs, "operation_id": operation_id,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "refa-" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
