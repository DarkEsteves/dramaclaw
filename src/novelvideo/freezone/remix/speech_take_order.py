"""Validate explicit playback instances against the authored dialogue order."""
from typing import Any


class SpeechTakeOrderError(ValueError):
    """A speech take owns turns that cannot be played as one continuous take."""

    def __init__(self, code: str, message: str, *, path: str,
                 take_id: str | None = None, beat_id: str | None = None,
                 turn_id: str | None = None) -> None:
        self.code = code
        self.path = path
        self.take_id = take_id
        self.beat_id = beat_id
        self.turn_id = turn_id
        super().__init__(message)


def _index(items: list[Any], predicate) -> int:
    return next((index for index, item in enumerate(items) if predicate(item)), 0)


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
                    take_index = _index(plan.speech_takes, lambda row: row.id == owner)
                    raise SpeechTakeOrderError(
                        "speech_take_interleaved",
                        f"speech take {owner} is interleaved; assign separate playback instances",
                        path=f"/speech_takes/{take_index}", take_id=owner,
                        beat_id=beat.id, turn_id=turn.id,
                    )
                seen.add(owner)
                ordered.append(owner)
            previous = owner
        authored = [take.id for take in takes if take.turn_ids]
        if ordered != authored:
            misplaced_id = next((take_id for take_id, expected in zip(ordered, authored)
                                 if take_id != expected), ordered[-1] if ordered else None)
            first_turn = (next((turn_id for turn_id in owners if owners[turn_id] == misplaced_id), None)
                          if misplaced_id is not None else None)
            raise SpeechTakeOrderError(
                "speech_take_order_conflict",
                f"speech take order in beat {beat.id} differs from authored turn order",
                path=f"/speech_takes/{_index(plan.speech_takes, lambda row: row.id == misplaced_id)}",
                take_id=misplaced_id, beat_id=beat.id, turn_id=first_turn,
            )
        positions = {turn.id: index for index, turn in enumerate(beat.turns)}
        for take in takes:
            if take.turn_ids != sorted(take.turn_ids, key=lambda turn_id: positions[turn_id]):
                raise SpeechTakeOrderError(
                    "speech_take_turn_order_conflict",
                    f"speech take {take.id} turn order differs from authored turn order",
                    path=f"/speech_takes/{_index(plan.speech_takes, lambda row: row.id == take.id)}",
                    take_id=take.id, beat_id=beat.id,
                    turn_id=next((turn_id for turn_id, other in zip(
                        take.turn_ids, sorted(take.turn_ids, key=lambda item: positions[item]))
                        if turn_id != other), None),
                )
