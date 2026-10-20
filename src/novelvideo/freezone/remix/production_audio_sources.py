"""Pure authoring checks for a production plan's intended audio sources."""

from __future__ import annotations

SPEECH_SOURCE_TIMING_GUIDANCE = (
    "先确定发声素材的独立使用范围，再指定由它承载的台词。"
    "尚未确定时间的台词通过 speech_placement_id 引用发声素材；"
    "发声素材自身不得直接或经 selection、其他 placement 的时间锚点反过来依赖这些台词。"
    "发声素材可按所属 beat 边界、段内 offset 或已有明确时间定位；"
    "同一段内有多份素材时分别安排范围，不全部铺满整段。"
    "字幕、图形及不负责这些台词发声的画面可以跟随台词或文字范围。"
    "已有人工时间及真实语音校准按保存内容保留，不重新估计。"
    "无台词但需要环境声或动作声的画面可以保留原声，无需新建音轨。"
)


class SpeechSourceTimingError(ValueError):
    """A speech carrier has an invalid relation to its authored speech beat."""

    def __init__(self, *, placement_index: int, placement_id: str,
                 turn_id: str | None = None, edge: str | None = None,
                 beat_id: str | None = None, code: str = "speech_source_timing_cycle") -> None:
        if code == "speech_source_timing_invalid":
            self.issue = {
                "code": code,
                "path": f"/placements/{placement_index}",
                "placement_id": placement_id,
                "beat_id": beat_id,
            }
            super().__init__(
                f"speech source placement {placement_id} must overlap the current beat {beat_id}"
            )
            return
        self.issue = {
            "code": "speech_source_timing_cycle",
            "path": f"/placements/{placement_index}/{edge}",
            "placement_id": placement_id,
            "turn_id": turn_id,
        }
        super().__init__(f"speech source placement {placement_id} has circular turn timing")

def planned_speech_sources(plan: dict) -> dict[str, list[str]]:
    """Group turn IDs by planned placement in authored dialogue order."""
    sources: dict[str, list[str]] = {}
    for beat in plan.get("beats", []):
        for turn in beat.get("turns", []):
            placement_id = turn.get("speech_placement_id")
            if placement_id is not None:
                sources.setdefault(placement_id, []).append(turn["id"])
    return sources


def _beat_ranges(plan: dict) -> dict[str, tuple[int, int]]:
    cursor = 0
    ranges: dict[str, tuple[int, int]] = {}
    for beat in plan.get("beats", []):
        end = cursor + beat["duration_frames"]
        ranges[beat["id"]] = (cursor, end)
        cursor = end
    return ranges


def _anchor_frame(anchor: dict, *, beat_ranges: dict[str, tuple[int, int]],
                  placements: dict[str, dict], selections: dict[str, dict],
                  turns: dict[str, tuple[dict, str]], visiting: set[tuple[str, str]]) -> int | None:
    kind = anchor["kind"]
    edge = anchor.get("edge")
    offset = anchor.get("offset_frames", 0)
    if kind == "program_frame":
        return anchor["frame"]
    if kind == "beat":
        bounds = beat_ranges.get(anchor.get("ref_id"))
        return bounds[0 if edge == "start" else 1] + offset if bounds else None
    if kind == "turn":
        target = turns.get(anchor.get("ref_id"))
        if target is None:
            return None
        turn, beat_id = target
        local = turn.get("start_frame" if edge == "start" else "end_frame")
        return beat_ranges[beat_id][0] + local + offset if local is not None else None
    if kind in {"placement", "selection"}:
        rows = placements if kind == "placement" else selections
        ref_id = anchor.get("ref_id")
        row = rows.get(ref_id)
        key = (kind, ref_id)
        if row is None or key in visiting:
            return None
        nested = _anchor_frame(row[edge], beat_ranges=beat_ranges, placements=placements,
                               selections=selections, turns=turns, visiting=visiting | {key})
        return nested + offset if nested is not None else None
    return None


def _anchor_dependent_turn(anchor: dict, *, turn_ids: set[str], placements: dict[str, dict],
                           selections: dict[str, dict], turns: dict[str, tuple[dict, str]],
                           visiting: set[tuple[str, str]]) -> str | None:
    kind, ref_id = anchor["kind"], anchor.get("ref_id")
    if kind in {"turn", "text"}:
        if ref_id not in turn_ids:
            return None
        turn = turns[ref_id][0]
        return ref_id if turn.get("start_frame") is None or turn.get("end_frame") is None else None
    if kind not in {"placement", "selection"}:
        return None
    key = (kind, ref_id)
    if key in visiting:
        return None
    row = (placements if kind == "placement" else selections).get(ref_id)
    return (_anchor_dependent_turn(row[anchor["edge"]], turn_ids=turn_ids,
                                   placements=placements, selections=selections, turns=turns,
                                   visiting=visiting | {key}) if row else None)


