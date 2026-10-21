"""Compare saved production goals without creative-guidance formatting noise.

A native plan topic may carry host guidance lines (speech mode, preserve and
replace choices) while the runner signs its artifact with the creative goal
alone. Those lines describe the same target, so target checks compare the
creative goal only.
"""
from __future__ import annotations

import re
from typing import Any

_GUIDANCE_LINE = re.compile(r"^\s*(保留与改变|声音方式|完整创作要求)\s*[:：]")


def canonical_goal(value: Any) -> str | None:
    """Return the creative goal with any host guidance block removed."""
    if not isinstance(value, str):
        return None
    kept: list[str] = []
    for line in value.replace("\r\n", "\n").split("\n"):
        if _GUIDANCE_LINE.match(line):
            break
        kept.append(line)
    return "\n".join(kept).strip()


def plan_goal_matches(left: Any, right: Any) -> bool:
    """True when both values name the same creative goal."""
    canonical = canonical_goal(left)
    return canonical is not None and canonical == canonical_goal(right)
