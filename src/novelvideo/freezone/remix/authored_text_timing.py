"""Validate author text positions without requiring generated media."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from fractions import Fraction
from typing import Any


def checked_offset(text: str, char_offset: int) -> int:
    if type(char_offset) is not int or not 0 <= char_offset <= len(text):
        raise ValueError("char_offset is outside the turn text")
    return char_offset


def make_text_anchor(
    text: str, turn_id: str, char_offset: int, *, affinity: str = "after", offset_frames: int = 0,
) -> dict[str, Any]:
    checked_offset(text, char_offset)
    if not isinstance(turn_id, str) or not turn_id or affinity not in {"before", "after"}:
        raise ValueError("text anchor needs a turn and before/after affinity")
    if type(offset_frames) is not int:
        raise ValueError("text anchor offset_frames must be an integer")
    return {"kind": "text", "ref_id": turn_id, "char_offset": char_offset,
            "text_hash": sha256(text.encode("utf-8")).hexdigest(),
            "affinity": affinity, "offset_frames": offset_frames}


def materialize_text_anchors(payload: dict[str, Any]) -> dict[str, Any]:
    """Stamp new text anchors from this exact author text; reject supplied stale hashes."""
    plan = deepcopy(payload)
    turns = {turn["id"]: turn["text"] for beat in plan.get("beats", []) for turn in beat.get("turns", [])}
    for row in [*plan.get("selections", []), *plan.get("placements", [])]:
        for edge in ("start", "end"):
            anchor = row.get(edge)
            if not isinstance(anchor, dict) or anchor.get("kind") != "text":
                continue
            turn_id = anchor.get("ref_id")
            if turn_id not in turns:
                raise ValueError(f"text anchor references missing turn {turn_id}")
            stamped = make_text_anchor(turns[turn_id], turn_id, anchor.get("char_offset"),
                                       affinity=anchor.get("affinity", "after"),
                                       offset_frames=anchor.get("offset_frames", 0))
            if "text_hash" in anchor and anchor["text_hash"] != stamped["text_hash"]:
                raise ValueError(f"text_hash differs from current turn {turn_id}")
            anchor.update(stamped)
    return plan


def locate_text_boundary(
    *, text: str, char_offset: int, affinity: str, words: list[dict[str, Any]],
    cue_start_us: int | None = None, cue_end_us: int | None = None,
    cue_quality: str = "manual",
) -> dict[str, Any]:
    """Locate one author-text boundary using only supplied speech evidence.

    Word edges are measured; a point inside a word is merely an estimate for
    preview. A sentence cue proves only its outer boundaries.
    """
    checked_offset(text, char_offset)
    if affinity not in {"before", "after"}:
        raise ValueError("affinity must be before or after")

    def result(source_us: int | None, quality: str | None,
               word_ids: list[str], issue: str | None = None) -> dict[str, Any]:
        return {"source_us": source_us, "quality": quality,
                "source_word_ids": word_ids, "issue_code": issue}

    if cue_quality not in {"manual", "measured"}:
        raise ValueError("cue_quality must be manual or measured")
    if char_offset == 0 and cue_start_us is not None:
        return result(cue_start_us, cue_quality, [])
    if char_offset == len(text) and cue_end_us is not None:
        return result(cue_end_us, cue_quality, [])

    usable: list[dict[str, Any]] = []
    for word in words:
        start, end = word.get("start_char"), word.get("end_char")
        source_start, source_end = word.get("start_us"), word.get("end_us")
        if (type(start) is int and type(end) is int and 0 <= start < end <= len(text)
                and type(source_start) is int and type(source_end) is int
                and 0 <= source_start < source_end and isinstance(word.get("id"), str)):
            usable.append(word)

    if affinity == "before":
        exact = [word for word in usable if word["end_char"] == char_offset]
        field = "end_us"
    else:
        exact = [word for word in usable if word["start_char"] == char_offset]
        field = "start_us"
    if len(exact) == 1:
        return result(exact[0][field], "measured", [exact[0]["id"]])
    if len(exact) > 1:
        return result(None, None, [], "ambiguous_word_evidence")

    interior = [word for word in usable
                if word["start_char"] < char_offset < word["end_char"]]
    if len(interior) == 1:
        word = interior[0]
        fraction = Fraction(char_offset - word["start_char"],
                            word["end_char"] - word["start_char"])
        source_us = word["start_us"] + round((word["end_us"] - word["start_us"]) * fraction)
        return result(source_us, "estimated", [word["id"]])
    return result(None, None, [],
                  "ambiguous_word_evidence" if interior else "missing_word_evidence")


def rebase_text_anchor(anchor: dict[str, Any], *, old_text: str, new_text: str) -> dict[str, Any]:
    """Keep a text boundary only when unchanged context proves its new offset."""
    if anchor.get("kind") != "text":
        raise ValueError("rebase_text_anchor requires a text anchor")
    offset = checked_offset(old_text, anchor.get("char_offset"))
    if anchor.get("text_hash") != sha256(old_text.encode("utf-8")).hexdigest():
        return {"anchor": deepcopy(anchor), "status": "needs_rebind"}
    if old_text == new_text:
        return {"anchor": deepcopy(anchor), "status": "preserved"}

    prefix = 0
    while prefix < min(len(old_text), len(new_text)) and old_text[prefix] == new_text[prefix]:
        prefix += 1
    suffix = 0
    while (suffix < min(len(old_text) - prefix, len(new_text) - prefix)
           and old_text[len(old_text) - suffix - 1] == new_text[len(new_text) - suffix - 1]):
        suffix += 1

    if offset < prefix and new_text.count(old_text[:prefix]) == 1:
        new_offset = offset
    elif offset > len(old_text) - suffix and suffix and new_text.count(old_text[-suffix:]) == 1:
        new_offset = offset + len(new_text) - len(old_text)
    else:
        return {"anchor": deepcopy(anchor), "status": "needs_rebind"}
    updated = deepcopy(anchor)
    updated["char_offset"] = new_offset
    updated["text_hash"] = sha256(new_text.encode("utf-8")).hexdigest()
    return {"anchor": updated, "status": "preserved"}
