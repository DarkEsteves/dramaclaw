"""Generate a validated B-roll plan for one immutable remix script."""

from __future__ import annotations

import json
from typing import Any

from novelvideo.freezone.recipe_runtime import (
    generate_recipe_text,
    get_recipe_for_runtime,
    validate_structured_recipe_output,
)


async def generate_broll_plan(*, script: dict[str, Any], username: str) -> dict[str, Any]:
    events = script.get("events")
    if not isinstance(events, list):
        raise ValueError("script events are required for B-roll planning")
    event_ids = {
        item.get("event_id") for item in events
        if isinstance(item, dict) and isinstance(item.get("event_id"), str)
    }
    recipe = get_recipe_for_runtime(username=username, recipe_id="reference-broll-plan")
    content = await generate_recipe_text(
        username=username,
        recipe_id="reference-broll-plan",
        node_kind="text",
        node_prompt="判断改写脚本的每个语义事件是否需要 B-roll。只输出符合 Recipe output_schema 的 JSON。",
        upstream_text=json.dumps(script, ensure_ascii=False)[:16_000],
    )
    payload = validate_structured_recipe_output(recipe, content)
    if payload.get("dialogue") != script.get("dialogue"):
        raise ValueError("B-roll plan must preserve the exact script dialogue")
    requirements = payload.get("media_requirements")
    if not isinstance(requirements, list):
        raise ValueError("B-roll plan media_requirements must be an array")
    seen: set[str] = set()
    for requirement in requirements:
        event_id = requirement.get("event_id") if isinstance(requirement, dict) else None
        if event_id not in event_ids:
            raise ValueError("B-roll plan event_id must reference the saved script")
        if event_id in seen:
            raise ValueError("B-roll plan event_id must be unique")
        seen.add(event_id)
    return payload
