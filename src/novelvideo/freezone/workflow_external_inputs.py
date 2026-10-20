"""Resolve declared Workflow inputs against an authenticated canvas snapshot."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from novelvideo.freezone.canvas_media_scope import scan_foreign_media_refs


def resolve_external_image_inputs(
    plan: dict[str, Any],
    canvas: dict[str, Any] | None,
    *,
    project_id: str,
    canvas_id: str | None = None,
    expected: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    declarations = plan.get("external_inputs") or []
    if not declarations:
        return {}
    if (
        not isinstance(canvas, dict)
        or str(canvas.get("project_id") or "") != project_id
    ):
        raise ValueError(
            "workflow external input canvas is unavailable in this project"
        )
    if canvas_id is not None and str(canvas.get("canvas_id") or "") != canvas_id:
        raise ValueError("workflow external input belongs to another canvas")
    revision = canvas.get("revision")
    if type(revision) is not int or revision < 1:
        raise ValueError("workflow external input canvas revision is invalid")
    nodes = {
        str(node.get("id")): node
        for node in canvas.get("nodes") or []
        if isinstance(node, dict) and isinstance(node.get("id"), str)
    }
    resolved: dict[str, dict[str, Any]] = {}
    for source in declarations:
        alias, node_id = source["id"], source["node_id"]
        node = nodes.get(node_id)
        if source.get("media_kind") == "reference_analysis":
            skill = plan.get("skill")
            if not isinstance(skill, dict) or skill.get("id") != "reference-video-remix":
                raise ValueError("reference analysis input requires reference-video-remix")
            if node is None or node.get("type") != "videoStoryNode":
                raise ValueError(f"workflow reference analysis is unavailable: {node_id}")
            data = node.get("data") if isinstance(node.get("data"), dict) else {}
            raw = data.get("rawResult")
            story = raw.get("video_story") if isinstance(raw, dict) else None
            structure = story.get("adaptation_structure") if isinstance(story, dict) else None
            has_context = isinstance(story, dict) and (
                isinstance(story.get("summary"), str) and bool(story["summary"].strip())
                or isinstance(structure, dict) and (
                    bool(structure.get("narrative_role")) or bool(structure.get("reusable_beats"))
                )
            )
            blueprint = data.get("referenceBlueprintRef")
            has_blueprint = (
                isinstance(blueprint, dict) and not data.get("blueprintDirty")
                and isinstance(blueprint.get("artifactId", blueprint.get("artifact_id")), str)
                and bool(blueprint.get("artifactId", blueprint.get("artifact_id")))
                and type(blueprint.get("revision")) is int and blueprint["revision"] > 0
            )
            media_url = data.get("sourceVideoUrl")
            if (not has_context and not has_blueprint or data.get("isAnalyzing")
                    or data.get("analysisError") or not isinstance(media_url, str) or not media_url.strip()):
                raise ValueError(f"workflow reference analysis is not completed: {node_id}")
            if scan_foreign_media_refs({"nodes": [node]}, project_id=project_id):
                raise ValueError(f"workflow reference analysis belongs to another project: {node_id}")
            snapshot = {key: data.get(key) for key in (
                "rawResult", "referenceBlueprintRef", "blueprintDirty", "sourceVideoUrl",
            )}
            resolved[alias] = {
                "node_id": node_id, "media_url": media_url, "canvas_revision": revision,
                "display_name": str(data.get("displayName") or data.get("title") or node_id),
                "analysis_snapshot": snapshot,
                "analysis_fingerprint": hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            }
            continue
        if node is None or node.get("type") not in {
            "uploadNode",
            "imageGenNode",
            "imageNode",
            "exportImageNode",
        }:
            raise ValueError(f"workflow external image node is unavailable: {node_id}")
        data = node.get("data") if isinstance(node.get("data"), dict) else {}
        if (
            isinstance(data.get("videoUrl"), str) and data["videoUrl"].strip()
        ) or data.get("media_kind") in {"video", "audio"}:
            raise ValueError(f"workflow external node is not an image: {node_id}")
        media_url = data.get("imageUrl") or data.get("previewImageUrl")
        if not media_url and node.get("type") == "imageGenNode":
            media_url = data.get("referenceImageUrl")
        if not media_url and node.get("type") == "uploadNode":
            media_url = data.get("source_url")
        if (
            not isinstance(media_url, str)
            or not media_url.strip()
            or data.get("assetMigration")
        ):
            raise ValueError(
                f"workflow external image has no persistent media: {node_id}"
            )
        if scan_foreign_media_refs({"nodes": [node]}, project_id=project_id):
            raise ValueError(
                f"workflow external image belongs to another project: {node_id}"
            )
        resolved[alias] = {
            "node_id": node_id,
            "media_url": media_url,
            "canvas_revision": revision,
            "display_name": str(
                data.get("displayName") or data.get("title") or node_id
            ),
        }
    if expected is not None and resolved != expected:
        raise ValueError(
            "workflow external input or canvas changed; prepare a new draft"
        )
    return resolved
