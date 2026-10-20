"""Preview corrected source words against planned dialogue without inventing time."""

from __future__ import annotations

import unicodedata
from typing import Any


def _spoken(value: str) -> str:
    return "".join(
        character for character in unicodedata.normalize("NFKC", value).casefold()
        if character.isalnum()
    )


def preview_recognized_words_to_plan_cues(
    words: list[dict[str, Any]],
    turns: list[dict[str, str]],
    source_duration_us: int,
    source_in_us: int = 0,
    source_out_us: int | None = None,
) -> list[dict[str, Any]]:
    """Return sentence envelopes and their exact recognized source words.

    The text comparison ignores punctuation and whitespace only. A recognized
    token that spans two planned turns must be split and reviewed by a person.
    """
    if not words or not turns:
        raise ValueError("recognized words and planned dialogue are required")
    if type(source_duration_us) is not int or source_duration_us <= 0:
        raise ValueError("invalid source duration")
    end_bound = source_duration_us if source_out_us is None else source_out_us
    if (type(source_in_us) is not int or type(end_bound) is not int
            or source_in_us < 0 or end_bound > source_duration_us
            or end_bound <= source_in_us):
        raise ValueError("invalid source range")
    previous_start = previous_end = -1
    for index, word in enumerate(words):
        start, end, content = word.get("start_us"), word.get("end_us"), word.get("text")
        if (not isinstance(content, str) or not content.strip()
                or type(start) is not int or type(end) is not int
                or start < source_in_us or end > end_bound or end <= start):
            raise ValueError(f"recognized word {index + 1} outside source range")
        if start < previous_start or end < previous_end:
            raise ValueError(f"recognized word {index + 1} is out of order")
        previous_start, previous_end = start, end

    rows: list[dict[str, Any]] = []
    position = 0
    for index, turn in enumerate(turns):
        expected = _spoken(turn.get("text", ""))
        if not expected or not turn.get("id"):
            raise ValueError(f"planned turn {index + 1} has no spoken text or ID")
        first = position
        matched = ""
        while position < len(words) and len(matched) < len(expected):
            token = _spoken(words[position]["text"])
            candidate = matched + token
            if token and not expected.startswith(candidate):
                remaining = expected[len(matched):]
                if remaining and token.startswith(remaining):
                    raise ValueError(f"recognized word {position + 1} crosses turn boundary")
                raise ValueError(f"planned turn {index + 1} differs from recognized word {position + 1}")
            matched = candidate
            position += 1
        if matched != expected:
            raise ValueError(f"planned turn {index + 1} is missing recognized words")
        while position < len(words) and not _spoken(words[position]["text"]):
            position += 1
        selected = words[first:position]
        start_us = selected[0]["start_us"] // 1000 * 1000
        end_us = (selected[-1]["end_us"] + 999) // 1000 * 1000
        if start_us < source_in_us or end_us > end_bound:
            raise ValueError(f"planned turn {index + 1} cannot fit SRT millisecond bounds")
        rows.append({
            "turn_id": turn["id"], "text": turn["text"],
            "source_start_us": start_us, "source_end_us": end_us,
            "words": [dict(word) for word in selected],
        })
    if position != len(words):
        raise ValueError("extra recognized words have no planned turn")
    return rows