def validate_plan_audio_sources(plan: dict) -> None:
    """Reject impossible or contradictory source intent in a validated plan."""
    placements = {row["id"]: row for row in plan.get("placements", [])}
    placement_indices = {row["id"]: index for index, row in enumerate(plan.get("placements", []))}
    resources = {row["id"]: row for row in plan.get("resources", [])}
    selections = {row["id"]: row for row in plan.get("selections", [])}
    beat_ranges = _beat_ranges(plan)
    turns = {turn["id"]: (turn, beat["id"])
             for beat in plan.get("beats", []) for turn in beat.get("turns", [])}
    takes_by_id = {row["id"]: row for row in plan.get("speech_takes", [])}

    for placement in placements.values():
        source_audio = placement.get("source_audio")
        if source_audio is not None and placement["kind"] != "visual":
            raise ValueError(f"source_audio applies only to visual placement {placement['id']}")
        if source_audio is True:
            resource = resources.get(placement.get("resource_id"))
            if resource is not None and (resource["media_kind"] != "video" or not resource["has_audio"]):
                raise ValueError(f"source_audio requires audible video at placement {placement['id']}")
            take = takes_by_id.get(placement.get("speech_take_id"))
            if take is not None and take["origin_placement_id"] != placement["id"]:
                raise ValueError(f"speech take window {placement['id']} cannot add source_audio")

    groups = planned_speech_sources(plan)
    for placement_id, turn_ids in groups.items():
        placement = placements.get(placement_id)
        if placement is None:
            raise ValueError(f"speech source placement {placement_id} is missing")
        if placement["kind"] not in {"visual", "audio"}:
            raise ValueError(f"speech source placement {placement_id} must be visual or speech audio")
        if placement["kind"] == "audio" and placement["role"] not in {"speech", "narration"}:
            raise ValueError(f"speech source audio placement {placement_id} needs speech or narration role")
        if placement.get("loop"):
            raise ValueError(f"speech source placement {placement_id} must be nonloop")
        if placement["kind"] == "visual" and placement.get("source_audio") is False:
            raise ValueError(f"speech source placement {placement_id} is muted")
        if placement.get("turn_id") is not None and (len(turn_ids) != 1 or turn_ids[0] != placement["turn_id"]):
            raise ValueError(f"speech source placement {placement_id} belongs to another turn, not the current turn")
        resource = resources.get(placement.get("resource_id"))
        if resource is not None and (
            placement["kind"] == "visual" and (resource["media_kind"] != "video" or not resource["has_audio"])
            or placement["kind"] == "audio" and (
                resource["media_kind"] not in {"video", "audio"}
                or resource["media_kind"] == "video" and not resource["has_audio"]
            )
        ):
            raise ValueError(f"speech source placement {placement_id} requires audible video or audio media")

        beat_ids = {turns[turn_id][1] for turn_id in turn_ids}
        if len(beat_ids) != 1:
            raise ValueError(f"speech source placement {placement_id} must belong to one beat")
        beat_id = next(iter(beat_ids))
        ordered = [turn["id"] for beat in plan["beats"] if beat["id"] == beat_id
                   for turn in beat.get("turns", [])]
        start_index = ordered.index(turn_ids[0])
        if ordered[start_index:start_index + len(turn_ids)] != turn_ids:
            raise ValueError(f"speech source placement {placement_id} needs contiguous turns")
        for edge in ("start", "end"):
            dependent_turn = _anchor_dependent_turn(
                placement[edge], turn_ids=set(turn_ids), placements=placements,
                selections=selections, turns=turns, visiting=set(),
            )
            if dependent_turn is not None:
                raise SpeechSourceTimingError(
                    placement_index=placement_indices[placement_id], placement_id=placement_id,
                    turn_id=dependent_turn, edge=edge,
                )
        start = _anchor_frame(placement["start"], beat_ranges=beat_ranges, placements=placements,
                              selections=selections, turns=turns, visiting=set())
        end = _anchor_frame(placement["end"], beat_ranges=beat_ranges, placements=placements,
                            selections=selections, turns=turns, visiting=set())
        beat_start, beat_end = beat_ranges[beat_id]
        if start is not None and end is not None and (start >= beat_end or end <= beat_start):
            raise SpeechSourceTimingError(
                placement_index=placement_indices[placement_id], placement_id=placement_id,
                beat_id=beat_id, code="speech_source_timing_invalid",
            )
        if start is not None and end is not None:
            for turn_id in turn_ids:
                turn = turns[turn_id][0]
                turn_start, turn_end = turn.get("start_frame"), turn.get("end_frame")
                if turn_start is not None and turn_end is not None and (
                    beat_start + turn_start < start or beat_start + turn_end > end
                ):
                    raise ValueError(f"turn {turn_id} lies outside speech source interval {placement_id}")
