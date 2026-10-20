"""Validate a persisted, human-submitted generation-parameter receipt.

The caller must load this record from host-owned storage. Model tool arguments
and chat text are never proof that the user submitted a parameter card.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

_PARAMETER_FIELDS = frozenset({
    "image_model", "image_aspect_ratio", "image_resolution", "image_quality",
    "image_variants_per_node", "video_model", "video_aspect_ratio",
    "video_resolution", "video_duration_seconds", "video_generate_audio",
    "video_variants_per_node",
})
_REQUIREMENT_FIELDS = _PARAMETER_FIELDS | {"production_scope", "image_count", "video_count"}
_COMPLETE_FIELDS = {
    "image": {"image_model", "image_aspect_ratio", "image_resolution", "image_variants_per_node"},
    "video": {"video_model", "video_aspect_ratio", "video_resolution", "video_variants_per_node"},
}


def generation_requirements_digest(
    commission: dict[str, Any], inputs: dict[str, Any],
) -> str:
    """Bind a card to its confirmed creative direction and media requirements."""
    requirements = generation_requirements_snapshot(commission, inputs)
    return hashlib.sha256(json.dumps(
        requirements, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def generation_requirements_snapshot(
    commission: dict[str, Any], inputs: dict[str, Any],
) -> dict[str, Any]:
    """Capture the exact requirement inputs used to validate each answer."""
    return {
        "direction": commission.get("direction"),
        "direction_revision": commission.get("direction_revision"),
        "production_scope": commission.get("production_scope"),
        "inputs": {key: inputs[key] for key in sorted(_REQUIREMENT_FIELDS) if key in inputs},
    }


def submitted_reference_parameter_receipt(
    interaction: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Project a durable browser answer into a narrowly scoped parameter receipt."""
    return _parameter_receipt(interaction, require_complete_media=True)


def _parameter_receipt(
    interaction: dict[str, Any] | None, *, require_complete_media: bool,
) -> dict[str, Any] | None:
    if not isinstance(interaction, dict):
        return None
    if not interaction.get("bridge_key") or not interaction.get("clarification_id"):
        return None
    event = interaction.get("event")
    result = interaction.get("result")
    if not isinstance(event, dict) or not isinstance(result, dict):
        return None
    if result.get("ok") is not True or result.get("status") != "clarification_frontend_result":
        return None
    asked = {
        question.get("id") for question in event.get("questions", [])
        if isinstance(question, dict) and question.get("id") in _PARAMETER_FIELDS
    }
    answers = result.get("answers")
    if not asked or not isinstance(answers, dict):
        return None
    accepted = {key: answers[key] for key in asked if key in answers}
    if not accepted:
        return None
    # The draft mapper expects one complete answer set per media type. A
    # supplemental one-field card may inform recommendations, but cannot be
    # replayed as a full parameter submission in a later turn.
    if require_complete_media:
        for media, mandatory in _COMPLETE_FIELDS.items():
            if any(key.startswith(media + "_") for key in accepted) and not mandatory <= accepted.keys():
                return None
    return {
        "status": "submitted", "scope": interaction.get("scope"),
        "requirements_digest": interaction.get("generation_requirements_digest"),
        "message_id": interaction.get("bridge_key"),
        "interaction_id": interaction.get("clarification_id"),
        "requirements_snapshot": interaction.get("requirements_snapshot"),
        "answers": accepted,
    }


def reusable_reference_parameters(
    receipt: dict[str, Any] | None,
    current_scope: dict[str, Any],
    requirements_digest: str,
) -> dict[str, Any] | None:
    if not isinstance(receipt, dict) or receipt.get("status") != "submitted":
        return None
    if receipt.get("scope") != current_scope:
        return None
    if not requirements_digest or receipt.get("requirements_digest") != requirements_digest:
        return None
    if not receipt.get("message_id") or not receipt.get("interaction_id"):
        return None
    answers = receipt.get("answers")
    return dict(answers) if isinstance(answers, dict) and answers else None


def merged_reference_parameter_answers(
    interactions: list[dict[str, Any]] | None,
    current_scope: dict[str, Any],
    requirements_digest: str,
    *, current_requirements: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Merge individually verified cards for one unchanged requirement scope.

    A supplemental card may answer only one missing field. Earlier answers
    remain valid, while a later, distinct submitted card can revise a field.
    """
    if not isinstance(interactions, list):
        return None
    answers: dict[str, Any] = {}
    seen: set[tuple[str, str]] = set()
    for interaction in interactions:
        receipt = _parameter_receipt(interaction, require_complete_media=False)
        if not receipt or receipt.get("scope") != current_scope:
            continue
        if not requirements_digest:
            continue
        exact_requirements = receipt.get("requirements_digest") == requirements_digest
        snapshot = receipt.get("requirements_snapshot")
        if not exact_requirements:
            if not isinstance(snapshot, dict) or not isinstance(current_requirements, dict):
                continue
            if any(snapshot.get(key) != current_requirements.get(key)
                   for key in ("direction", "direction_revision", "production_scope")):
                continue
        identity = (receipt["message_id"], receipt["interaction_id"])
        if identity in seen:
            continue
        seen.add(identity)
        previous_inputs = snapshot.get("inputs") if isinstance(snapshot, dict) else None
        current_inputs = current_requirements.get("inputs") if isinstance(current_requirements, dict) else None
        accepted_fields: dict[str, Any] = {}
        for key, value in receipt["answers"].items():
            if not exact_requirements:
                if not isinstance(previous_inputs, dict) or not isinstance(current_inputs, dict):
                    continue
                missing = object()
                if previous_inputs.get(key, missing) != current_inputs.get(key, missing):
                    continue
                model_key = ("video_model" if key.startswith("video_") else
                             "image_model" if key.startswith("image_") else None)
                if (model_key and previous_inputs.get(model_key, missing) !=
                        current_inputs.get(model_key, missing)):
                    continue
            accepted_fields[key] = value
        for model_key, prefix in (("video_model", "video_"), ("image_model", "image_")):
            if (model_key in accepted_fields and model_key in answers
                    and answers[model_key] != accepted_fields[model_key]):
                for old_key in tuple(answers):
                    if old_key.startswith(prefix):
                        del answers[old_key]
        answers.update(accepted_fields)
    return answers or None


def material_parameter_values(answers: dict[str, Any] | None) -> dict[str, Any]:
    """Convert saved single-choice UI answers to native controls, with no defaults."""
    if not isinstance(answers, dict):
        return {}
    result: dict[str, Any] = {}
    for key, selected in answers.items():
        if key not in _PARAMETER_FIELDS or key == 'video_duration_seconds':
            continue  # Material clip lengths are planned separately per step.
        if isinstance(selected, dict):
            options = selected.get('option_ids')
            if isinstance(options, list) and len(options) == 1:
                selected = options[0]
            elif isinstance(selected.get('custom_text'), str) and selected['custom_text'].strip():
                selected = selected['custom_text'].strip()
            else:
                continue
        if key.endswith('_generate_audio'):
            if isinstance(selected, str):
                selected = {'true': True, 'false': False}.get(selected.casefold())
            if type(selected) is not bool:
                continue
        elif key.endswith('_variants_per_node'):
            if isinstance(selected, str) and selected in {'1', '2', '4'}:
                selected = int(selected)
            if type(selected) is not int or selected not in {1, 2, 4}:
                continue
        elif not isinstance(selected, str) or not selected.strip():
            continue
        result[key] = selected
    return result
