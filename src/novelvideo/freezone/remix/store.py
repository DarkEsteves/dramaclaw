"""Project-scoped immutable storage for structured remix artifacts."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path
import re
import tempfile
import uuid
from typing import Any, Callable

from .contracts import RemixArtifactEnvelope, validate_payload


_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_MAX_FILE_BYTES = 8 * 1024 * 1024


def _speech_source_signature(plan: dict[str, Any], take_id: str) -> tuple[Any, ...] | None:
    """The source facts a recognition or correction actually measured."""
    take = next((row for row in plan.get("speech_takes", []) if row["id"] == take_id), None)
    if take is None:
        return None
    resource = next((row for row in plan.get("resources", []) if row["id"] == take["resource_id"]), None)
    turns = {turn["id"]: turn for beat in plan["beats"] for turn in beat.get("turns", [])}
    if resource is None or any(turn_id not in turns for turn_id in take["turn_ids"]):
        return None
    return (
        take["beat_id"], take["resource_id"], take["origin_placement_id"],
        take["source_in_us"], take.get("source_out_us"), take["speed_num"],
        take["speed_den"], take["lead_in_frames"], tuple(take["turn_ids"]),
        resource["media_path"], resource["media_hash"], resource["duration_us"],
        tuple((turn_id, turns[turn_id]["speaker"], turns[turn_id]["text"])
              for turn_id in take["turn_ids"]),
    )


class RemixArtifactConflict(ValueError):
    """The supplied expected revision is no longer current."""


class RemixArtifactStore:
    def __init__(self, project_dir: Path, project_id: str, *, created_by: str = "", producer_version: str = "remix-store.v1") -> None:
        self.project_dir = Path(project_dir).resolve()
        self.project_id = self._project_id(project_id)
        self.created_by = created_by
        self.producer_version = producer_version
        self.root = self.project_dir / "freezone" / "_remix_artifacts"

    @staticmethod
    def _project_id(project_id: str) -> str:
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError("project_id is required")
        return project_id

    @staticmethod
    def _validate_id(artifact_id: str) -> None:
        if not isinstance(artifact_id, str) or not _ID.fullmatch(artifact_id):
            raise ValueError("Invalid remix artifact ID")

    @staticmethod
    def _validate_revision(revision: int) -> None:
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise ValueError("Revision must be positive")

    def _directory(self, artifact_id: str) -> Path:
        self._validate_id(artifact_id)
        path = self.root / artifact_id
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ValueError("Invalid remix artifact directory")
        return path

    @contextmanager
    def _locked(self):
        from novelvideo.ports import get_canvas_write_mutex

        self.project_dir.mkdir(parents=True, exist_ok=True)
        freezone = self.project_dir / "freezone"
        if freezone.is_symlink() or self.root.is_symlink():
            raise ValueError("Remix artifact storage contains a symlink")
        freezone.mkdir(exist_ok=True)
        self.root.mkdir(exist_ok=True)
        with get_canvas_write_mutex().write_mutex(self.project_dir, "remix_artifacts") as guard:
            yield guard

    @staticmethod
    def _canonical(value: dict[str, Any]) -> bytes:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _reference_list(upstream_refs: list[dict[str, Any]], project_id: str) -> list[dict[str, Any]]:
        if not isinstance(upstream_refs, list):
            raise ValueError("upstream_refs must be a list")
        normalized: list[dict[str, Any]] = []
        for reference in upstream_refs:
            if not isinstance(reference, dict):
                raise ValueError("upstream_refs must contain objects")
            artifact_id = reference.get("artifact_id")
            revision = reference.get("revision")
            ref_project = reference.get("project_id", project_id)
            if ref_project != project_id:
                raise ValueError("Cross-project artifact references are not allowed")
            RemixArtifactStore._validate_id(artifact_id)
            RemixArtifactStore._validate_revision(revision)
            normalized.append({"artifact_id": artifact_id, "revision": revision, "project_id": project_id})
        return normalized

    def _path(self, artifact_id: str, revision: int, suffix: str) -> Path:
        self._validate_revision(revision)
        return self._directory(artifact_id) / f"{revision}{suffix}"

    def _validate_upstream_refs(self, refs: list[dict[str, Any]]) -> None:
        """Resolve every declared dependency within this project's storage scope."""
        for reference in refs:
            self._read(reference["artifact_id"], reference["revision"])

    def _validate_v2_lineage(self, schema: str, payload: dict[str, Any], refs: list[dict[str, Any]], *, target_artifact_id: str | None = None, previous_plan: dict[str, Any] | None = None) -> None:
        if schema == "production_output_review.v1":
            from .production_output_review import validate_production_output_review_lineage
            validate_production_output_review_lineage(self, payload, refs)
            return
        if schema == "material_work_package.v1":
            from .material_work import validate_material_work_lineage
            validate_material_work_lineage(self, payload, refs)
            return
        if schema == "material_review.v1":
            from .material_review import validate_material_review_lineage
            validate_material_review_lineage(self, payload, refs)
            return
        if schema == "reference_commission.v1":
            from .reference_branches import validate_reference_branch_lineage
            validate_reference_branch_lineage(self, payload, refs)
            if payload["project_id"] != self.project_id:
                raise ValueError("commission belongs to another project")
            if previous_plan is not None:
                for field in ("project_id", "canvas_id", "requester_user_id", "analysis_node_id", "analysis_evidence"):
                    if payload[field] != previous_plan["payload"][field]:
                        raise ValueError("commission source or actor cannot change")
            for field, expected_schema in (("plan_ref", "production_plan.v2"), ("review_ref", "production_review.v1"), ("work_ref", "material_work_package.v1"), ("manifest_ref", "composition_manifest.v2"), ("output_review_ref", "production_output_review.v1")):
                ref = payload.get(field)
                if ref is None:
                    continue
                if not any(row["artifact_id"] == ref.get("artifact_id") and row["revision"] == ref.get("revision") for row in refs):
                    raise ValueError("commission requires its saved dependency")
                if self._read(ref["artifact_id"], ref["revision"])["schema"] != expected_schema:
                    raise ValueError("commission dependency schema differs")
            if previous_plan is not None:
                previous_decisions = previous_plan['payload']['decisions']
                if payload['decisions'][:len(previous_decisions)] != previous_decisions:
                    raise ValueError('commission host decision receipts cannot change')
            if payload['phase'] in {'preparing_materials','aligning_speech','composing','reviewing_output','completed'} and payload.get('plan_ref'):
                from .reference_commission import _plan_approval_receipts
                from .production_review import production_decision_fingerprint
                plan_ref = payload['plan_ref']
                plan = self._read(plan_ref['artifact_id'],plan_ref['revision'])
                current_plan = self._read(plan_ref['artifact_id'],self._latest_revision(plan_ref['artifact_id']))
                review_ref = payload['review_ref']
                review = self._read(review_ref['artifact_id'],review_ref['revision'])['payload']
                direction_ref = review['direction_ref']
                direction = self._read(direction_ref['artifact_id'],direction_ref['revision'])['payload']
                if (review['status'] != 'passed'
                    or review['plan_ref']['artifact_id'] != plan['id']
                    or direction_ref['artifact_id'] != target_artifact_id
                    or direction['direction_revision'] != payload['direction_revision']
                    or review['direction_fingerprint'] != payload['direction_fingerprint']
                    or direction['direction_fingerprint'] != payload['direction_fingerprint']
                    or production_decision_fingerprint(plan['payload']) != review['decision_fingerprint']
                    or production_decision_fingerprint(current_plan['payload']) != review['decision_fingerprint']):
                    raise ValueError('creative plan approval lineage changed')
                approved = False
                for receipt in _plan_approval_receipts(payload):
                    confirmed_ref = receipt['plan_ref']
                    confirmed = self._read(confirmed_ref['artifact_id'],confirmed_ref['revision'])
                    if (review['plan_ref']['revision'] <= confirmed['revision']
                        and production_decision_fingerprint(confirmed['payload']) == review['decision_fingerprint']):
                        approved = True
                        break
                if not approved:
                    raise ValueError('creative plan approval requires its original host receipt')
            if payload['phase'] in {'aligning_speech','composing','reviewing_output','completed'} and payload.get('plan_ref'):
                ref = payload['plan_ref']
                if self._latest_revision(ref['artifact_id']) != ref['revision']:
                    raise RemixArtifactConflict('production plan changed before stage publication')
                if payload.get('work_ref'):
                    from .material_work import validate_material_completion
                    work_ref = payload['work_ref']
                    work = self._read(work_ref['artifact_id'], self._latest_revision(work_ref['artifact_id']))
                    validate_material_completion(self, work, self._read(ref['artifact_id'],ref['revision'])['payload'])
            return
        if schema == "production_review.v1":
            from .production_review import evaluate_saved_production_review, production_decision_fingerprint, _fingerprint
            from .production_inventory import _LockedEvidenceStore
            for field, expected_schema in (("plan_ref", "production_plan.v2"), ("direction_ref", "reference_commission.v1")):
                ref = payload[field]
                if not any(row["artifact_id"] == ref.get("artifact_id") and row["revision"] == ref.get("revision") for row in refs):
                    raise ValueError("production review requires exact dependencies")
                if self._read(ref["artifact_id"], ref["revision"])["schema"] != expected_schema:
                    raise ValueError("production review dependency schema differs")
            plan = self._read(payload["plan_ref"]["artifact_id"], payload["plan_ref"]["revision"])
            direction = self._read(payload["direction_ref"]["artifact_id"], payload["direction_ref"]["revision"])
            if self._latest_revision(plan["id"]) != plan["revision"]:
                raise RemixArtifactConflict("plan changed before publishing review")
            current_direction = self._read(direction["id"], self._latest_revision(direction["id"]))["payload"]
            if not direction["payload"].get("direction_revision") or payload["direction_fingerprint"] != _fingerprint(direction["payload"].get("direction")) or current_direction.get("direction_fingerprint") != direction["payload"].get("direction_fingerprint"):
                raise RemixArtifactConflict("review direction changed or was not confirmed")
            if payload["plan_content_hash"] != plan["content_hash"] or payload["decision_fingerprint"] != production_decision_fingerprint(plan["payload"]):
                raise ValueError("production review plan differs")
            # Lineage validation is already inside the caller's artifact lease.
            # Reuse its read path while resolving the saved source dialogue;
            # public read/read_latest would try to acquire the same OS lock.
            class _LineageGuard:
                def reassert(self):
                    return None

            evaluated = evaluate_saved_production_review(
                _LockedEvidenceStore(self, _LineageGuard()),
                plan['payload'], direction, payload['findings'],
                plan_ref=payload['plan_ref'],
            )
            if payload["status"] != evaluated["status"] or payload["checks"] != evaluated["checks"]:
                raise ValueError("production review does not match server checks")
            return
        if schema == "composition_manifest.v2":
            from .production_plan_v2 import compile_production_plan_v2

            sources = payload.get("source_versions", [])
            if len(sources) != 1 or not isinstance(sources[0], dict) or set(sources[0]) != {"artifact_id", "revision"}:
                raise ValueError("v2 manifest requires one exact saved source revision")
            source = sources[0]
            if not any(ref["artifact_id"] == source["artifact_id"] and ref["revision"] == source["revision"] for ref in refs):
                raise ValueError("saved source revision must be included in upstream_refs")
            artifact = self._read(source["artifact_id"], source["revision"])
            if artifact["schema"] != "production_plan.v2":
                raise ValueError("v2 manifest requires a production_plan.v2 saved source revision")
            expected = compile_production_plan_v2(artifact["payload"])
            expected["source_versions"] = sources
            if payload != expected:
                raise ValueError("manifest differs from its exact saved source revision; revise the plan and compile again")
            return
        if schema == "word_timeline.v1" and self.producer_version == "remix-adopt-source-speech.v1":
            from .source_speech_adoption import validate_adopted_timeline
            validate_adopted_timeline(self, payload, refs)
            return
        if schema in {"speech_correction_preview.v1", "speech_correction_review.v1"}:
            from .agent_speech_review import validate_speech_review_lineage
            validate_speech_review_lineage(self, schema, payload, refs)
            return
        if schema == "speech_cue_evidence.v1":
            from .agent_speech_review import validate_correction_provenance
            validate_correction_provenance(self, payload, refs)
            ref = payload["source_plan_ref"]
            if not any(row["artifact_id"] == ref["artifact_id"] and row["revision"] == ref["revision"] for row in refs):
                raise ValueError("cue evidence source_plan_ref must be included in upstream_refs")
            source = self._read(ref["artifact_id"], ref["revision"])
            if source["schema"] != "production_plan.v2":
                raise ValueError("cue evidence requires a saved production plan")
            plan = source["payload"]
            take = next((row for row in plan.get("speech_takes", []) if row["id"] == payload["take_id"]), None)
            resource = next((row for row in plan["resources"] if take and row["id"] == take["resource_id"]), None)
            if resource is None or resource["media_hash"] != payload["media_hash"]:
                raise ValueError("cue evidence source differs from the saved take")
            recognition_ref = payload.get("recognition_ref")
            if recognition_ref is not None:
                if not any(row["artifact_id"] == recognition_ref["artifact_id"]
                           and row["revision"] == recognition_ref["revision"] for row in refs):
                    raise ValueError("cue evidence recognition_ref must be included in upstream_refs")
                recognition_artifact = self._read(recognition_ref["artifact_id"], recognition_ref["revision"])
                recognition = recognition_artifact["payload"]
                recognition_plan_ref = recognition.get("plan_ref")
                if (recognition_artifact["schema"] != "word_timeline.v1"
                    or recognition_artifact.get("producer_version") != "remix-align-production-speech.v1"
                    or recognition.get("status") != "script_mismatch"
                    or not recognition.get("recognized_words")
                    or recognition.get("media_hash") != "sha256:" + resource["media_hash"]
                    or recognition.get("placement_id") != take["origin_placement_id"]
                    or not isinstance(recognition_plan_ref, dict)
                    or recognition_plan_ref.get("artifact_id") != ref["artifact_id"]
                    or recognition.get("turn_id") != take["turn_ids"][0]
                    or (recognition.get("group_turn_ids") is not None
                        and recognition["group_turn_ids"] != take["turn_ids"])
                    or (len(take["turn_ids"]) > 1
                        and recognition.get("group_turn_ids") != take["turn_ids"])
                    or recognition.get("source_in_us") != 0
                    or recognition.get("source_out_us") != resource["duration_us"]):
                    raise ValueError("cue evidence recognition_ref differs from the saved take or trusted alignment")
                if not any(row["artifact_id"] == recognition_plan_ref["artifact_id"]
                           and row["revision"] == recognition_plan_ref.get("revision")
                           for row in recognition_artifact["upstream_refs"]):
                    raise ValueError("cue evidence recognition_ref lacks its exact source plan dependency")
                recognized_plan = self._read(recognition_plan_ref["artifact_id"], recognition_plan_ref["revision"])
                if (recognized_plan["schema"] != "production_plan.v2"
                    or _speech_source_signature(recognized_plan["payload"], take["id"])
                    != _speech_source_signature(plan, take["id"])):
                    raise ValueError("cue evidence recognition_ref differs from the saved take source revision")
            turns = {turn["id"]: turn for beat in plan["beats"] for turn in beat.get("turns", [])}
            imported_rows = []
            if payload["basis"] == "srt_import":
                from .speech_cues import import_source_srt, split_srt_speaker_prefix
                imported_rows = import_source_srt(payload["original_srt"], payload["take_id"])
            for index, cue in enumerate(payload["cues"]):
                if cue["turn_id"] not in take["turn_ids"] or cue["turn_id"] not in turns:
                    raise ValueError("cue evidence turn does not belong to the saved take")
                if imported_rows and imported_rows[index]["text"] != cue["text"]:
                    prefix = split_srt_speaker_prefix(imported_rows[index]["text"])
                    if prefix is None or prefix[0] != turns[cue["turn_id"]]["speaker"] or prefix[1] != cue["text"]:
                        raise ValueError("SRT speaker prefix differs from the bound turn speaker")
                normalized_text = "".join(cue["text"].split())
                if not normalized_text or normalized_text not in "".join(turns[cue["turn_id"]]["text"].split()):
                    raise ValueError("cue evidence text differs from the bound spoken turn")
                if cue["start_us"] < take["source_in_us"] or cue["end_us"] > (take.get("source_out_us") or resource["duration_us"]):
                    raise ValueError("cue evidence exceeds the take playback interval")
                if cue["end_us"] > resource["duration_us"]:
                    raise ValueError("cue evidence exceeds source duration")
            corrected_words = payload.get("corrected_words", [])
            if corrected_words:
                from .speech_correction import preview_recognized_words_to_plan_cues
                try:
                    expected = preview_recognized_words_to_plan_cues(
                        corrected_words,
                        [{"id": turn_id, "text": turns[turn_id]["text"]} for turn_id in take["turn_ids"]],
                        resource["duration_us"], take["source_in_us"], take.get("source_out_us"),
                    )
                except ValueError as exc:
                    raise ValueError(f"corrected word text or timing differs from ordered plan turns: {exc}") from exc
                actual = [(cue["turn_id"], cue["text"], cue["start_us"], cue["end_us"])
                          for cue in payload["cues"]]
                projected = [(row["turn_id"], row["text"], row["source_start_us"], row["source_end_us"])
                             for row in expected]
                if actual != projected:
                    raise ValueError("corrected word text or timing differs from ordered plan turns and accepted SRT cues")
            return
        if schema != "production_plan.v2":
            return
        # Branching a conflicted plan keeps accepted timing evidence. The exact
        # branch source is declared and retained as an upstream dependency, so
        # evidence from an unrelated plan remains invalid.
        allowed_plan_ids = {target_artifact_id}
        branch_ref = payload.get("provenance", {}).get("branch_source_ref")
        seen_branches: set[str] = set()
        first_branch = True
        while branch_ref:
            if branch_ref["artifact_id"] in seen_branches or branch_ref["artifact_id"] == target_artifact_id:
                raise ValueError("production plan branch lineage contains a cycle")
            if first_branch and not any(row["artifact_id"] == branch_ref["artifact_id"]
                                        and row["revision"] == branch_ref["revision"] for row in refs):
                raise ValueError("branch_source_ref must be included in upstream_refs")
            ancestor = self._read(branch_ref["artifact_id"], branch_ref["revision"])
            if ancestor["schema"] != "production_plan.v2":
                raise ValueError("branch_source_ref must reference production_plan.v2")
            seen_branches.add(branch_ref["artifact_id"])
            allowed_plan_ids.add(branch_ref["artifact_id"])
            branch_ref = ancestor["payload"].get("provenance", {}).get("branch_source_ref")
            first_branch = False
        previously_accepted = [previous_plan] if previous_plan is not None else []
        branch_source = payload.get("provenance", {}).get("branch_source_ref")
        if branch_source:
            previously_accepted.append(self._read(branch_source["artifact_id"], branch_source["revision"]))
        takes = {row["id"]: row for row in payload.get("speech_takes", [])}
        resources = {row["id"]: row for row in payload.get("resources", [])}
        for cue in payload.get("speech_cues", []):
            if cue["timing_source"] == "asr" and cue["timing_status"] == "ready":
                turn = next(turn for beat in payload["beats"] for turn in beat.get("turns", []) if turn["id"] == cue["turn_id"])
                timing = turn.get("speech_timing")
                words = {row["id"]: row for row in (timing or {}).get("words", [])}
                selected = [words[word_id] for word_id in cue.get("word_ids", []) if word_id in words]
                take = takes[cue["take_id"]]
                resource = resources[take["resource_id"]]
                if (not timing or timing["evidence_ref"] != cue.get("evidence_ref")
                    or timing["placement_id"] != take["origin_placement_id"]
                    or timing["media_hash"] != resource["media_hash"]
                    or timing["text"] != turn["text"]
                    or timing["text_hash"] != hashlib.sha256(turn["text"].encode()).hexdigest()
                    or not selected or len(selected) != len(cue.get("word_ids", []))
                    or min(row["start_us"] for row in selected) != cue["source_start_us"]
                    or max(row["end_us"] for row in selected) != cue["source_end_us"]):
                    raise ValueError("ASR cue evidence differs from accepted source words")
            if cue["timing_source"] not in {"manual", "srt_import"} or cue["timing_status"] != "ready":
                continue
            declared = cue.get("evidence_ref")
            if not declared or not any(row["artifact_id"] == declared["artifact_id"] and row["revision"] == declared["revision"] for row in refs):
                raise ValueError("ready cue evidence_ref must be included in upstream_refs")
            artifact = self._read(declared["artifact_id"], declared["revision"])
            evidence = artifact["payload"]
            take = takes[cue["take_id"]]
            resource = resources[take["resource_id"]]
            source_ref = evidence.get("source_plan_ref")
            if source_ref is None:
                raise ValueError("cue evidence requires exact source plan lineage")
            if source_ref["artifact_id"] not in allowed_plan_ids:
                raise ValueError("cue evidence must originate from the same production plan")
            source_plan = self._read(source_ref["artifact_id"], source_ref["revision"])["payload"]
            source_turn = next((turn for beat in source_plan["beats"] for turn in beat.get("turns", []) if turn["id"] == cue["turn_id"]), None)
            current_turn = next(turn for beat in payload["beats"] for turn in beat.get("turns", []) if turn["id"] == cue["turn_id"])
            if source_turn is None or source_turn["text"] != current_turn["text"]:
                raise ValueError("cue evidence belongs to a different spoken text revision")
            accepted = {"id": cue["id"], "turn_id": cue["turn_id"], "text": cue["text"],
                        "start_us": cue["source_start_us"], "end_us": cue["source_end_us"]}
            if (artifact["schema"] != "speech_cue_evidence.v1" or evidence["basis"] != cue["timing_source"]
                or evidence["take_id"] != take["id"] or evidence["media_hash"] != resource["media_hash"]
                or accepted not in evidence["cues"]):
                raise ValueError("cue evidence differs from accepted source timing")
            corrected_evidence = bool(evidence.get("corrected_words")) and bool(evidence.get("recognition_ref"))
            if corrected_evidence and _speech_source_signature(source_plan, take["id"]) != _speech_source_signature(payload, take["id"]):
                raise ValueError("corrected cue evidence differs from the current speech take source")
            if corrected_evidence or cue.get("word_ids"):
                timing = current_turn.get("speech_timing") or {}
                if corrected_evidence and not timing:
                    accepted_legacy = any(
                        prior["schema"] == "production_plan.v2"
                        and any(old_cue.get("id") == cue["id"] and old_cue == cue
                                for old_cue in prior["payload"].get("speech_cues", []))
                        and any(old_turn["id"] == current_turn["id"] and not old_turn.get("speech_timing")
                                for old_beat in prior["payload"]["beats"] for old_turn in old_beat.get("turns", []))
                        for prior in previously_accepted
                    )
                    if accepted_legacy:
                        continue
                    raise ValueError("cue word IDs require corrected speech timing")
                words = {word["id"]: word for word in timing.get("words", [])}
                selected = [words[word_id] for word_id in cue["word_ids"] if word_id in words]
                expected_words = [word for word in evidence.get("corrected_words") or []
                                  if accepted["start_us"] <= word["start_us"]
                                  and word["end_us"] <= accepted["end_us"]]
                if (evidence.get("recognition_ref") is None
                    or timing.get("evidence_ref") != declared
                    or (corrected_evidence and not cue["word_ids"])
                    or len(selected) != len(cue["word_ids"])
                    or [(word["text"], word["start_us"], word["end_us"]) for word in selected]
                    != [(word["text"], word["start_us"], word["end_us"]) for word in expected_words]):
                    raise ValueError("cue word IDs differ from corrected source words")
        provenance = payload.get("provenance", {})
        for field, expected_schema in (("source_plan_ref", "production_plan.v1"),
                                       ("blueprint_ref", "reference_blueprint.v1"),
                                       ("branch_source_ref", "production_plan.v2")):
            declared = provenance.get(field)
            if declared is None:
                continue
            if not any(ref["artifact_id"] == declared["artifact_id"] and ref["revision"] == declared["revision"] for ref in refs):
                raise ValueError(f"{field} must be included in upstream_refs")
            if self._read(declared["artifact_id"], declared["revision"])["schema"] != expected_schema:
                raise ValueError(f"{field} must reference {expected_schema}")
        for beat in payload.get("beats", []):
            for turn in beat.get("turns", []):
                timing = turn.get("speech_timing")
                if not timing:
                    continue
                declared = timing["evidence_ref"]
                if not any(ref["artifact_id"] == declared["artifact_id"] and ref["revision"] == declared["revision"] for ref in refs):
                    raise ValueError("speech timing evidence_ref must be included in upstream_refs")
                artifact = self._read(declared["artifact_id"], declared["revision"])
                evidence = artifact["payload"]
                corrected_evidence = artifact["schema"] == "speech_cue_evidence.v1" and bool(evidence.get("corrected_words")) and bool(evidence.get("recognition_ref"))
                source_ref = evidence.get("plan_ref") or evidence.get("source_plan_ref")
                adopted = artifact.get("producer_version") == "remix-adopt-source-speech.v1"
                if adopted:
                    from .source_speech_adoption import validate_adopted_timeline
                    validate_adopted_timeline(self, evidence, artifact["upstream_refs"])
                    if [word["id"] for word in timing["words"]] != [word["id"] for word in evidence.get("words", [])]:
                        raise ValueError("adopted timing differs from immutable source word identities")
                    from .speech_correction import _spoken
                    source_words = {word["id"]: word for word in evidence["words"]}
                    ordered_indices = {word_id: index for index, word_id in enumerate(source_words)}
                    for cue in payload.get("speech_cues", []):
                        if cue["turn_id"] != turn["id"] or cue["timing_status"] != "ready" or cue["timing_source"] != "asr":
                            continue
                        selected_ids = cue["word_ids"]
                        indices = [ordered_indices[word_id] for word_id in selected_ids if word_id in source_words]
                        if (len(indices) != len(selected_ids) or not indices
                            or indices != list(range(indices[0], indices[-1] + 1))
                            or _spoken(cue["text"]) != _spoken("".join(source_words[word_id]["text"] for word_id in selected_ids))):
                            raise ValueError("adopted cue text differs from ordered measured source words")
                    if _speech_source_signature(self._read(evidence["plan_ref"]["artifact_id"], evidence["plan_ref"]["revision"])["payload"], evidence["take_id"]) != _speech_source_signature(payload, evidence["take_id"]):
                        raise ValueError("adopted speech timing differs from current take source")
                if artifact.get("producer_version") not in {"remix-align-production-speech.v1", "remix-adopt-source-speech.v1"} and not corrected_evidence:
                    # Historical runners used the generic store producer. Carry
                    # forward only the unchanged accepted timing, never newly
                    # submitted client evidence bearing the same old marker.
                    accepted_legacy = artifact.get("producer_version") == "remix-store.v1" and any(
                        prior["schema"] == "production_plan.v2"
                        and any(row["artifact_id"] == declared["artifact_id"] and row["revision"] == declared["revision"]
                                for row in prior["upstream_refs"])
                        and any(old_turn["id"] == turn["id"] and old_turn.get("speech_timing") == timing
                                for old_beat in prior["payload"]["beats"] for old_turn in old_beat.get("turns", []))
                        for prior in previously_accepted
                    )
                    if not accepted_legacy:
                        raise ValueError("speech timing evidence_ref requires server alignment evidence")
                if (not isinstance(source_ref, dict) or source_ref.get("artifact_id") not in allowed_plan_ids
                    or not any(row["artifact_id"] == source_ref.get("artifact_id") and row["revision"] == source_ref.get("revision")
                               for row in artifact["upstream_refs"])):
                    raise ValueError("speech timing evidence_ref must originate from the same production plan")
                source_plan = self._read(source_ref["artifact_id"], source_ref["revision"])
                if source_plan["schema"] != "production_plan.v2":
                    raise ValueError("speech timing evidence_ref requires production plan lineage")
                source_turn = next((row for source_beat in source_plan["payload"]["beats"] for row in source_beat.get("turns", [])
                                    if row["id"] == turn["id"]), None)
                source_payload = source_plan["payload"]
                source_placement = next((row for row in source_payload.get("placements", []) if row["id"] == timing["placement_id"]), None)
                source_take = next((row for row in source_payload.get("speech_takes", [])
                                    if row["origin_placement_id"] == timing["placement_id"] and turn["id"] in row["turn_ids"]), None)
                source_resource_id = (source_take or source_placement or {}).get("resource_id")
                source_resource = next((row for row in source_payload.get("resources", []) if row["id"] == source_resource_id), None)
                if source_resource is None or source_resource["media_hash"] != timing["media_hash"]:
                    raise ValueError("speech timing evidence_ref differs from its source media revision")
                if (source_turn is None or source_turn["text"] != turn["text"]
                    or timing["text"] != turn["text"]
                    or timing["text_hash"] != hashlib.sha256(turn["text"].encode()).hexdigest()):
                    raise ValueError("speech timing evidence_ref belongs to a different spoken text revision")
                if corrected_evidence:
                    own_cues = [row for row in evidence["cues"] if row["turn_id"] == turn["id"]]
                    expected_words = [word for word in evidence["corrected_words"]
                                      if any(row["start_us"] <= word["start_us"]
                                             and word["end_us"] <= row["end_us"] for row in own_cues)]
                    if (evidence["take_id"] not in {take["id"] for take in payload.get("speech_takes", []) if take["origin_placement_id"] == timing["placement_id"] and turn["id"] in take["turn_ids"]}
                        or evidence["media_hash"] != timing["media_hash"]
                        or [(word["text"], word["start_us"], word["end_us"]) for word in timing["words"]]
                        != [(word["text"], word["start_us"], word["end_us"]) for word in expected_words]):
                        raise ValueError("speech timing evidence_ref differs from corrected source words")
                    continue
                if (artifact["schema"] != "word_timeline.v1" or evidence.get("status") != "completed"
                    or evidence.get("turn_id") != turn["id"]
                    or evidence.get("placement_id") != timing["placement_id"]
                    or evidence.get("media_hash") != "sha256:" + timing["media_hash"]
                    or evidence.get("text_hash") != timing["text_hash"]
                    or (timing.get("source_in_us") is not None and evidence.get("source_in_us") != timing["source_in_us"])
                    or (timing.get("source_out_us") is not None and evidence.get("source_out_us") != timing["source_out_us"])
                    or [(word.get("text"), word.get("start_us"), word.get("end_us")) for word in evidence.get("words", [])]
                    != [(word["text"], word["start_us"], word["end_us"]) for word in timing["words"]]
                    or any((word.get("start_char"), word.get("end_char")) != (source.get("start_char"), source.get("end_char"))
                           for word, source in zip(timing["words"], evidence.get("words", []), strict=True)
                           if word.get("start_char") is not None or word.get("end_char") is not None)):
                    raise ValueError("speech timing evidence_ref does not match the accepted source words")

    def _read_bytes(self, path: Path) -> bytes:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as exc:
            raise FileNotFoundError("Artifact or revision not found") from exc
        try:
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("Invalid remix artifact file")
                if info.st_size > _MAX_FILE_BYTES:
                    raise ValueError("Remix artifact file exceeds size limit")
                content = stream.read(_MAX_FILE_BYTES + 1)
                if len(content) > _MAX_FILE_BYTES:
                    raise ValueError("Remix artifact file exceeds size limit")
        except OSError as exc:
            raise ValueError("Invalid remix artifact file") from exc
        return content

    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            value = json.loads(self._read_bytes(path).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid remix artifact file") from exc
        if not isinstance(value, dict):
            raise ValueError("Invalid remix artifact file")
        return value

    def _read(self, artifact_id: str, revision: int) -> dict[str, Any]:
        envelope_path = self._path(artifact_id, revision, ".json")
        raw = self._read_json(envelope_path)
        try:
            envelope = RemixArtifactEnvelope.model_validate(raw)
        except Exception as exc:
            raise ValueError("Invalid remix artifact envelope") from exc
        if envelope.id != artifact_id or envelope.revision != revision or envelope.project_id != self.project_id:
            raise FileNotFoundError("Artifact or revision not found")
        expected_payload_path = f"{revision}.payload.json"
        if envelope.payload_path != expected_payload_path:
            raise ValueError("Invalid remix artifact payload path")
        payload_path = envelope_path.parent / envelope.payload_path
        payload = self._read_json(payload_path)
        if "sha256:" + hashlib.sha256(self._canonical(payload)).hexdigest() != envelope.content_hash:
            raise ValueError("Remix artifact content hash mismatch")
        validated = validate_payload(envelope.schema_name, payload)
        return {**envelope.model_dump(mode="json", by_alias=True), "payload": validated}

    def _latest_revision(self, artifact_id: str) -> int:
        directory = self._directory(artifact_id)
        revisions = [int(entry.stem) for entry in directory.glob("*.json") if entry.stem.isdecimal()]
        if not revisions:
            raise FileNotFoundError("Artifact or revision not found")
        return max(revisions)

    def production_cache_epoch(self) -> str:
        """Read a tiny invalidation marker without scanning artifact history."""
        path = self.root / "_production_epoch.json"
        if not path.exists() and not path.is_symlink():
            return "legacy"
        value = self._read_json(path).get("epoch")
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
            raise ValueError("invalid production cache epoch")
        return value

    def _atomic_json(self, target: Path, value: dict[str, Any], guard: Any) -> None:
        fd, temporary_name = tempfile.mkstemp(prefix=".remix-", suffix=".tmp", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(self._canonical(value))
                stream.flush()
                os.fsync(stream.fileno())
            guard.reassert()
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def _publish(
        self, *, artifact_id: str, schema: str, revision: int, payload: dict[str, Any],
        upstream_refs: list[dict[str, Any]], created_by: str, guard: Any,
    ) -> dict[str, Any]:
        directory = self._directory(artifact_id)
        directory.mkdir(exist_ok=True)
        envelope_path = self._path(artifact_id, revision, ".json")
        if envelope_path.exists():
            raise RemixArtifactConflict("Artifact revision already exists")
        payload_path = self._path(artifact_id, revision, ".payload.json")
        content_hash = "sha256:" + hashlib.sha256(self._canonical(payload)).hexdigest()
        envelope = RemixArtifactEnvelope(
            id=artifact_id, schema=schema, revision=revision, project_id=self.project_id,
            created_by=created_by, content_hash=content_hash, upstream_refs=upstream_refs,
            producer_version=self.producer_version, payload_path=payload_path.name,
        ).model_dump(mode="json", by_alias=True)
        # Invalidate display indexes before attempting publication. A crash
        # leaves a cold cache rather than a falsely current production view.
        self._atomic_json(self.root / "_production_epoch.json", {"epoch": uuid.uuid4().hex}, guard)
        # The envelope is the commit record. A crashed writer can leave an orphaned
        # payload, but readers can never observe a published partial revision.
        self._atomic_json(payload_path, payload, guard)
        self._atomic_json(envelope_path, envelope, guard)
        return {**envelope, "payload": payload}

    def create(self, schema: str, payload: dict[str, Any], upstream_refs: list[dict[str, Any]]) -> dict[str, Any]:
        if schema == "production_plan.v2":
            from .authored_text_timing import materialize_text_anchors
            payload = materialize_text_anchors(payload)
        validated = validate_payload(schema, payload)
        refs = self._reference_list(upstream_refs, self.project_id)
        artifact_id = uuid.uuid4().hex
        with self._locked() as guard:
            self._validate_upstream_refs(refs)
            self._validate_v2_lineage(schema, validated, refs, target_artifact_id=artifact_id)
            return self._publish(
                artifact_id=artifact_id, schema=schema, revision=1, payload=validated,
                upstream_refs=refs, created_by=self.created_by, guard=guard,
            )

    def read(self, artifact_id: str, revision: int) -> dict[str, Any]:
        self._validate_id(artifact_id)
        self._validate_revision(revision)
        with self._locked():
            return self._read(artifact_id, revision)

    def read_latest(self, artifact_id: str) -> dict[str, Any]:
        self._validate_id(artifact_id)
        with self._locked():
            return self._read(artifact_id, self._latest_revision(artifact_id))

    def repair_blank_created_by(
        self, *, artifact_id: str, expected_revision: int, actor: str,
        operation_id: str, verify_provenance: Callable[[], str],
        apply: bool = False,
    ) -> dict[str, Any]:
        """Repair one legacy envelope after its native provenance was checked.

        This maintenance entry point changes only envelope metadata. The
        caller provides a provenance reader which is rerun under this lock.
        """
        self._validate_id(artifact_id)
        self._validate_revision(expected_revision)
        actor = str(actor or "").strip()
        if not actor or not re.fullmatch(r"[a-f0-9]{32}", operation_id):
            raise ValueError("verified actor and operation identity are required")
        with self._locked() as guard:
            if verify_provenance() != actor:
                raise ValueError("artifact repair provenance changed")
            if self._latest_revision(artifact_id) != expected_revision:
                raise RemixArtifactConflict("artifact revision changed before actor repair")
            artifact = self._read(artifact_id, expected_revision)
            if artifact["schema"] not in {"production_plan.v2", "composition_manifest.v2",
                                          "reference_evidence.v1", "reference_blueprint.v1",
                                          "reference_analysis.v1"}:
                raise ValueError("actor repair is limited to native production artifacts")
            current_actor = str(artifact.get("created_by") or "").strip()
            if current_actor:
                if current_actor != actor:
                    raise ValueError("artifact has a conflicting nonblank owner")
                return {"status": "already_repaired", "artifact_id": artifact_id,
                        "revision": expected_revision, "created_by": actor}
            if not apply:
                return {"status": "would_repair", "artifact_id": artifact_id,
                        "revision": expected_revision, "created_by": actor}
            envelope_path = self._path(artifact_id, expected_revision, ".json")
            envelope = self._read_json(envelope_path)
            backup_dir = self.root / "_actor_repair_backups"
            if backup_dir.is_symlink():
                raise ValueError("actor repair backup directory must not be a symlink")
            backup_dir.mkdir(exist_ok=True)
            backup_path = backup_dir / f"{artifact_id}-{expected_revision}.json"
            if backup_path.exists():
                if self._read_json(backup_path) != envelope:
                    raise ValueError("actor repair backup differs from current envelope")
            else:
                self._atomic_json(backup_path, envelope, guard)
            repaired = {**envelope, "created_by": actor}
            self._atomic_json(self.root / "_production_epoch.json", {"epoch": uuid.uuid4().hex}, guard)
            self._atomic_json(envelope_path, repaired, guard)
            audit = {"artifact_id": artifact_id, "revision": expected_revision,
                     "project_id": self.project_id, "operation_id": operation_id,
                     "created_by": actor, "before_content_hash": envelope["content_hash"],
                     "after_content_hash": repaired["content_hash"]}
            self._atomic_json(backup_dir / f"{artifact_id}-{expected_revision}.audit.json", audit, guard)
            return {"status": "repaired", "artifact_id": artifact_id,
                    "revision": expected_revision, "created_by": actor}

    def revise(
        self, artifact_id: str, expected_revision: int, payload: dict[str, Any],
        upstream_refs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self._validate_id(artifact_id)
        self._validate_revision(expected_revision)
        refs = self._reference_list(upstream_refs, self.project_id)
        with self._locked() as guard:
            self._validate_upstream_refs(refs)
            current = self._read(artifact_id, self._latest_revision(artifact_id))
            if current["revision"] != expected_revision:
                raise RemixArtifactConflict("Artifact changed; reload the latest revision before saving")
            validated = validate_payload(current["schema"], payload)
            self._validate_v2_lineage(current["schema"], validated, refs, target_artifact_id=artifact_id, previous_plan=current)
            return self._publish(
                artifact_id=artifact_id, schema=current["schema"], revision=expected_revision + 1,
                payload=validated, upstream_refs=refs, created_by=current["created_by"], guard=guard,
            )
