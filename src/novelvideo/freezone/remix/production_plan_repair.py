"""Constrained, in-memory repair of one failed native plan draft."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

from jsonschema import Draft202012Validator

from .plan_issues import issue_error_message, issue_from_pydantic_row


_PATH = re.compile(r"/(?:beats|placements|selections|output)(?:/[A-Za-z0-9_-]+)*\Z")
_HOST_FIELDS = {"resources", "provenance", "speech_takes", "speech_cues",
                "speech_timing", "resource_id", "speech_take_id",
                "words", "text_hash", "source_in_us", "source_out_us"}
# A carrier rule failed; the bounded correction is choosing which authored
# turn uses which planned source, never editing the carrier's media or text.
_SPEECH_SOURCE_BINDING_CODES = frozenset({
    "speech_source_spans_beats",
    "speech_source_turns_not_contiguous",
    "speech_source_turn_binding_mismatch",
    "speech_source_missing",
})
# A carrier's own interval contradicts its turns/beat; only its anchors move.
_SPEECH_SOURCE_ANCHOR_CODES = frozenset({
    "speech_source_timing_invalid",
    "speech_source_interval_excludes_turn",
})


def plan_repair_issues(error: BaseException) -> list[dict[str, Any]]:
    """Extract native validation locations without parsing rendered error text."""
    from jsonschema import ValidationError as JsonSchemaValidationError
    from pydantic import ValidationError as PydanticValidationError

    current: BaseException | None = error
    while current is not None:
        if isinstance(current, PydanticValidationError):
            return [_pydantic_row_issue(row) for row in current.errors()]
        if isinstance(current, JsonSchemaValidationError):
            child = next((item for item in current.context if item.path), None)
            location = [*current.path, *(child.path if child is not None else [])]
            return [{"path": "/" + "/".join(str(part) for part in location),
                     "code": "schema_validation_failed",
                     "message": child.message if child is not None else current.message}]
        current = current.__cause__
    try:
        rows = json.loads(str(error))
    except (TypeError, ValueError):
        return []
    return [row for row in rows if isinstance(row, dict) and isinstance(row.get("code"), str)] \
        if isinstance(rows, list) else []


def _pydantic_row_issue(row: dict[str, Any]) -> dict[str, Any]:
    """Prefer the rule's own IDs; fall back to the rendered field path."""
    issue: dict[str, Any] = issue_from_pydantic_row(row)
    path = issue.get("path") or "/"
    if path.endswith("/graphic") and "emphasis" in str(row.get("msg") or "").lower():
        issue["path"] = path + "/emphasis"
    message = issue_error_message(row)
    if message is not None:
        issue["message"] = message
    return issue


def _eligible_speech_turn(draft: dict[str, Any], turn: Any) -> bool:
    """Only an unconfirmed, planned source choice may be reassigned."""
    if not isinstance(turn, dict) or turn.get("speech_timing") or turn.get("words"):
        return False
    if turn.get("timing_basis", "estimated") != "estimated":
        return False
    turn_id = turn.get("id")
    return not (any(turn_id in take.get("turn_ids", []) for take in draft.get("speech_takes", []))
                or any(cue.get("turn_id") == turn_id for cue in draft.get("speech_cues", [])))


def _turn_binding_path(draft: dict[str, Any], turn_id: Any) -> str | None:
    if not isinstance(turn_id, str):
        return None
    for beat_index, beat in enumerate(draft.get("beats", [])):
        if not isinstance(beat, dict):
            continue
        for turn_index, turn in enumerate(beat.get("turns", [])):
            if isinstance(turn, dict) and turn.get("id") == turn_id:
                return f"/beats/{beat_index}/turns/{turn_index}/speech_placement_id"
    return None


