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
_INLINE_SPEAKER = re.compile(r"[。！？!?]\s*(?:男|女|男生|女生|人物\d+|角色\d+|旁白|口播)[：:]")


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
    return {"dialogue": dialogue, "selections": selections, "segments": []}


def _has_one_speaker_turn_per_line(dialogue: str) -> bool:
    lines = [line for line in dialogue.splitlines() if line.strip()]
    return bool(lines) and all(
        _SPEAKER_LINE.match(line) and not _INLINE_SPEAKER.search(line)
        for line in lines
    )


async def generate_script_draft(*, blueprint: dict[str, Any], topic: str, username: str) -> dict[str, Any]:
    recipe = get_recipe_for_runtime(username=username, recipe_id="reference-remix-script")
    source = {key: blueprint.get(key) for key in (
        "narrative_role", "events", "preserve_replace_rules", "observations", "inferences", "user_choices"
    )}
    prompt = (
        "按给定参考蓝图和新主题生成可编辑的改编台词及语义关键句。"
        "dialogue 必须按说话轮次写，每句独立一行，格式为“角色：台词”；"
        "参考片有男女对话时分别标“男：”“女：”，保持角色和轮换顺序一致。"
        "角色标签只表示说话者，不决定用口播视频还是独立配音。"
        "只输出符合 Recipe output_schema 的 JSON。"
    )
    for attempt in range(2):
        content = await generate_recipe_text(
            username=username,
            recipe_id="reference-remix-script",
            node_kind="text",
            node_prompt=prompt if attempt == 0 else prompt + "上一版把说话者的台词写成一整段；请重新生成完整 JSON，每句都按“角色：台词”独立换行。",
            user_goal=topic,
            upstream_text=json.dumps(source, ensure_ascii=False)[:12_000],
        )
        draft = draft_from_recipe_output(validate_structured_recipe_output(recipe, content))
        if _has_one_speaker_turn_per_line(draft["dialogue"]):
            return draft
    raise ValueError("生成台词格式无效：每句都应独立一行并写成“角色：台词”。请重试。")
