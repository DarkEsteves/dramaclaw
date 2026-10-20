"""Validate explicit playback instances against the authored dialogue order."""
from typing import Any


def validate_speech_take_order(plan: Any) -> None:
    for beat in plan.beats:
        takes = [take for take in plan.speech_takes if take.beat_id == beat.id]
        owners = {turn_id: take.id for take in takes for turn_id in take.turn_ids}
        ordered: list[str] = []
        seen: set[str] = set()
        previous = None
        for turn in beat.turns:
            owner = owners.get(turn.id)
            if owner is None:
                continue
            if owner != previous:
                if owner in seen:
                    raise ValueError(f"speech take {owner} is interleaved; assign separate playback instances")
                seen.add(owner)
                ordered.append(owner)
            previous = owner
        if ordered != [take.id for take in takes if take.turn_ids]:
            raise ValueError(f"speech take order in beat {beat.id} differs from authored turn order")
        positions = {turn.id: index for index, turn in enumerate(beat.turns)}
        for take in takes:
            if take.turn_ids != sorted(take.turn_ids, key=lambda turn_id: positions[turn_id]):
                raise ValueError(f"speech take {take.id} turn order differs from authored turn order")