def _speech_binding_paths(draft: dict[str, Any], issue: dict[str, Any]) -> set[str]:
    """Open the source choice of every eligible turn the rule implicates.

    A carrier may only be re-bound by re-pointing authored turns: the carrier's
    own media, text and host bindings stay untouched. Only turns whose source
    is still an unconfirmed estimate are eligible, so confirmed assets cannot
    be silently re-dubbed by a repair.
    """
    placement_id, beat_id = issue.get("placement_id"), issue.get("beat_id")
    wanted = {beat_id} if isinstance(beat_id, str) else set()
    if isinstance(placement_id, str):
        wanted.update(beat.get("id") for beat in draft.get("beats", [])
                      if any(turn.get("speech_placement_id") == placement_id
                             for turn in beat.get("turns", []) if isinstance(turn, dict)))
    candidates: set[str] = set()
    reported = issue.get("path")
    if isinstance(reported, str) and reported.endswith("/speech_placement_id"):
        candidates.add(reported)
    turn_path = _turn_binding_path(draft, issue.get("turn_id"))
    if turn_path is not None:
        candidates.add(turn_path)
    for beat_index, beat in enumerate(draft.get("beats", [])):
        if not isinstance(beat, dict) or beat.get("id") not in wanted:
            continue
        for turn_index, _turn in enumerate(beat.get("turns", [])):
            candidates.add(f"/beats/{beat_index}/turns/{turn_index}/speech_placement_id")
    paths: set[str] = set()
    for path in candidates:
        try:
            turn = _read_path(draft, path.rsplit("/", 1)[0])
        except (KeyError, IndexError, ValueError, TypeError):
            continue
        if _eligible_speech_turn(draft, turn):
            paths.add(path)
    return paths


