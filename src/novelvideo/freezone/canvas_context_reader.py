"""Bounded, read-only projections of a saved canvas.

This deliberately does not reconstruct the browser ontology or dynamic actions.
Artifacts are referenced, never flattened into truncated dialogue or plan data.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
from itertools import islice
import json
import re
import time
import os
import sqlite3
from pathlib import Path
from typing import Any

MAX_REQUESTS = 20
MAX_PAGE_NODES = 100
MAX_RESPONSE_BYTES = 128 * 1024
EXCLUDED = [
    "selection",
    "unsaved_changes",
    "editable_parameters",
    "dynamic_action_catalog",
    "full_ontology",
]
_BROWSER_TYPES = {
    "selection_detail",
    "node_action_catalog",
    "action_catalog",
    "canvas_action_catalog",
    "node_action_read",
    "node_create_schema",
    "audio_voice_options",
    "validate_canvas_commands",
    "slot_candidates",
    "mainline_projection_assets",
    "canvas_command_catalog",
    "link_type_catalog",
}
_saved_read_mode: ContextVar[str] = ContextVar(
    "canvas_read_mode", default="current_browser"
)


def _binding_path(state_dir: Path, turn_id: str) -> Path:
    return (
        Path(state_dir)
        / "freezone"
        / "context_read_turns"
        / (hashlib.sha256(turn_id.encode()).hexdigest() + ".json")
    )


@contextmanager
def saved_context_execution(
    *,
    execution_context: Any,
    turn_id: str,
    state_dir: Path,
    coordinator_lease: dict | None = None,
    human_message_id: int | None = None,
    chat_scope_id: str | None = None,
):
    if not turn_id or execution_context.surface != "freezone":
        raise ValueError("saved context requires a trusted Freezone turn")
    path = _binding_path(state_dir, turn_id)
    binding = {
        key: getattr(execution_context, key)
        for key in ("requester_user_id", "project_id", "canvas_id", "agent_id")
    }
    if (human_message_id is None) != (chat_scope_id is None):
        raise ValueError("human message and chat scope must be bound together")
    if human_message_id is not None and (type(human_message_id) is not int or human_message_id < 1):
        raise ValueError("human message id must be a persisted positive integer")
    binding.update(
        turn_id=turn_id,
        expires_at=time.time() + (24 * 60 * 60 if coordinator_lease else 3600 if human_message_id else 300),
    )
    if human_message_id is not None:
        binding.update(human_message_id=human_message_id, chat_scope_id=chat_scope_id)
    if coordinator_lease:
        binding["coordinator_lease"] = coordinator_lease
    path.parent.mkdir(parents=True, exist_ok=True)
    # Bounded housekeeping; stale crash records never authorize after expiry.
    for stale in islice(path.parent.glob("*.json"), 100):
        try:
            data = json.loads(stale.read_text())
            if isinstance(data, dict) and data.get("expires_at", 0) <= time.time():
                stale.unlink(missing_ok=True)
        except (FileNotFoundError, ValueError):
            continue
    # Reject live turn reuse. Binding grants no project access on its own.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(binding, stream)
    token = _saved_read_mode.set("saved")
    try:
        yield
    finally:
        _saved_read_mode.reset(token)
        path.unlink(missing_ok=True)


def canvas_read_mode() -> str:
    return _saved_read_mode.get()


def saved_turn_binding(state_dir: Path, turn_id: str) -> dict | None:
    if not turn_id:
        return None
    path = _binding_path(state_dir, turn_id)
    try:
        binding = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if (
        not isinstance(binding, dict)
        or binding.get("turn_id") != turn_id
        or binding.get("expires_at", 0) <= time.time()
    ):
        return None
    lease = binding.get("coordinator_lease")
    if lease:
        # A production binding is valid only while its existing coordinator owns
        # the exact actor/project/canvas/agent/turn lease. Open read-only; no DB creation.
        try:
            with sqlite3.connect(
                Path(lease["db_path"]).resolve().as_uri() + "?mode=ro", uri=True
            ) as connection:
                row = connection.execute(
                    """SELECT payload_json FROM reference_remix_continuations
                    WHERE run_id=? AND requester_user_id=? AND project_id=? AND canvas_id=? AND agent_id=?
                      AND lease_owner=? AND lease_until>?""",
                    (
                        lease["run_id"],
                        binding["requester_user_id"],
                        binding["project_id"],
                        binding["canvas_id"],
                        binding["agent_id"],
                        lease["owner_token"],
                        time.time(),
                    ),
                ).fetchone()
            if not row or json.loads(row[0]).get("resume_turn_id") != turn_id:
                return None
        except (sqlite3.Error, KeyError, ValueError, TypeError):
            return None
    return binding


def _string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _source(node: dict) -> str | None:
    data = node.get("data") or {}
    fields = (
        ("videoUrl", "sourceUrl", "previewImageUrl")
        if node.get("type") == "videoNode"
        else ("audioUrl", "sourceUrl")
        if node.get("type") == "audioNode"
        else (
            "imageUrl",
            "previewImageUrl",
            "referenceImageUrl",
            "videoUrl",
            "audioUrl",
            "sourceUrl",
        )
    )
    return next((_string(data.get(k)) for k in fields if _string(data.get(k))), None)


def _media_type(node: dict) -> str | None:
    kind = node.get("type")
    if kind == "videoNode":
        return "video"
    if kind == "audioNode":
        return "audio"
    if kind in {
        "uploadNode",
        "imageNode",
        "imageGenNode",
        "exportImageNode",
        "pano360ViewerNode",
    }:
        return "image"
    return _string((node.get("data") or {}).get("media_kind"))


def _project_node(node: dict) -> dict:
    data = node.get("data") or {}
    item = {
        "node_id": node["id"],
        "node_type": node.get("type"),
        "position": node.get("position", {"x": 0, "y": 0}),
    }
    # Only allow listed semantic fields; never return arbitrary node data.
    if _string(data.get("label")):
        item["label"] = data["label"]
    media = _media_type(node)
    if media:
        item["media_type"] = media
    source = _source(node)
    if source:
        item["source_url"] = source
    preview = _string(data.get("previewImageUrl")) or (
        source
        if node.get("type") == "videoNode"
        else _string(data.get("imageUrl")) or _string(data.get("referenceImageUrl"))
    )
    if preview:
        item["preview_url"] = preview
    if node.get("type") == "videoStoryNode":
        raw = data.get("rawResult")
        story = raw.get("video_story") if isinstance(raw, dict) else None
        status = (
            "failed"
            if data.get("analysisError")
            else "running"
            if data.get("isAnalyzing")
            else "completed"
            if raw
            else "not_started"
        )
        item["reference_analysis"] = {
            "status": status,
            "analysis_node_id": node["id"],
            "run_id": data.get("referenceAnalysisRunId")
            or data.get("referenceAnalysisCompletedRunId"),
            "source_url": data.get("sourceVideoUrl"),
            "summary": story.get("summary", "")[:1200]
            if isinstance(story, dict) and isinstance(story.get("summary"), str)
            else None,
            "result_ref": data.get("referenceBlueprintRef"),
            "next_action": "await_completion_event"
            if status == "running"
            else "freezone_get_production_progress"
            if status == "completed"
            else "inspect_error",
        }
        item["reference_analysis"].update(
            analysis_ref=data.get("referenceAnalysisRef"),
            evidence_ref=data.get("referenceEvidenceRef"),
            transcript_ref=data.get("referenceTranscriptRef"),
            blueprint_ref=data.get("referenceBlueprintRef"),
        )
    if node.get("type") == "scriptNode" and data.get("remixRole") == "production_plan":
        ref = data.get("productionPlanV2Ref") or {}
        valid_ref = (
            isinstance(ref, dict)
            and isinstance(ref.get("artifactId"), str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", ref["artifactId"])
            and type(ref.get("revision")) is int
            and ref["revision"] > 0
        )
        draft = data.get("productionPlanV2Draft") or {}
        if not isinstance(draft, dict) or draft.get("schema_version") != 2:
            draft = data.get("productionPlanDraft") or {} if not valid_ref else {}
        output = (
            draft.get("output")
            if isinstance(draft, dict) and draft.get("schema_version") == 2
            else None
        )
        if not isinstance(output, dict) or not all(
            type(output.get(k)) is int and output[k] > 0
            for k in ("width", "height", "fps_num", "fps_den")
        ):
            output = None
        else:
            output = {
                k: output[k]
                for k in ("width", "height", "fps_num", "fps_den", "background")
                if k in output
                and (
                    k != "background"
                    or isinstance(output[k], str)
                    and re.fullmatch(r"#[0-9a-fA-F]{6}", output[k])
                )
            }
        item["production_plan"] = {
            "remix_role": "production_plan",
            "schema": "production_plan.v2" if valid_ref else None,
            "plan_ref": {"artifact_id": ref["artifactId"], "revision": ref["revision"]}
            if valid_ref
            else None,
            "goal": _string(draft.get("goal")),
            "output": output,
            "dirty": data.get("productionPlanV2Dirty") is True
            or not valid_ref
            and data.get("productionPlanDirty") is True,
            "conflict": data.get("productionPlanV2Conflict") is True,
        }
    instance = _string(data.get("workflowInstanceId"))
    if instance:
        item["workflow_identity"] = {
            "instance_id": instance,
            "plan_node_id": _string(data.get("workflowPlanNodeId")),
            "config_confirmed": data.get("workflowConfigConfirmed") is True,
        }
    if (
        node.get("type") == "htmlArtifactNode"
        and _string(data.get("artifactId"))
        and type(data.get("artifactVersion")) is int
        and data["artifactVersion"] > 0
    ):
        item["html_artifact"] = {
            "id": data["artifactId"],
            "version": data["artifactVersion"],
        }
    return item


def _reference_media(node: dict, nodes: list[dict], edges: list[dict]) -> list[dict]:
    if node.get("type") not in {"imageGenNode", "imageNode", "videoNode", "scriptNode"}:
        return []
    incoming = [edge for edge in edges if edge["target"] == node["id"]]
    by_id = {item["id"]: item for item in nodes}
    order = (node.get("data") or {}).get("referenceOrder") or []
    if isinstance(order, list):
        incoming.sort(
            key=lambda edge: (
                order.index(edge["source"]) if edge["source"] in order else len(order)
            )
        )
    counts = {"video": 0, "image": 0, "audio": 0}
    seen = set()
    items = []
    for edge in incoming:
        source = by_id.get(edge["source"])
        if not source:
            continue
        kind, url = _media_type(source), _source(source)
        if kind not in counts or not url or (kind, url) in seen:
            continue
        seen.add((kind, url))
        counts[kind] += 1
        data = source.get("data") or {}
        projected = _project_node(source)
        items.append(
            {
                "mention": "@"
                + {"video": "视频", "audio": "音频", "image": "图片"}[kind]
                + str(counts[kind]),
                "node_id": source["id"],
                "label": _string(data.get("displayName"))
                or {"video": "视频", "audio": "音频", "image": "图片"}[kind],
                "media_type": kind,
                "source_url": url,
                "preview_url": projected.get("preview_url"),
                "link_type": (edge.get("data") or {}).get("link_type"),
            }
        )
    return items


def _node_with_media(node: dict, nodes: list[dict], edges: list[dict]) -> dict:
    result = _project_node(node)
    media = _reference_media(node, nodes, edges)
    if media:
        result["reference_media"] = media
    return result


def _edge(edge: dict) -> dict:
    return {
        "edge_id": edge.get("id"),
        "source": edge.get("source"),
        "target": edge.get("target"),
        "source_handle": edge.get("sourceHandle"),
        "target_handle": edge.get("targetHandle"),
        "link_type": (edge.get("data") or {}).get("link_type"),
    }


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def read_saved_context(
    canvas: dict,
    *,
    project_id: str,
    canvas_id: str,
    requests: list[dict],
    expected_revision: Any = None,
) -> dict:
    if len(requests) > MAX_REQUESTS:
        raise ValueError("at most 20 context requests")
    for request in requests:
        if (
            not isinstance(request, dict)
            or not isinstance(request.get("type"), str)
            or not 1 <= len(request["type"]) <= 64
        ):
            raise ValueError("request type must be a bounded string")
        if "node_id" in request and (
            not isinstance(request["node_id"], str) or len(request["node_id"]) > 128
        ):
            raise ValueError("node_id must be a bounded string")
        if "cursor" in request and (
            not isinstance(request["cursor"], str) or len(request["cursor"]) > 2048
        ):
            raise ValueError("cursor must be a bounded string")
    revision = canvas.get("revision")
    if expected_revision is not None and expected_revision != revision:
        raise ValueError("revision_conflict")
    nodes = [
        node
        for node in canvas.get("nodes", [])
        if isinstance(node, dict)
        and isinstance(node.get("id"), str)
        and not node.get("deleted")
    ]
    by_id = {node["id"]: node for node in nodes}
    edges = [
        edge
        for edge in canvas.get("edges", [])
        if isinstance(edge, dict)
        and edge.get("source") in by_id
        and edge.get("target") in by_id
    ]
    result = {
        "schema_version": "canvas_context_response.v1",
        "source": "saved_canvas",
        "canvas_revision": revision,
        "canvas_context_status": "resolved",
        "project_id": project_id,
        "canvas_id": canvas_id,
        "coverage": {
            "complete": False,
            "excluded": EXCLUDED,
            "display_fields": "labels may differ; no browser display-name catalog",
        },
        "responses": [],
    }
    for request in requests:
        kind = request.get("type")
        response: dict = {"type": kind, "data": None}
        if kind not in {"node_detail", "neighbor_graph", "canvas_summary"}:
            response.update(
                status="requires_browser" if kind in _BROWSER_TYPES else "unsupported",
                coverage={
                    "complete": False,
                    "reason": "current browser state or capability required"
                    if kind in _BROWSER_TYPES
                    else "saved projection not implemented",
                },
            )
            result["responses"].append(response)
            continue
        node_id = request.get("node_id")
        if kind != "canvas_summary":
            response["node_id"] = node_id
        if kind != "canvas_summary" and node_id not in by_id:
            result["responses"].append(response)
            continue
        selected_ids = set(by_id) if kind == "canvas_summary" else {node_id}
        if kind == "node_detail" and by_id[node_id].get("type") == "groupNode":
            selected_ids.update(
                node["id"] for node in nodes if node.get("parentId") == node_id
            )
        if kind == "neighbor_graph":
            depth = request.get("depth", 1)
            if type(depth) is not int or not 0 <= depth <= 2:
                raise ValueError("neighbor depth must be between 0 and 2")
            response["depth"] = depth
            for _ in range(depth):
                frontier = frozenset(selected_ids)
                selected_ids.update(
                    endpoint
                    for edge in edges
                    if edge["source"] in frontier or edge["target"] in frontier
                    for endpoint in (edge["source"], edge["target"])
                )
        selected = [node for node in nodes if node["id"] in selected_ids]
        selected_edges = [
            edge
            for edge in edges
            if edge["source"] in selected_ids or edge["target"] in selected_ids
        ]
        fingerprint = hashlib.sha256(
            json.dumps(
                [
                    project_id,
                    canvas_id,
                    revision,
                    {k: v for k, v in request.items() if k != "cursor"},
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        offset = edge_offset = 0
        if request.get("cursor"):
            try:
                cursor = json.loads(base64.urlsafe_b64decode(request["cursor"]))
                if (
                    not isinstance(cursor, list)
                    or len(cursor) != 3
                    or cursor[0] != fingerprint
                    or type(cursor[1]) is not int
                    or type(cursor[2]) is not int
                    or not 0 <= cursor[1] <= len(selected)
                    or not 0 <= cursor[2] <= len(selected_edges)
                ):
                    raise ValueError()
                offset, edge_offset = cursor[1:]
            except (ValueError, TypeError, IndexError):
                raise ValueError("invalid or stale cursor") from None
        page = selected[offset : offset + MAX_PAGE_NODES]
        edge_page = selected_edges[edge_offset : edge_offset + MAX_PAGE_NODES]
        response["data"] = {
            "schema_version": "canvas_node_detail.v1"
            if kind == "node_detail"
            else "saved_canvas_summary.v1"
            if kind == "canvas_summary"
            else "canvas_node_reference.v1",
            "project": project_id,
            "canvas_id": canvas_id,
            "nodes": [_node_with_media(node, nodes, edges) for node in page],
            "edges": [_edge(edge) for edge in edge_page],
        }
        if kind == "canvas_summary":
            response["data"].update(
                node_count=len(nodes), edge_count=len(edges), ontology_complete=False
            )
        while _size(result) + _size(response) + 4096 > MAX_RESPONSE_BYTES and (
            len(page) > 1 or edge_page
        ):
            if edge_page:
                edge_page.pop()
                response["data"]["edges"].pop()
            else:
                page.pop()
                response["data"]["nodes"].pop()
        if _size(result) + _size(response) + 4096 > MAX_RESPONSE_BYTES:
            response.update(
                data=None,
                status="response_too_large",
                coverage={
                    "complete": False,
                    "reason": "single node exceeds response budget; use artifact API for referenced data",
                },
            )
        else:
            complete = offset + len(page) == len(selected) and edge_offset + len(
                edge_page
            ) == len(selected_edges)
            response["coverage"] = {
                "complete": complete,
                "projection": "saved_compact",
                "excluded": EXCLUDED,
            }
            if not complete:
                response["cursor"] = base64.urlsafe_b64encode(
                    json.dumps(
                        [fingerprint, offset + len(page), edge_offset + len(edge_page)]
                    ).encode()
                ).decode()
        result["responses"].append(response)
    return result
