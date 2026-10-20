"""Resolve generated speech content from one saved production script."""

from __future__ import annotations

from typing import Any


def generation_dialogue(plan: dict[str, Any], turn_ids: list[str]) -> list[dict[str, str]]:
    if not isinstance(turn_ids, list) or any(not isinstance(value, str) for value in turn_ids):
        raise ValueError("turn IDs must be an ordered list")
    if len(set(turn_ids)) != len(turn_ids):
        raise ValueError("turn IDs cannot repeat in one generation step")
    turns = [turn for beat in plan.get("beats", []) for turn in beat.get("turns", [])]
    by_id = {turn["id"]: turn for turn in turns}
    if len(by_id) != len(turns) or any(turn_id not in by_id for turn_id in turn_ids):
        raise ValueError("turn ID does not belong to this saved plan")
    order = {turn["id"]: index for index, turn in enumerate(turns)}
    if [order[turn_id] for turn_id in turn_ids] != sorted(order[turn_id] for turn_id in turn_ids):
        raise ValueError("turn IDs must retain the saved script order")
    return [{"turn_id": turn_id, "speaker": by_id[turn_id].get("speaker", ""),
             "text": by_id[turn_id]["text"]} for turn_id in turn_ids]