def _digest(draft: dict[str, Any]) -> str:
    encoded = json.dumps(draft, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_path(draft: dict[str, Any], path: str) -> Any:
    value: Any = draft
    for part in path.split("/")[1:]:
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def build_plan_repair_scope(
    draft: dict[str, Any], issues: list[dict[str, Any]],
) -> dict[str, Any]:
    """Bind editable paths to a particular draft digest and source fragments."""
    paths: set[str] = set()
    repair_context: dict[str, Any] = {}
    for issue in issues:
        code = issue.get("code")
        if code in {"primary_shot_count_mismatch", "primary_shot_timing_invalid"}:
            paths.add("/placements")
        elif code == "duration_mismatch":
            paths.add("/beats")
        elif code == "output_requirement_mismatch":
            path = issue.get("path")
            if isinstance(path, str) and re.fullmatch(r"/output/[A-Za-z0-9_]+", path):
                paths.add(path)
        elif code == "locked_dialogue_changed":
            for index, _beat in enumerate(draft.get("beats", [])):
                paths.add(f"/beats/{index}/turns")
        elif code in _SPEECH_SOURCE_ANCHOR_CODES:
            placement_id = issue.get("placement_id")
            placement_index = next(
                (index for index, row in enumerate(draft.get("placements", []))
                 if isinstance(row, dict) and row.get("id") == placement_id),
                None,
            )
            if placement_index is not None:
                paths.update({f"/placements/{placement_index}/start",
                              f"/placements/{placement_index}/end"})
                repair_context = {"placement_id": placement_id, "beat_id": issue.get("beat_id")}
        elif code in _SPEECH_SOURCE_BINDING_CODES:
            binding_paths = _speech_binding_paths(draft, issue)
            if binding_paths:
                paths.update(binding_paths)
                repair_context = {"placement_id": issue.get("placement_id"),
                                  "beat_id": issue.get("beat_id"),
                                  "turn_id": issue.get("turn_id")}
        else:
            path = issue.get("path")
            if isinstance(path, str) and _PATH.fullmatch(path):
                parts = path.split("/")[1:]
                if not any(part in _HOST_FIELDS for part in parts):
                    if parts[-1] == "speech_placement_id":
                        try:
                            turn = _read_path(draft, "/" + "/".join(parts[:-1]))
                        except (KeyError, IndexError, ValueError, TypeError):
                            continue
                        if not _eligible_speech_turn(draft, turn):
                            continue
                    paths.add(path)
    # Path selection is entirely host owned. Never permit a model to repair
    # runtime bindings or add fields outside the native authoring schema.
    fragments = {}
    for path in sorted(paths):
        try:
            fragments[path] = copy.deepcopy(_read_path(draft, path))
        except (KeyError, IndexError, ValueError, TypeError):
            continue
    return {"base_digest": _digest(draft), "allowed_paths": sorted(fragments),
            "fragments": fragments, "issues": copy.deepcopy(issues),
            "repair_context": repair_context}


def plan_repair_json_schema(scope: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False,
            "properties": {
                "base_digest": {"const": scope["base_digest"]},
                "changes": {"type": "array", "minItems": 1,
                            "items": {"type": "object", "additionalProperties": False,
                                      "properties": {"path": {"enum": scope["allowed_paths"]},
                                                     "value": {}},
                                      "required": ["path", "value"]}},
            }, "required": ["base_digest", "changes"]}


def _unchanged_except(before: dict[str, Any], after: dict[str, Any], allowed: set[str]) -> bool:
    return ({key: value for key, value in before.items() if key not in allowed}
            == {key: value for key, value in after.items() if key not in allowed})


def _check_collection_repair(
    draft: dict[str, Any], path: str, value: Any, scope: dict[str, Any],
) -> None:
    """Keep valid authored content when a broad collection must be editable."""
    codes = {issue.get("code") for issue in scope["issues"]}
    if path == "/placements":
        before = draft["placements"]
        if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
            raise ValueError("repair_unrelated_field_changed")
        old_by_id = {row.get("id"): row for row in before}
        new_by_id = {row.get("id"): row for row in value}
        if len(new_by_id) != len(value) or not old_by_id.keys() <= new_by_id.keys():
            raise ValueError("repair_unrelated_field_changed")
        if [row.get("id") for row in value if row.get("id") in old_by_id] != [
            row.get("id") for row in before
        ]:
            raise ValueError("repair_unrelated_field_changed")
        count_issue = "primary_shot_count_mismatch" in codes
        timing_ids = {issue.get("entity_id") for issue in scope["issues"]
                      if issue.get("code") == "primary_shot_timing_invalid"}
        for identifier, old in old_by_id.items():
            new = new_by_id[identifier]
            may_retime = (old.get("kind") == "visual" and old.get("role") == "primary"
                          and (count_issue or identifier in timing_ids))
            if not _unchanged_except(old, new, {"start", "end"} if may_retime else set()):
                raise ValueError("repair_unrelated_field_changed")
        for identifier in new_by_id.keys() - old_by_id.keys():
            added = new_by_id[identifier]
            if not count_issue or added.get("kind") != "visual" or added.get("role") != "primary":
                raise ValueError("repair_unrelated_field_changed")
    elif path == "/beats":
        before = draft["beats"]
        if not isinstance(value, list) or len(value) != len(before):
            raise ValueError("repair_unrelated_field_changed")
        for old, new in zip(before, value):
            if not isinstance(new, dict) or not _unchanged_except(old, new, {"duration_frames"}):
                raise ValueError("repair_unrelated_field_changed")
    elif re.fullmatch(r"/beats/\d+/turns", path):
        before = _read_path(draft, path)
        if not isinstance(value, list) or len(value) != len(before):
            raise ValueError("repair_unrelated_field_changed")
        for old, new in zip(before, value):
            if not isinstance(new, dict) or not _unchanged_except(old, new, {"speaker", "text"}):
                raise ValueError("repair_unrelated_field_changed")


def apply_plan_repair(
    draft: dict[str, Any], response: dict[str, Any], *, scope: dict[str, Any],
) -> dict[str, Any]:
    """Merge only approved replacements into a copy; publication is separate."""
    if scope["base_digest"] != _digest(draft) or response.get("base_digest") != scope["base_digest"]:
        raise ValueError("repair_digest_mismatch")
    if any(change.get("path") not in scope["allowed_paths"]
           for change in response.get("changes", []) if isinstance(change, dict)):
        raise ValueError("repair_path_not_allowed")
    Draft202012Validator(plan_repair_json_schema(scope)).validate(response)
    fixed = copy.deepcopy(draft)
    for change in response["changes"]:
        path = change["path"]
        _check_collection_repair(draft, path, change["value"], scope)
        parent: Any = fixed
        parts = path.split("/")[1:]
        for part in parts[:-1]:
            parent = parent[int(part)] if isinstance(parent, list) else parent[part]
        final = parts[-1]
        if isinstance(parent, list):
            parent[int(final)] = copy.deepcopy(change["value"])
        else:
            parent[final] = copy.deepcopy(change["value"])
    return fixed
