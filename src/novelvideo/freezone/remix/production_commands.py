"""Versioned, project-scoped authoring commands for production plans."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import re
from typing import Any

from .contracts import validate_payload
from .production_plan_v2 import BeatV2, TurnV2, OutputV2, PlacementV2, ResourceV2, SelectionV2, SpeechTakeV2
from .production_command_schema import production_payload_fields, structural_command_errors
from .production_audio_sources import planned_speech_sources
from .store import RemixArtifactStore


class ProductionCommandConflict(ValueError):
    def __init__(self, message: str, *, current_revision: int | None = None) -> None:
        super().__init__(message)
        self.current_revision = current_revision


_COMMAND_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_FIELDS = production_payload_fields()
_SPEECH_SOURCE_FIELDS = {
    "kind", "role", "resource_id", "turn_id", "speech_take_id", "start", "end",
    "source_in_us", "source_out_us", "speed_num", "speed_den", "loop", "volume",
}


def validate_production_command(command: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(command, dict):
        raise ValueError("production command has unexpected fields")
    required = {"command_id", "plan_ref", "kind", "target_id", "payload"}
    missing = required - set(command)
    if missing:
        raise ValueError("production command missing required fields: " + ", ".join(sorted(missing)))
    if set(command) != required:
        raise ValueError("production command has unexpected fields: " + ", ".join(sorted(set(command) - required)))
    if not isinstance(command["command_id"], str) or not _COMMAND_ID.fullmatch(command["command_id"]):
        raise ValueError("invalid production command_id")
    ref = command["plan_ref"]
    if (not isinstance(ref, dict) or set(ref) != {"artifact_id", "revision"}
            or not isinstance(ref["artifact_id"], str)
            or type(ref["revision"]) is not int or ref["revision"] < 1):
        raise ValueError("plan_ref must be an exact saved artifact revision")
    RemixArtifactStore._validate_id(ref["artifact_id"])
    kind, target_id, payload = command["kind"], command["target_id"], command["payload"]
    if kind not in _FIELDS or not isinstance(target_id, str) or not target_id:
        raise ValueError("invalid production command kind or target_id")
    if not isinstance(payload, dict) or not payload or not set(payload) <= _FIELDS[kind]:
        unexpected = ", ".join(sorted(set(payload) - _FIELDS[kind])) if isinstance(payload, dict) else "payload is not an object"
        raise ValueError(
            f"{kind} production command payload has unexpected fields; "
            f"unexpected keys: {unexpected or '(none; payload is empty)'}; "
            f"accepted payload keys: {', '.join(sorted(_FIELDS[kind]))}"
        )
    if kind == "insert_turn":
        missing = _FIELDS[kind] - set(payload)
        if missing:
            raise ValueError(
                "insert_turn missing required payload keys: " + ", ".join(sorted(missing))
                + "; required shape: {beat_id, turn: {id, text, speaker (optional)}, index}; "
                "beat_id belongs in payload; timing_status is not a TurnV2 field"
            )
        if not isinstance(payload["beat_id"], str) or not payload["beat_id"]:
            raise ValueError("insert_turn beat_id must be a nonempty string")
        if type(payload["index"]) is not int or payload["index"] < 0:
            raise ValueError("insert_turn index must be a nonnegative integer")
        try:
            TurnV2.model_validate(payload["turn"])
        except ValueError as error:
            raise ValueError(f"insert_turn payload.turn must match TurnV2: {error}") from error
    if kind == 'set_goal' and (set(payload) != {'goal'} or not isinstance(payload['goal'], str) or not payload['goal'].strip() or len(payload['goal']) > 2000):
        raise ValueError('set_goal requires a nonempty production goal')
    if kind == "batch":
        entries = payload.get("commands")
        if not isinstance(entries, list) or not 1 <= len(entries) <= 100 or set(payload) != {"commands"}:
            raise ValueError("batch requires 1 to 100 bounded commands")
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict) or entry.get("kind") == "batch" or set(entry) != {"kind", "target_id", "payload"}:
                keys = set(entry) if isinstance(entry, dict) else set()
                expected = {"kind", "target_id", "payload"}
                raise ValueError(
                    f"batch cannot contain nested or unexpected commands; batch child {index}; "
                    f"missing keys: {', '.join(sorted(expected - keys)) or '(none)'}; "
                    f"unexpected keys: {', '.join(sorted(keys - expected)) or '(none)'}; "
                    "exact child keys: kind, payload, target_id; nested batch is forbidden"
                )
            validate_production_command({**entry, "command_id": str(index), "plan_ref": ref})
    if kind in {"bind_speech_source", "reassign_speech_source", "upsert_placement", "upsert_selection", "remove_entity", "set_output"} and set(payload) != _FIELDS[kind]:
        raise ValueError("production command payload is incomplete")
    if kind == "bind_speech_source":
        if (not isinstance(payload["placement_id"], str) or not payload["placement_id"]
                or not isinstance(payload["turn_ids"], list) or not payload["turn_ids"]
                or any(not isinstance(turn_id, str) or not turn_id for turn_id in payload["turn_ids"])
                or len(payload["turn_ids"]) != len(set(payload["turn_ids"]))):
            raise ValueError("bind_speech_source requires one placement and ordered unique turn_ids")
    if kind == "reassign_speech_source":
        if (payload["placement_id"] is not None and (not isinstance(payload["placement_id"], str) or not payload["placement_id"])
                or not isinstance(payload["turn_ids"], list) or len(payload["turn_ids"]) < 2
                or any(not isinstance(turn_id, str) or not turn_id for turn_id in payload["turn_ids"])
                or len(payload["turn_ids"]) != len(set(payload["turn_ids"]))):
            raise ValueError("reassign_speech_source requires a source or null and ordered unique shared turn_ids")
    if kind == "edit_turn" and "invalidate_speech_timing" in payload:
        if payload["invalidate_speech_timing"] is not True or not {"text", "speech_placement_id"} & set(payload):
            raise ValueError("invalidate_speech_timing requires changed text or speech source")
    if kind == "upsert_placement":
        candidate = deepcopy(payload["placement"])
        if not isinstance(candidate, dict):
            raise ValueError("placement must be an object")
        for edge in ("start", "end"):
            anchor = candidate.get(edge)
            if isinstance(anchor, dict) and anchor.get("kind") == "text" and "text_hash" not in anchor:
                # Shape-check before loading the saved turn; the real digest is
                # stamped from that turn inside the transaction below.
                anchor["text_hash"] = "0" * 64
        PlacementV2.model_validate(candidate)
        placement = payload["placement"]
        if placement["id"] != target_id:
            raise ValueError("placement id differs from target_id")
        payload = {"placement": placement}
    elif kind == "set_output":
        payload = {"output": OutputV2.model_validate(payload["output"]).model_dump(mode="json")}
    if kind == "remove_entity" and payload["entity_kind"] not in {"placement", "selection", "beat", "turn"}:
        raise ValueError("unsupported removal entity kind")
    if kind == "bind_media" and (not isinstance(payload.get("resource_id"), str) or not payload["resource_id"]):
        raise ValueError("bind_media requires a resource_id")
    if kind == "bind_media" and "invalidate_speech_timing" in payload and payload["invalidate_speech_timing"] is not True:
        raise ValueError("invalidate_speech_timing must be explicit true")
    if kind == "bind_media" and "resource" in payload:
        resource = ResourceV2.model_validate(payload["resource"]).model_dump(mode="json", exclude_none=True)
        if resource["id"] != payload["resource_id"]:
            raise ValueError("resource id differs from resource_id")
        payload = {"resource_id": payload["resource_id"], "resource": resource,
                   **({"invalidate_speech_timing": True} if payload.get("invalidate_speech_timing") else {})}
    errors = structural_command_errors(command)
    if errors:
        raise ValueError(f"{kind} production command structure is invalid: {errors[0].message}")
    return {**command, "payload": payload}


def _one(items: list[dict[str, Any]], target_id: str, label: str) -> dict[str, Any]:
    match = next((row for row in items if row["id"] == target_id), None)
    if match is None:
        raise ValueError(f"{label} {target_id} not found")
    return match


def _verified_resource(store: RemixArtifactStore, plan: dict[str, Any], resource_id: str) -> dict[str, Any]:
    resource = _one(plan["resources"], resource_id, "resource")
    path = (store.project_dir / resource["media_path"]).resolve()
    if not path.is_relative_to(store.project_dir) or not path.is_file():
        raise ValueError("bound project media is unavailable")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != resource["media_hash"]:
        raise ValueError("bound project media changed after registration")
    return resource


def _take_turn_ids(plan: dict[str, Any], turn_ids: set[str]) -> set[str]:
    affected = set(turn_ids)
    for take in plan.get("speech_takes", []):
        if affected.intersection(take["turn_ids"]):
            affected.update(take["turn_ids"])
    return affected


def _invalidate_turn_timing(plan: dict[str, Any], turn_ids: set[str]) -> None:
    turn_ids = _take_turn_ids(plan, turn_ids)
    for beat in plan["beats"]:
        for turn in beat.get("turns", []):
            if turn["id"] in turn_ids:
                turn.pop("speech_timing", None)
                turn["words"] = []
                turn["timing_basis"] = "estimated"
                turn.pop("start_frame", None)
                turn.pop("end_frame", None)
    for cue in plan.get("speech_cues", []):
        if cue["turn_id"] in turn_ids:
            cue["timing_status"] = "stale"
            cue["word_ids"] = []


def _guard_speech_evidence(plan: dict[str, Any], turn_ids: set[str], *, allow_invalidation: bool = False) -> None:
    """Refuse a generic edit that would erase accepted timing or word anchors."""
    turn_ids = _take_turn_ids(plan, turn_ids)
    turns = [turn for beat in plan["beats"] for turn in beat.get("turns", []) if turn["id"] in turn_ids]
    words = {word["id"] for turn in turns for word in turn.get("words", [])}
    words.update(word["id"] for turn in turns for word in (turn.get("speech_timing") or {}).get("words", []))
    for row in [*plan.get("selections", []), *plan.get("placements", [])]:
        if any(anchor.get("kind") == "word" and anchor.get("ref_id") in words
               for anchor in (row["start"], row["end"])):
            raise ValueError("word anchors depend on this speech timing; use the dedicated correction flow")
    cues = [cue for cue in plan.get("speech_cues", []) if cue["turn_id"] in turn_ids]
    if not allow_invalidation and (any(cue["timing_status"] == "ready" or cue.get("word_ids") for cue in cues)
            or any(turn.get("speech_timing") or turn.get("words") or turn.get("timing_basis") != "estimated"
                   for turn in turns)):
        raise ValueError("accepted speech timing would be discarded; use the dedicated correction flow")


def _materialize_speech_source(store: RemixArtifactStore, plan: dict[str, Any], *,
                               beat_id: str, placement_id: str, requested: list[str]) -> bool:
    """Create the single playback take and draft cues for one contiguous source."""
    beat = _one(plan["beats"], beat_id, "beat")
    placement = _one(plan["placements"], placement_id, "placement")
    if placement["kind"] not in {"visual", "audio"} or placement.get("loop"):
        raise ValueError("speech source must be a nonloop visual or audio placement")
    if placement["kind"] == "audio" and placement["role"] not in {"speech", "narration"}:
        raise ValueError("speech source audio placement must have speech or narration role")
    if not placement.get("resource_id"):
        raise ValueError("speech source placement has no bound resource")
    resource = _verified_resource(store, plan, placement["resource_id"])
    if resource["media_kind"] not in {"video", "audio"} or not resource["has_audio"]:
        raise ValueError("speech source requires audible project media")
    ordered_ids = [turn["id"] for turn in beat["turns"]]
    if any(turn_id not in ordered_ids for turn_id in requested):
        raise ValueError("speech source turns must belong to the target beat")
    first = ordered_ids.index(requested[0])
    if ordered_ids[first:first + len(requested)] != requested:
        raise ValueError("speech source turn_ids must be contiguous in dialogue order")
    existing = next((take for take in plan.get("speech_takes", [])
                     if take["beat_id"] == beat_id and take["origin_placement_id"] == placement_id), None)
    if existing and any(cue["take_id"] == existing["id"] and cue["timing_status"] == "ready"
                        for cue in plan.get("speech_cues", [])):
        raise ValueError("accepted speech timing requires the dedicated correction flow")
    if existing and existing["turn_ids"] == requested and existing["resource_id"] == resource["id"] and all(
        any(cue["turn_id"] == turn_id and cue["take_id"] == existing["id"]
            for cue in plan.get("speech_cues", [])) for turn_id in requested
    ):
        return False
    if existing and (existing["resource_id"] != resource["id"] or not set(existing["turn_ids"]) <= set(requested)):
        raise ValueError("speech source must include all turns already assigned to this take")
    for turn in beat["turns"]:
        if turn["id"] not in requested:
            continue
        if turn.get("speech_placement_id") not in {None, placement_id}:
            raise ValueError("turn is already assigned to another speech source")
        if any(turn["id"] in take["turn_ids"] and take is not existing for take in plan.get("speech_takes", [])):
            raise ValueError("turn is already assigned to another speech take")
    _guard_speech_evidence(plan, set(requested))
    take_id = existing["id"] if existing else f"take:{beat_id}:{placement_id}"
    if not existing and any(take["id"] == take_id for take in plan.get("speech_takes", [])):
        raise ValueError("speech take id is already in use")
    if existing:
        existing["turn_ids"] = requested
    else:
        plan.setdefault("speech_takes", []).append({
            "id": take_id, "beat_id": beat_id, "resource_id": resource["id"],
            "turn_ids": requested, "origin_placement_id": placement_id,
            "source_in_us": placement.get("source_in_us", 0), "source_out_us": placement.get("source_out_us"),
            "speed_num": placement.get("speed_num", 1), "speed_den": placement.get("speed_den", 1),
            "lead_in_frames": 0, "volume": placement.get("volume", 1),
        })
    for turn in beat["turns"]:
        if turn["id"] not in requested:
            continue
        turn["speech_placement_id"] = placement_id
        if not any(cue["turn_id"] == turn["id"] for cue in plan.get("speech_cues", [])):
            cue_id = f"cue:{turn['id']}"
            if any(cue["id"] == cue_id for cue in plan.get("speech_cues", [])):
                raise ValueError("speech cue id is already in use")
            plan.setdefault("speech_cues", []).append({
                "id": cue_id, "turn_id": turn["id"], "take_id": take_id,
                "text": turn["text"], "timing_source": "asr", "timing_status": "draft", "word_ids": [],
            })
    if placement["kind"] == "visual":
        placement["speech_take_id"] = take_id
    return True


def _reassign_draft_speech_source(store: RemixArtifactStore, plan: dict[str, Any], *,
                                  beat_id: str, requested: list[str], placement_id: str | None) -> bool:
    """Move a whole draft take while retaining accepted and anchored evidence guards."""
    beat = _one(plan["beats"], beat_id, "beat")
    take = next((row for row in plan.get("speech_takes", [])
                 if row["beat_id"] == beat_id and requested[0] in row["turn_ids"]), None)
    if take is None or len(take["turn_ids"]) < 2 or take["turn_ids"] != requested:
        raise ValueError("reassign_speech_source requires every turn of one shared take in order")
    if take["origin_placement_id"] == placement_id:
        raise ValueError("speech source is unchanged")
    ordered = [turn["id"] for turn in beat["turns"]]
    first = ordered.index(requested[0])
    if ordered[first:first + len(requested)] != requested:
        raise ValueError("shared speech turns must be contiguous in dialogue order")
    try:
        _guard_speech_evidence(plan, set(requested))
    except ValueError as exc:
        if "word anchors" in str(exc):
            raise ValueError("word anchors depend on this source; resolve them before reassignment") from exc
        raise ValueError("accepted speech timing must be resolved before reassigning source") from exc
    cues = [cue for cue in plan.get("speech_cues", []) if cue["take_id"] == take["id"]]
    if any(cue["timing_status"] == "ready" or cue.get("evidence_ref") or cue.get("word_ids")
           or cue.get("source_start_us") is not None or cue.get("source_end_us") is not None for cue in cues):
        raise ValueError("accepted speech timing must be resolved before reassigning source")
    if any(row.get("speech_take_id") == take["id"] and row["id"] != take["origin_placement_id"]
           for row in plan["placements"]):
        raise ValueError("speech take windows depend on this source")
    cue_ids = {cue["id"] for cue in cues}
    for row in [*plan["placements"], *plan.get("selections", [])]:
        if any(anchor.get("kind") == "take" and anchor.get("ref_id") == take["id"]
               or anchor.get("kind") == "cue" and anchor.get("ref_id") in cue_ids
               for anchor in (row["start"], row["end"])):
            raise ValueError("speech anchors depend on this source")
    if placement_id is not None:
        placement = _one(plan["placements"], placement_id, "placement")
        if (placement["kind"] not in {"visual", "audio"} or placement.get("loop")
                or placement["kind"] == "audio" and placement["role"] not in {"speech", "narration"}
                or placement["kind"] == "visual" and placement.get("source_audio") is False):
            raise ValueError("replacement speech source must be audible visual or speech audio")
    _invalidate_turn_timing(plan, set(requested))
    plan["speech_takes"] = [row for row in plan["speech_takes"] if row["id"] != take["id"]]
    plan["speech_cues"] = [cue for cue in plan.get("speech_cues", []) if cue["take_id"] != take["id"]]
    old_origin = _one(plan["placements"], take["origin_placement_id"], "placement")
    if old_origin.get("speech_take_id") == take["id"]:
        old_origin.pop("speech_take_id")
    for turn in beat["turns"]:
        if turn["id"] in requested:
            turn["speech_placement_id"] = placement_id
    if placement_id is None or not placement.get("resource_id"):
        return False
    planned = planned_speech_sources(plan)[placement_id]
    return _materialize_speech_source(store, plan, beat_id=beat_id, placement_id=placement_id,
                                      requested=planned)


def _apply_edit(store: RemixArtifactStore, source: dict[str, Any], command: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    plan = deepcopy(source)
    kind, target_id, payload = command["kind"], command["target_id"], dict(command["payload"])
    invalidated = ["projection", "render"]
    blockers: list[str] = []
    if kind == 'set_goal':
        plan['goal'] = payload['goal']
    elif kind.startswith("insert_"):
        field, model = ("beat", BeatV2) if kind == "insert_beat" else ("turn", TurnV2)
        if set(payload) != _FIELDS[kind]:
            raise ValueError("insert payload is incomplete")
        row = model.model_validate(payload[field]).model_dump(mode="json", exclude_none=True)
        rows = plan["beats"] if field == "beat" else _one(plan["beats"], payload["beat_id"], "beat")["turns"]
        existing = plan["beats"] if field == "beat" else [t for b in plan["beats"] for t in b["turns"]]
        if row["id"] != target_id or any(r["id"] == target_id for r in existing):
            raise ValueError("entity id differs or already exists")
        if type(payload["index"]) is not int or not 0 <= payload["index"] <= len(rows):
            raise ValueError("invalid insert index")
        rows.insert(payload["index"], row)
    elif kind.startswith("reorder_"):
        parent = plan if kind == "reorder_beats" else _one(plan["beats"], target_id, "beat")
        field = "beats" if kind == "reorder_beats" else "turns"
        rows, ids = parent[field], payload["ids"]
        if not isinstance(ids, list) or len(ids) != len(rows) or any(not isinstance(x, str) for x in ids) or len(set(ids)) != len(ids) or set(ids) != {r["id"] for r in rows}:
            raise ValueError("reorder ids must be an exact permutation")
        parent[field] = [_one(rows, x, field) for x in ids]
    elif kind == "edit_beat":
        beat = _one(plan["beats"], target_id, "beat")
        beat.update(payload)
    elif kind == "edit_speech_take":
        take = _one(plan.get("speech_takes", []), target_id, "speech take")
        updated = SpeechTakeV2.model_validate({**take, **payload}).model_dump(mode="json", exclude_none=True)
        take.update(updated)
    elif kind == "edit_turn":
        turn = _one([row for beat in plan["beats"] for row in beat.get("turns", [])], target_id, "turn")
        explicit_invalidation = payload.pop("invalidate_speech_timing", False)
        text_changed = "text" in payload and payload["text"] != turn["text"]
        source_changed = ("speech_placement_id" in payload
                          and payload["speech_placement_id"] != turn.get("speech_placement_id"))
        if explicit_invalidation and not (text_changed or source_changed):
            raise ValueError("invalidate_speech_timing requires changed text or speech source")
        old_take = next((take for take in plan.get("speech_takes", [])
                         if target_id in take["turn_ids"]), None) if source_changed else None
        if old_take and (_one(plan["placements"], old_take["origin_placement_id"], "placement")["kind"] == "audio"
                         or len(old_take["turn_ids"]) != 1 or any(
            row.get("speech_take_id") == old_take["id"] and row["id"] != old_take["origin_placement_id"]
            for row in plan["placements"]
        )):
            raise ValueError("shared speech take source changes require the dedicated correction flow")
        if old_take and any(cue["take_id"] == old_take["id"] and cue["timing_status"] == "ready"
                            for cue in plan.get("speech_cues", [])):
            raise ValueError("accepted speech timing requires the dedicated correction flow")
        if text_changed or source_changed:
            from .authored_text_timing import rebase_text_anchor

            _guard_speech_evidence(plan, {target_id}, allow_invalidation=explicit_invalidation)
            if old_take:
                plan["speech_takes"] = [take for take in plan["speech_takes"] if take is not old_take]
                plan["speech_cues"] = [cue for cue in plan.get("speech_cues", [])
                                       if cue["take_id"] != old_take["id"]]
                old_origin = _one(plan["placements"], old_take["origin_placement_id"], "placement")
                if old_origin.get("speech_take_id") == old_take["id"]:
                    old_origin.pop("speech_take_id")
            if text_changed:
                for row in [*plan.get("selections", []), *plan.get("placements", [])]:
                    for edge in ("start", "end"):
                        anchor = row[edge]
                        if anchor.get("kind") == "text" and anchor.get("ref_id") == target_id:
                            rebased = rebase_text_anchor(anchor, old_text=turn["text"],
                                                         new_text=payload["text"])
                            row[edge] = rebased["anchor"]
                            if rebased["status"] == "needs_rebind":
                                blockers.append("text_anchor_requires_rebind")
            _invalidate_turn_timing(plan, {target_id})
            invalidated = ["speech_timing", *invalidated]
            if source_changed or turn.get("speech_placement_id") or any(target_id in take["turn_ids"] for take in plan.get("speech_takes", [])):
                blockers.append("speech_timing_requires_recalibration")
        turn.update(payload)
        if source_changed and turn.get("speech_placement_id"):
            placement_id = turn["speech_placement_id"]
            placement = _one(plan["placements"], placement_id, "placement")
            if placement.get("resource_id"):
                planned = planned_speech_sources(plan)[placement_id]
                beat_id = next(beat["id"] for beat in plan["beats"]
                               if any(row["id"] == target_id for row in beat["turns"]))
                if _materialize_speech_source(store, plan, beat_id=beat_id,
                                              placement_id=placement_id, requested=planned):
                    invalidated = ["speech_timing", *invalidated]
                    blockers.append("speech_timing_requires_calibration")
    elif kind == "bind_media":
        explicit_invalidation = payload.pop("invalidate_speech_timing", False)
        placement = _one(plan["placements"], target_id, "placement")
        if "resource" in payload:
            if any(row["id"] == payload["resource_id"] for row in plan["resources"]):
                raise ValueError("resource is already registered")
            # Reuse the same project-local probe as /media/attach. A plan may
            # name a node but cannot manufacture media metadata or a digest.
            from novelvideo.api.routes.remix_media import _inspect_existing_media

            candidate = payload["resource"]
            inspected = _inspect_existing_media(store.project_dir, candidate["media_path"])
            if any(candidate[key] != inspected[key] for key in
                   ("media_path", "media_hash", "media_kind", "duration_us", "has_audio")):
                raise ValueError("resource metadata differs from project media")
            plan["resources"].append(candidate)
        resource = _verified_resource(store, plan, payload["resource_id"])
        if placement["kind"] == "visual" and resource["media_kind"] not in {"image", "video"}:
            raise ValueError("visual placement requires image or video media")
        if placement["kind"] == "audio" and (resource["media_kind"] not in {"audio", "video"} or not resource["has_audio"]):
            raise ValueError("audio placement requires audible media")
        if placement["kind"] == "graphic" or placement["kind"] == "subtitle":
            raise ValueError("this placement cannot bind media")
        old_resource_id = placement.get("resource_id")
        if explicit_invalidation and old_resource_id == resource["id"]:
            raise ValueError("invalidate_speech_timing requires a changed media resource")
        placement["resource_id"] = resource["id"]
        affected = set()
        for take in plan.get("speech_takes", []):
            if take["origin_placement_id"] == target_id and old_resource_id != resource["id"]:
                if not (resource["media_kind"] == "audio" or resource["media_kind"] == "video" and resource["has_audio"]):
                    raise ValueError("speech take requires audible project media")
                take["resource_id"] = resource["id"]
                take["source_in_us"] = 0
                take["source_out_us"] = None
                for window in plan["placements"]:
                    if window.get("speech_take_id") == take["id"]:
                        window["resource_id"] = resource["id"]
                affected.update(take["turn_ids"])
        if old_resource_id != resource["id"]:
            affected.update(turn["id"] for beat in plan["beats"] for turn in beat.get("turns", [])
                            if turn.get("speech_placement_id") == target_id
                            or (turn.get("speech_timing") or {}).get("placement_id") == target_id)
        if affected:
            if not (resource["media_kind"] == "audio" or resource["media_kind"] == "video" and resource["has_audio"]):
                raise ValueError("speech source requires audible project media")
            _guard_speech_evidence(plan, affected, allow_invalidation=explicit_invalidation)
            _invalidate_turn_timing(plan, affected)
            invalidated = ["speech_timing", *invalidated]
            blockers.append("speech_timing_requires_recalibration")
        planned = planned_speech_sources(plan).get(target_id, [])
        if planned and not any(take["origin_placement_id"] == target_id for take in plan.get("speech_takes", [])):
            beat_id = next(beat["id"] for beat in plan["beats"]
                           if any(turn["id"] == planned[0] for turn in beat["turns"]))
            if _materialize_speech_source(store, plan, beat_id=beat_id,
                                          placement_id=target_id, requested=planned):
                invalidated = ["speech_timing", *invalidated]
                blockers.append("speech_timing_requires_calibration")
    elif kind == "bind_speech_source":
        if _materialize_speech_source(store, plan, beat_id=target_id,
                                      placement_id=payload["placement_id"],
                                      requested=payload["turn_ids"]):
            invalidated = ["speech_timing", *invalidated]
            blockers.append("speech_timing_requires_calibration")
    elif kind == "reassign_speech_source":
        materialized = _reassign_draft_speech_source(store, plan, beat_id=target_id,
                                                     requested=payload["turn_ids"],
                                                     placement_id=payload["placement_id"])
        invalidated = ["speech_timing", *invalidated]
        blockers.append("speech_timing_requires_calibration" if materialized
                        else "speech_timing_requires_recalibration")
    elif kind == "upsert_placement":
        from .authored_text_timing import materialize_text_anchors

        normalized = materialize_text_anchors({"beats": plan["beats"], "placements": [payload["placement"]]})
        replacement = PlacementV2.model_validate(normalized["placements"][0]).model_dump(mode="json", exclude_none=True)
        old = next((row for row in plan["placements"] if row["id"] == target_id), None)
        if replacement.get("resource_id"):
            _verified_resource(store, plan, replacement["resource_id"])
        if old is None:
            plan["placements"].append(replacement)
        else:
            spoken_placement = any(take["origin_placement_id"] == target_id for take in plan.get("speech_takes", []))
            spoken_placement = spoken_placement or any(
                turn.get("speech_placement_id") == target_id
                or (turn.get("speech_timing") or {}).get("placement_id") == target_id
                for beat in plan["beats"] for turn in beat.get("turns", [])
            )
            if spoken_placement and any(old.get(field) != replacement.get(field) for field in _SPEECH_SOURCE_FIELDS):
                raise ValueError("speech placement source or playback changes require a dedicated take edit or bind_media")
            if spoken_placement and old.get("source_audio") != replacement.get("source_audio"):
                if any(take["origin_placement_id"] == target_id for take in plan.get("speech_takes", [])):
                    raise ValueError("speech take source changes require the dedicated correction flow")
                affected = {turn["id"] for beat in plan["beats"] for turn in beat.get("turns", [])
                            if turn.get("speech_placement_id") == target_id
                            or (turn.get("speech_timing") or {}).get("placement_id") == target_id}
                _guard_speech_evidence(plan, affected)
            plan["placements"] = [replacement if row["id"] == target_id else row for row in plan["placements"]]
    elif kind == "upsert_selection":
        from .authored_text_timing import materialize_text_anchors

        normalized = materialize_text_anchors({"beats": plan["beats"], "selections": [payload["selection"]]})
        replacement = SelectionV2.model_validate(normalized["selections"][0]).model_dump(mode="json", exclude_none=True)
        if replacement["id"] != target_id:
            raise ValueError("selection id differs from target_id")
        plan["selections"] = [row for row in plan.get("selections", []) if row["id"] != target_id] + [replacement]
    elif kind == "remove_entity":
        entity_kind = payload["entity_kind"]
        if entity_kind in {"beat", "turn"}:
            parent = plan if entity_kind == "beat" else next((b for b in plan["beats"] if any(t["id"] == target_id for t in b["turns"])), None)
            if parent is None:
                raise ValueError("turn not found")
            field = "beats" if entity_kind == "beat" else "turns"
            row = _one(parent[field], target_id, entity_kind)
            turn_ids = {t["id"] for t in row["turns"]} if entity_kind == "beat" else {target_id}
            protected = {(entity_kind, target_id)} | {("turn", x) for x in turn_ids} | {("text", x) for x in turn_ids}
            protected |= {("word", w["id"]) for b in plan["beats"] for t in b["turns"] if t["id"] in turn_ids for w in [*t.get("words", []), *(t.get("speech_timing") or {}).get("words", [])]}
            if (any((a.get("kind"), a.get("ref_id")) in protected or r.get("turn_id") in turn_ids for r in [*plan.get("placements", []), *plan.get("selections", [])] for a in [r["start"], r["end"]])
                or any(c["turn_id"] in turn_ids for c in plan.get("speech_cues", []))
                or any((entity_kind == "beat" and t["beat_id"] == target_id) or set(t["turn_ids"]) & turn_ids for t in plan.get("speech_takes", []))):
                raise ValueError("reference conflict: dependent anchors, placements, cues or takes")
            _guard_speech_evidence(plan, turn_ids)
            parent[field] = [r for r in parent[field] if r["id"] != target_id]
            return validate_payload("production_plan.v2", plan), invalidated, blockers
        field = "placements" if entity_kind == "placement" else "selections"
        _one(plan[field], target_id, payload["entity_kind"])
        if field == "placements" and (any(take["origin_placement_id"] == target_id for take in plan.get("speech_takes", []))
                                      or any(turn.get("speech_placement_id") == target_id
                                             or (turn.get("speech_timing") or {}).get("placement_id") == target_id
                                             for beat in plan["beats"] for turn in beat.get("turns", []))):
            raise ValueError("speech placement cannot be removed while a turn or take depends on it")
        plan[field] = [row for row in plan[field] if row["id"] != target_id]
    elif kind == "set_output":
        plan["output"] = payload["output"]
    else:
        raise ValueError("unsupported production command")
    return validate_payload("production_plan.v2", plan), invalidated, blockers


def apply_production_command(store: RemixArtifactStore, command: dict[str, Any]) -> dict[str, Any]:
    """Apply one bounded edit under the store mutex with an on-disk replay receipt.

    The receipt is written before the revision envelope, so a process death
    between writes can be resumed without publishing the same command twice.
    """
    request = validate_production_command(command)
    if request["kind"] == "batch":
        return apply_production_commands(store, request["plan_ref"], request["payload"]["commands"], request["command_id"])
    return _apply_transaction(store, request, [request])


def apply_production_commands(store: RemixArtifactStore, plan_ref: dict[str, Any],
                              commands: list[dict[str, Any]], command_id: str) -> dict[str, Any]:
    """Commit bounded edits together with one revision and durable replay receipt."""
    if not isinstance(commands, list) or not 1 <= len(commands) <= 100:
        raise ValueError("batch requires between 1 and 100 commands")
    if any(not isinstance(row, dict) or set(row) != {"kind", "target_id", "payload"} for row in commands):
        raise ValueError("batch command has unexpected fields")
    validated = [validate_production_command({**row, "command_id": command_id, "plan_ref": plan_ref}) for row in commands]
    return _apply_transaction(store, {"command_id": command_id, "plan_ref": plan_ref, "commands": validated}, validated)


def preflight_production_command(store: RemixArtifactStore, command: dict[str, Any]) -> dict[str, Any]:
    """Read-only simulation using the same edit and lineage checks as execution."""
    request = validate_production_command(command)
    ref = request["plan_ref"]
    commands = ([validate_production_command({**row, "command_id": request["command_id"],
                                             "plan_ref": ref})
                 for row in request["payload"]["commands"]]
                if request["kind"] == "batch" else [request])
    transaction_request = ({"command_id": request["command_id"], "plan_ref": ref,
                            "commands": commands} if request["kind"] == "batch" else request)
    receipt_dir = store.root / "_production_commands"
    if receipt_dir.is_symlink():
        raise ValueError("invalid production command storage")
    receipt_path = receipt_dir / (hashlib.sha256(request["command_id"].encode()).hexdigest() + ".json")
    if receipt_path.exists():
        receipt = store._read_json(receipt_path)
        fingerprint = hashlib.sha256(store._canonical(transaction_request)).hexdigest()
        if receipt.get("fingerprint") != fingerprint:
            raise ProductionCommandConflict("command_id reused for a different request")
        try:
            saved_result = store._read(ref["artifact_id"], ref["revision"] + 1)
        except FileNotFoundError:
            saved_result = None
        if saved_result is not None:
            if saved_result["content_hash"] != receipt["content_hash"]:
                raise ProductionCommandConflict("command result revision was replaced")
            previous = receipt["result"]
            return {"ok": True, "replayed": True, "plan_ref": ref, "issues": [],
                    "speech_estimates": previous.get("speech_estimates", []),
                    "plan_diagnostics": previous.get("plan_diagnostics", [])}
    current = store._read(ref["artifact_id"], store._latest_revision(ref["artifact_id"]))
    if current["schema"] != "production_plan.v2":
        raise ValueError("production command requires production_plan.v2")
    if current["revision"] != ref["revision"]:
        raise ProductionCommandConflict("plan changed; reload before editing",
                                        current_revision=current["revision"])
    updated = current["payload"]
    for index, entry in enumerate(commands):
        try:
            updated, _, _ = _apply_edit(store, updated, entry)
        except ValueError as exc:
            speech_unbound = (entry["kind"] == "bind_speech_source"
                              and str(exc) == "speech source placement has no bound resource")
            placement_id = entry["payload"].get("placement_id") if speech_unbound else None
            prefix = f"/payload/commands/{index}" if request["kind"] == "batch" else ""
            return {"ok": False, "plan_ref": ref, "issues": [{
                "code": "speech_source_media_required" if speech_unbound else "production_command_invalid",
                "path": (f"{prefix}/payload/placement_id" if speech_unbound else prefix or "/payload"),
                "entity_id": placement_id or entry["target_id"],
                "message": (f"画面 {placement_id} 尚未绑定实际视频或音频。草稿关联台词请设置 "
                            "speech_placement_id；素材生成并绑定后再校准语音。"
                            if speech_unbound else str(exc)),
            }], "speech_estimates": [], "plan_diagnostics": []}
    refs = current["upstream_refs"]
    try:
        store._validate_upstream_refs(refs)
        store._validate_v2_lineage("production_plan.v2", updated, refs,
                                   target_artifact_id=ref["artifact_id"], previous_plan=current)
    except ValueError as exc:
        return {"ok": False, "plan_ref": ref, "issues": [{
            "code": "production_plan_lineage_invalid", "path": "/payload",
            "entity_id": request["target_id"], "message": str(exc),
        }], "speech_estimates": [], "plan_diagnostics": []}
    from .production_plan_diagnostics import collect_plan_diagnostics, measure_plan_speech
    return {"ok": True, "plan_ref": ref, "issues": [],
            "speech_estimates": measure_plan_speech(updated),
            "plan_diagnostics": collect_plan_diagnostics(updated)}


def _apply_transaction(store: RemixArtifactStore, request: dict[str, Any], commands: list[dict[str, Any]]) -> dict[str, Any]:
    artifact_id = request["plan_ref"]["artifact_id"]
    expected = request["plan_ref"]["revision"]
    fingerprint = hashlib.sha256(store._canonical(request)).hexdigest()
    with store._locked() as guard:
        receipt_dir = store.root / "_production_commands"
        if receipt_dir.is_symlink():
            raise ValueError("invalid production command storage")
        receipt_dir.mkdir(exist_ok=True)
        receipt_path = receipt_dir / (hashlib.sha256(request["command_id"].encode()).hexdigest() + ".json")
        receipt = store._read_json(receipt_path) if receipt_path.exists() else None
        if receipt and receipt.get("fingerprint") != fingerprint:
            raise ProductionCommandConflict("command_id reused for a different request")
        if receipt:
            try:
                saved = store._read(artifact_id, expected + 1)
            except FileNotFoundError:
                saved = None
            if saved is not None:
                if saved["content_hash"] != receipt["content_hash"]:
                    raise ProductionCommandConflict("command result revision was replaced")
                return receipt["result"]
        current = store._read(artifact_id, store._latest_revision(artifact_id))
        if current["schema"] != "production_plan.v2":
            raise ValueError("production command requires production_plan.v2")
        if current["revision"] != expected:
            raise ProductionCommandConflict("plan changed; reload before editing", current_revision=current["revision"])
        updated = current["payload"]
        invalidated, blockers = [], []
        for command in commands:
            updated, changes, issues = _apply_edit(store, updated, command)
            invalidated = list(dict.fromkeys([*invalidated, *changes]))
            blockers = list(dict.fromkeys([*blockers, *issues]))
        refs = current["upstream_refs"]
        store._validate_upstream_refs(refs)
        store._validate_v2_lineage("production_plan.v2", updated, refs,
                                   target_artifact_id=artifact_id, previous_plan=current)
        result = {"plan_ref": {"artifact_id": artifact_id, "revision": expected + 1},
                  "command_id": request["command_id"],
                  "executed_commands": [{"kind": command["kind"], "target_id": command["target_id"]}
                                        for command in commands],
                  "changed_ids": list(dict.fromkeys(c["target_id"] for c in commands)), "invalidated": invalidated, "blockers": blockers}
        from .production_plan_diagnostics import collect_plan_diagnostics, measure_plan_speech
        result["speech_estimates"] = measure_plan_speech(updated)
        result["plan_diagnostics"] = collect_plan_diagnostics(updated)
        receipt = {"fingerprint": fingerprint,
                   "content_hash": "sha256:" + hashlib.sha256(store._canonical(updated)).hexdigest(),
                   "result": result}
        store._atomic_json(receipt_path, receipt, guard)
        store._publish(artifact_id=artifact_id, schema="production_plan.v2", revision=expected + 1,
                       payload=updated, upstream_refs=refs, created_by=current["created_by"], guard=guard)
    from novelvideo.chat.reference_production_observer import wake_local_observers
    wake_local_observers()
    return result
