"""Generate an editable script draft from one saved reference blueprint."""

from __future__ import annotations

import json
import re
from typing import Any

from novelvideo.freezone.recipe_runtime import (
    generate_recipe_text,
    get_recipe_for_runtime,
    validate_structured_recipe_output,
)


_SPEAKER_LINE = re.compile(r"^\s*([^：:\n]{1,24})[：:]\s*(\S.*)$")


def performance_segments_from_dialogue(dialogue: str) -> list[dict[str, Any]]:
    """Seed editable performance takes from spoken lines, independent of B-roll."""
    lines = dialogue.splitlines(keepends=True)
    nonblank = [line.rstrip("\r\n") for line in lines if line.strip()]
    matches = [_SPEAKER_LINE.match(line) for line in nonblank]
    if not matches or not all(matches):
        return [{
            "segment_id": "segment-1", "start_char": 0,
            "end_char": len(dialogue), "speaker": "旁白", "source": "voiceover",
        }]
    segments: list[dict[str, Any]] = []
    offset = 0
    for line in lines:
        if line.strip():
            content = line.rstrip("\r\n")
            match = _SPEAKER_LINE.match(content)
            assert match is not None
            segments.append({
                "segment_id": f"segment-{len(segments) + 1}",
                "start_char": offset + match.start(2),
                "end_char": offset + match.end(2),
                "speaker": match.group(1).strip(),
                "source": "speaking_video",
            })
        offset += len(line)
    return segments


def draft_from_recipe_output(payload: dict[str, Any]) -> dict[str, Any]:
    """Resolve model-proposed quotes against real dialogue without trusting offsets."""
    dialogue = payload.get("dialogue")
    proposed = payload.get("selections")
    if not isinstance(dialogue, str) or not dialogue.strip() or not isinstance(proposed, list) or not proposed:
        raise ValueError("生成脚本缺少有效台词或关键句")
    if len(dialogue) > 8_000 or len(proposed) > 24:
        raise ValueError("生成脚本超出首版长度限制")
    selections = []
    used: list[tuple[int, int]] = []
    for index, item in enumerate(proposed, start=1):
        if not isinstance(item, dict):
            raise ValueError("生成脚本的关键句格式无效")
        label, quote = item.get("label"), item.get("quote")
        if not isinstance(label, str) or not label.strip() or not isinstance(quote, str) or not quote.strip():
            raise ValueError("生成脚本的关键句格式无效")
        occurrences = []
        cursor = 0
        while True:
            position = dialogue.find(quote, cursor)
            if position < 0:
                break
            occurrences.append(position)
            cursor = position + 1
        choice = item.get("occurrence")
        if choice is None and len(occurrences) != 1:
            raise ValueError("生成脚本的关键句不存在或重复，无法确定位置")
        if choice is not None and (type(choice) is not int or choice < 1 or choice > len(occurrences)):
            raise ValueError("生成脚本的关键句位置无效")
        start = occurrences[(choice or 1) - 1]
        end = start + len(quote)
        if any(start < previous_end and end > previous_start for previous_start, previous_end in used):
            raise ValueError("生成脚本的关键句区间重叠")
        used.append((start, end))
        selections.append({"id": f"generated-{index}", "label": label.strip(), "start": start, "end": end})
    return {"dialogue": dialogue, "selections": selections, "segments": performance_segments_from_dialogue(dialogue)}


async def generate_script_draft(*, blueprint: dict[str, Any], topic: str, username: str) -> dict[str, Any]:
    recipe = get_recipe_for_runtime(username=username, recipe_id="reference-remix-script")
    source = {key: blueprint.get(key) for key in (
        "narrative_role", "events", "preserve_replace_rules", "observations", "inferences", "user_choices"
    )}
    content = await generate_recipe_text(
        username=username,
        recipe_id="reference-remix-script",
        node_kind="text",
        node_prompt="按给定参考蓝图和新主题生成一份可编辑的改编口播台词及语义关键句。只输出符合 Recipe output_schema 的 JSON。",
        user_goal=topic,
        upstream_text=json.dumps(source, ensure_ascii=False)[:12_000],
    )
    return draft_from_recipe_output(validate_structured_recipe_output(recipe, content))
