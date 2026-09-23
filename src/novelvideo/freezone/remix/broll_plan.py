"""Generate a validated B-roll plan for one immutable remix script."""

from __future__ import annotations

import json
from typing import Any

from novelvideo.freezone.recipe_runtime import (
    generate_recipe_text,
    get_recipe_for_runtime,
    validate_structured_recipe_output,
)

_PLANNING_BATCH_CHARS = 12_000
_PLANNING_CAPTION_CHARS = 4_000


def _planning_event_batches(events: list[Any]) -> list[list[dict[str, str]]]:
    projected = [
        {
            "event_id": str(item["event_id"]),
            "caption": str(item.get("caption") or "")[:_PLANNING_CAPTION_CHARS],
        }
        for item in events
        if isinstance(item, dict)
    ]
    batches: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    for event in projected:
        candidate = [*current, event]
        if current and len(json.dumps({"events": candidate}, ensure_ascii=False)) > _PLANNING_BATCH_CHARS:
            batches.append(current)
            current = [event]
        else:
            current = candidate
    if current:
        batches.append(current)
    return batches


async def generate_broll_plan(*, script: dict[str, Any], username: str) -> dict[str, Any]:
    events = script.get("events")
    if not isinstance(events, list):
        raise ValueError("script events are required for B-roll planning")
    event_id_list = [
        item.get("event_id") for item in events
        if isinstance(item, dict) and isinstance(item.get("event_id"), str) and item.get("event_id")
    ]
    if not event_id_list or len(event_id_list) != len(events):
        raise ValueError("script events must contain non-empty event_id values")
    event_ids = set(event_id_list)
    if len(event_ids) != len(event_id_list):
        raise ValueError("script event_id must be unique")
    recipe = get_recipe_for_runtime(username=username, recipe_id="reference-broll-plan")

    async def request_plan(batch: list[dict[str, str]], node_prompt: str) -> tuple[list[dict[str, Any]], set[str]]:
        batch_event_ids = {item["event_id"] for item in batch}
        content = await generate_recipe_text(
            username=username,
            recipe_id="reference-broll-plan",
            node_kind="text",
            node_prompt=node_prompt,
            upstream_text=json.dumps({"events": batch}, ensure_ascii=False),
        )
        payload = validate_structured_recipe_output(recipe, content)
        requirements = payload.get("media_requirements")
        if not isinstance(requirements, list):
            raise ValueError("B-roll plan media_requirements must be an array")
        seen: set[str] = set()
        for requirement in requirements:
            event_id = requirement.get("event_id") if isinstance(requirement, dict) else None
            if event_id not in batch_event_ids:
                raise ValueError("B-roll plan event_id must reference the saved script")
            if event_id in seen:
                raise ValueError("B-roll plan event_id must be unique")
            if not isinstance(requirement.get("required"), bool):
                raise ValueError("B-roll plan required must be boolean")
            seen.add(event_id)
        return requirements, seen

    requirements: list[dict[str, Any]] = []
    seen: set[str] = set()
    for batch in _planning_event_batches(events):
        batch_event_ids = {item["event_id"] for item in batch}
        batch_requirements, batch_seen = await request_plan(
            batch,
            "判断输入 events 中每个语义事件是否需要 B-roll。必须为每一个 event_id 输出且仅输出一条 media_requirements；"
            "不需要补充画面的事件也必须输出 required=false。dialogue 输出空字符串，segments、selections 和 "
            "blueprint_mappings 输出空数组。只输出符合 Recipe output_schema 的 JSON。",
        )
        if batch_seen != batch_event_ids:
            missing = [item["event_id"] for item in batch if item["event_id"] not in batch_seen]
            batch_requirements, batch_seen = await request_plan(
                batch,
                "上一次 B-roll 规划遗漏了事件。请重新输出当前 events 的完整规划，每个 event_id 只出现一次；"
                f"遗漏的 event_id：{json.dumps(missing, ensure_ascii=False)}。不需要补充画面的事件也必须输出 required=false。"
                "dialogue 输出空字符串，其他脚本数组字段输出空数组。只输出符合 Recipe output_schema 的 JSON。",
            )
        if batch_seen != batch_event_ids:
            raise ValueError("B-roll plan must decide every saved script event")
        requirements.extend(batch_requirements)
        seen.update(batch_seen)
    if seen != event_ids:
        raise ValueError("B-roll plan must decide every saved script event")
    # The model decides only whether an existing event needs supporting media.
    # Every script field remains byte-for-byte sourced from the immutable saved
    # revision, so creative output cannot silently rewrite dialogue or timing.
    return {**script, "media_requirements": requirements}
