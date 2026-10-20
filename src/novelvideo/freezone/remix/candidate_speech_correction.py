"""Authenticated lexical corrections to immutable candidate speech observations."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import hashlib
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .speech_review_summary import build_speech_review_summary, classify_speech_difference

SCHEMA = "candidate_speech_correction.v1"
PRODUCER = "remix-candidate-speech-correction.v1"


class CandidateSpeechCorrectionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    authorization_scope: dict[str, Any]
    observation_ref: dict[str, Any]
    work_ref: dict[str, Any]
    plan_ref: dict[str, Any]
    step_id: str
    node_id: str
    resource_signature: dict[str, Any]
    target_signature: str
    target_turns: list[dict[str, Any]]
    corrections: list[dict[str, Any]] = Field(max_length=20000)
    corrected_words: list[dict[str, Any]] = Field(max_length=20000)
    actor_kind: Literal["user"] = "user"
    actor_id: str = Field(min_length=1)
    command_id: str = Field(min_length=1, max_length=128)
    decision_receipt: str = Field(pattern=r"^[a-f0-9]{64}$")


def _candidate_context(store, ctx, canvas: dict, *, canvas_id: str, work_ref: dict,
                       step_id: str, node_id: str, require_write: bool, _allow_no_dialogue: bool = False):
    from .material_execution import canvas_material_resource
    from .material_work import _exact_ref, _fingerprint
    from .source_speech import candidate_scope

    _exact_ref(work_ref)
    scope = candidate_scope(ctx, canvas, canvas_id, require_write=require_write)
    if store.project_id != ctx.project_id or store.project_dir != Path(ctx.output_dir).resolve():
        raise ValueError("candidate store differs from host project scope")
    work = store.read(work_ref["artifact_id"], work_ref["revision"])
    if work["schema"] != "material_work_package.v1" or store.read_latest(work["id"])["revision"] != work["revision"]:
        raise ValueError("current material work required")
    step = next((row for row in work["payload"]["steps"] if row["id"] == step_id), None)
    if not step or step.get("node_id") != node_id or step["status"] in {"rejected", "stale"}:
        raise ValueError("current candidate material required")
    plan = store.read_latest(work["payload"]["plan_ref"]["artifact_id"])
    node = next((row for row in canvas.get("nodes", []) if row.get("type") == "scriptNode"
                 and (row.get("data", {}).get("productionPlanV2Ref") or {}).get("artifactId") == plan["id"]), None)
    if (not node or (node["data"]["productionPlanV2Ref"].get("revision") != plan["revision"])
            or node["data"].get("productionPlanV2Dirty") or node["data"].get("productionPlanV2Conflict")
            or _fingerprint(store, plan["payload"], work["payload"]["requirements"]) != work["payload"]["input_fingerprint"]):
        raise ValueError("current plan target changed")
    required = [row for row in work["payload"]["requirements"] if row["final_step_id"] == step_id]
    ids = {turn_id for row in required for turn_id in row.get("turn_ids", [])}
    for take in plan["payload"].get("speech_takes", []):
        if any(take["id"] in row.get("take_ids", []) for row in required):
            ids.update(take["turn_ids"])
    turns = [{"id": row["id"], "speaker": row.get("speaker") or "", "text": row["text"]}
             for beat in plan["payload"]["beats"] for row in beat.get("turns", []) if row["id"] in ids]
    if (not turns and not _allow_no_dialogue) or len(turns) != len(ids):
        raise ValueError("candidate has no declared dialogue target")
    resource = canvas_material_resource(store, canvas, node_id, step["media_kind"])
    return work, plan, resource, scope, turns


def _context(store, ctx, canvas: dict, *, canvas_id: str, work_ref: dict,
             step_id: str, node_id: str, observation_ref: dict, require_write: bool):
    from .material_work import _exact_ref
    from .source_speech import read_source_speech_observation

    _exact_ref(observation_ref)
    work, plan, resource, scope, turns = _candidate_context(store, ctx, canvas,
        canvas_id=canvas_id, work_ref=work_ref, step_id=step_id, node_id=node_id,
        require_write=require_write)
    raw = store.read(observation_ref["artifact_id"], observation_ref["revision"])
    observation = read_source_speech_observation(store, observation_ref, scope=scope, resource=resource,
        language=raw["payload"].get("requested_language") or raw["payload"]["language"],
        provider_signature=raw["payload"]["provider_signature"])
    summary = build_speech_review_summary(observation["payload"], target_turns=turns, observation_ref=observation_ref)
    return work, plan, resource, scope, summary


def read_candidate_speech_review(store, ctx, canvas: dict, *, canvas_id: str,
                                work_ref: dict, step_id: str, node_id: str, observation_ref: dict) -> dict:
    _, plan, _, scope, summary = _context(store, ctx, canvas, canvas_id=canvas_id,
        work_ref=work_ref, step_id=step_id, node_id=node_id, observation_ref=observation_ref, require_write=False)
    pointer = _pointer(store, scope, ctx.requester_user_id, work_ref["artifact_id"],
                       step_id, observation_ref, summary["target_signature"])
    if pointer.is_file():
        ref = store._read_json(pointer).get("correction_ref")
        correction = validate_candidate_correction(store, ref,
            observation=store.read(observation_ref["artifact_id"], observation_ref["revision"]),
            target_turns=summary["target_turns"], scope=scope, requester_user_id=ctx.requester_user_id)
        summary["correction_ref"] = ref
        summary["corrected_words"] = correction["payload"]["corrected_words"]
    return {"work_ref": work_ref, "plan_ref": {"artifact_id": plan["id"], "revision": plan["revision"]},
            "step_id": step_id, "node_id": node_id, "summary": summary}


def find_candidate_speech_review(store, ctx, canvas: dict, *, canvas_id: str,
                                work_ref: dict, step_id: str, node_id: str,
                                language: str = "auto") -> dict:
    """Resolve the current immutable observation without admission or provider calls."""
    from .source_speech import configured_provider_signature, source_speech_key

    if not isinstance(language, str) or not language or len(language) > 32:
        raise ValueError("invalid recognition language")
    _, _, resource, scope, turns = _candidate_context(store, ctx, canvas, canvas_id=canvas_id,
        work_ref=work_ref, step_id=step_id, node_id=node_id, require_write=False, _allow_no_dialogue=True)
    if not resource.get("has_audio"):
        return {"status": "unavailable", "message": "该素材没有音轨"}
    if not turns:
        return {"status": "unavailable", "message": "该素材未安排台词，可直接审听声音"}
    try:
        provider = configured_provider_signature()
    except (ValueError, RuntimeError):
        return {"status": "unavailable", "message": "语音识别服务尚未配置"}
    key = source_speech_key(scope=scope, media_hash=resource["media_hash"], stream_index=0,
                            language=language, provider_signature=provider)
    if not store._path(key, 1, ".json").is_file():
        return {"status": "pending", "message": "尚未获得当前完整素材的识别结果"}
    result = read_candidate_speech_review(store, ctx, canvas, canvas_id=canvas_id,
        work_ref=work_ref, step_id=step_id, node_id=node_id,
        observation_ref={"artifact_id": key, "revision": 1})
    return {"status": "ready", **result}


def _pointer(store, scope: dict, actor_id: str, work_id: str, step_id: str,
             observation_ref: dict, target_signature: str):
    directory = store.root / "_candidate_speech_corrections"
    if directory.is_symlink():
        raise ValueError("candidate correction index contains a symlink")
    key = hashlib.sha256(store._canonical({
        "scope": scope, "actor_id": actor_id, "work_id": work_id, "step_id": step_id,
        "observation_ref": observation_ref, "target_signature": target_signature})).hexdigest()
    return directory / (key + ".json")


def validate_candidate_correction(store, ref: dict, *, observation: dict,
                                  target_turns: list[dict], scope: dict, requester_user_id: str) -> dict:
    """Reconstruct exact reviewed words; this does not approve material sound."""
    from .material_work import _exact_ref
    _exact_ref(ref)
    saved = store.read(ref["artifact_id"], ref["revision"])
    payload = saved["payload"]
    original_ref = {"artifact_id": observation["id"], "revision": observation["revision"]}
    summary = build_speech_review_summary(observation["payload"], observation_ref=original_ref,
                                         target_turns=target_turns)
    receipt = hashlib.sha256(store._canonical({
        key: value for key, value in payload.items() if key != "decision_receipt"})).hexdigest()
    if (saved["schema"] != SCHEMA or saved["producer_version"] != PRODUCER or saved["revision"] != 1
            or saved["created_by"] != requester_user_id or payload["actor_id"] != requester_user_id
            or payload["actor_kind"] != "user" or payload["authorization_scope"] != scope
            or payload["observation_ref"] != original_ref
            or payload["resource_signature"] != observation["payload"]["resource_signature"]
            or payload["target_signature"] != summary["target_signature"]
            or payload["target_turns"] != target_turns or payload["decision_receipt"] != receipt
            or not any(row["artifact_id"] == original_ref["artifact_id"] and row["revision"] == original_ref["revision"]
                       for row in saved["upstream_refs"])):
        raise ValueError("candidate correction provenance differs")
    words = deepcopy(observation["payload"]["recognized_words"])
    by_id = {row["id"]: row for row in words}
    seen = set()
    for change in payload["corrections"]:
        if (set(change) != {"word_id", "text", "reason"} or change["word_id"] not in by_id
                or change["word_id"] in seen or not isinstance(change["text"], str)
                or not change["text"].strip() or not isinstance(change["reason"], str)
                or not change["reason"].strip()):
            raise ValueError("candidate correction words differ from source")
        by_id[change["word_id"]]["text"] = change["text"].strip()
        seen.add(change["word_id"])
    if (words != payload["corrected_words"] or classify_speech_difference(summary["target_text"],
            "".join(row["text"] for row in words)) not in {"exact", "normalized_match"}):
        raise ValueError("candidate correction differs from observed word times or target")
    return saved


def save_candidate_speech_correction(store, ctx, canvas: dict, *, canvas_id: str,
                                    work_ref: dict, step_id: str, node_id: str, observation_ref: dict,
                                    target_signature: str, corrections: list[dict], confirmed: bool,
                                    command_id: str, actor_kind: str, _canvas_guard=None) -> dict:
    from .store import RemixArtifactStore
    from .reference_director import _MaterialPublicationGuard

    if actor_kind != "user" or confirmed is not True:
        raise ValueError("authenticated user listening confirmation required")
    if not isinstance(command_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", command_id):
        raise ValueError("invalid correction command")
    work, plan, resource, scope, summary = _context(store, ctx, canvas,
        canvas_id=canvas_id, work_ref=work_ref, step_id=step_id, node_id=node_id,
        observation_ref=observation_ref, require_write=True)
    if target_signature != summary["target_signature"]:
        raise ValueError("correction target changed")
    if summary["timing_granularity"] != "word":
        raise ValueError("real word timing required for lexical corrections")
    words = deepcopy(summary["recognized_words"])
    by_id = {word["id"]: word for word in words}
    seen = set()
    for change in corrections:
        if (set(change) != {"word_id", "text", "reason"} or change["word_id"] not in by_id
                or change["word_id"] in seen or not isinstance(change["text"], str)
                or not change["text"].strip() or len(change["text"]) > 1000
                or not isinstance(change["reason"], str) or not change["reason"].strip()
                or len(change["reason"]) > 1000):
            raise ValueError("correction requires one known word, text and reason")
        seen.add(change["word_id"])
        by_id[change["word_id"]]["text"] = change["text"].strip()
    if not corrections or classify_speech_difference(summary["target_text"], "".join(row["text"] for row in words)) not in {"exact", "normalized_match"}:
        raise ValueError("corrected recognition still differs from intended dialogue")
    plan_ref = {"artifact_id": plan["id"], "revision": plan["revision"]}
    payload = {
        "schema_version": 1,
        "authorization_scope": scope, "observation_ref": observation_ref, "work_ref": work_ref,
        "plan_ref": plan_ref, "step_id": step_id, "node_id": node_id,
        "resource_signature": {"media_hash": resource["media_hash"], "stream_index": 0},
        "target_signature": target_signature, "target_turns": summary["target_turns"],
        "corrections": deepcopy(corrections), "corrected_words": words,
        "actor_id": ctx.requester_user_id, "actor_kind": "user", "command_id": command_id,
    }
    payload["decision_receipt"] = hashlib.sha256(store._canonical(payload)).hexdigest()
    typed = CandidateSpeechCorrectionV1.model_validate(payload).model_dump(mode="json")
    publisher = RemixArtifactStore(store.project_dir, store.project_id,
                                   created_by=ctx.requester_user_id, producer_version=PRODUCER)
    artifact_id = hashlib.sha256(store._canonical({
        "scope": scope, "actor_id": ctx.requester_user_id, "work_id": work["id"],
        "step_id": step_id, "command_id": command_id})).hexdigest()
    refs = publisher._reference_list([observation_ref, work_ref, plan_ref], store.project_id)
    with publisher._locked() as guard:
        # Use unlocked store primitives while holding the publication guard;
        # reacquiring the same artifact mutex would deadlock.
        if (publisher._latest_revision(work["id"]) != work["revision"]
                or publisher._latest_revision(plan["id"]) != plan["revision"]):
            raise ValueError("candidate work or target changed before correction publication")
        current_work = publisher._read(work["id"], work["revision"])
        current_plan = publisher._read(plan["id"], plan["revision"])
        if current_work["content_hash"] != work["content_hash"] or current_plan["content_hash"] != plan["content_hash"]:
            raise ValueError("candidate work or target changed")
        from .source_speech import _verified_media
        _verified_media(publisher, resource)
        publisher._validate_upstream_refs(refs)
        if publisher._path(artifact_id, 1, ".json").exists():
            saved = publisher._read(artifact_id, 1)
            if saved["payload"] != typed:
                raise ValueError("correction command reused with different inputs")
        else:
            saved = publisher._publish(artifact_id=artifact_id, schema=SCHEMA, revision=1,
                payload=typed, upstream_refs=refs, created_by=ctx.requester_user_id,
                guard=_MaterialPublicationGuard(guard, _canvas_guard) if _canvas_guard else guard)
        correction_ref = {"artifact_id": saved["id"], "revision": saved["revision"]}
        pointer = _pointer(publisher, scope, ctx.requester_user_id, work["id"], step_id,
                           observation_ref, target_signature)
        pointer.parent.mkdir(exist_ok=True)
        publisher._atomic_json(pointer, {"correction_ref": correction_ref},
                              _MaterialPublicationGuard(guard, _canvas_guard) if _canvas_guard else guard)
    return {"correction_ref": correction_ref,
            "correction": saved, "summary": {**summary, "corrected_words": words}}
