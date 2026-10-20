from copy import deepcopy
from pathlib import Path
import pytest
from novelvideo.freezone.remix.production_plan_v2 import ProductionPlanV2


def passing_findings():
    from novelvideo.freezone.remix.production_review import REVIEW_CATEGORIES
    return [{'category': category, 'entity_kind': 'plan', 'entity_id': 'plan', 'verdict': 'pass',
             'reason': 'Reviewed saved content', 'evidence': ['intro']} for category in REVIEW_CATEGORIES]


def test_current_typed_requirements_override_all_passing_categories(tmp_path):
    from novelvideo.freezone.remix.production_review import evaluate_production_review
    result = evaluate_production_review(plan(), {'requirements': {'primary_shots': {'min': 5},
        'duration_us': {'min': 4_000_000}, 'output_requirements': {'width': 1080}}}, passing_findings(), project_dir=tmp_path)
    assert result['status'] == 'changes_required'
    assert {'primary_shot_count_mismatch', 'duration_mismatch', 'output_requirement_mismatch'} <= {
        row['code'] for row in result['checks'] if row['status'] == 'failed'}


def test_all_passing_creative_findings_cannot_approve_zero_length_title(tmp_path):
    from novelvideo.freezone.remix.production_review import evaluate_production_review

    payload = plan()
    payload.setdefault('placements', []).append({
        'id': 'bad-title', 'kind': 'graphic', 'role': 'effect', 'track_id': 'graphics',
        'start': {'kind': 'beat', 'ref_id': 'intro', 'edge': 'end'},
        'end': {'kind': 'beat', 'ref_id': 'intro', 'edge': 'end'},
        'graphic': {'template': 'title_bar', 'text': '结束'},
    })
    result = evaluate_production_review(payload, {}, passing_findings(), project_dir=tmp_path)
    assert result['status'] == 'changes_required'
    assert any(row['code'] == 'invalid_timing' and row['entity_id'] == 'bad-title'
               and row['status'] == 'failed' for row in result['checks'])


@pytest.mark.parametrize('verdict', [None, 'unsupported', 'change'])
def test_semantic_requirements_need_supported_entity_findings(verdict):
    from novelvideo.freezone.remix.production_review import evaluate_production_review
    findings = passing_findings()
    if verdict:
        findings.append({'category': 'direction', 'entity_kind': 'beat', 'entity_id': 'intro',
            'requirement_id': 'keep-question', 'verdict': verdict,
            'reason': 'Question inspected', 'evidence': ['intro:提问']})
    result = evaluate_production_review(plan(), {'requirements': {
        'semantic': [{'id': 'keep-question', 'text': 'Keep the questioning atmosphere'}]}}, findings)
    assert result['status'] == 'changes_required'


@pytest.mark.parametrize('evidence,ref,expected', [
    (['invented-shot:question'], None, 'changes_required'),
    (['intro:提问'], None, 'changes_required'),
    (['Question purpose retained'], {'entity_kind': 'beat', 'entity_id': 'intro', 'field': 'purpose'}, 'passed'),
])
def test_semantic_evidence_must_identify_actual_saved_content(evidence, ref, expected):
    from novelvideo.freezone.remix.production_review import evaluate_production_review
    findings = passing_findings() + [{'category': 'direction', 'entity_kind': 'beat', 'entity_id': 'intro',
        'requirement_id': 'keep-question', 'verdict': 'pass', 'reason': 'Question purpose retained',
        'evidence': evidence, **({'evidence_refs': [ref]} if ref else {})}]
    result = evaluate_production_review(plan(), {'requirements': {
        'semantic': [{'id': 'keep-question', 'text': 'Keep the questioning atmosphere'}]}}, findings)
    assert result['status'] == expected


def test_saved_review_resolves_original_dialogue_from_current_analysis(tmp_path):
    from test_production_requirements import commission_fixture
    from novelvideo.freezone.remix.production_review import review_production_plan
    store, c, _ = commission_fixture(tmp_path, {'dialogue': {'mode': 'preserve_source'}})
    payload = plan()
    saved = store.create('production_plan.v2', payload, [])
    reviewed = review_production_plan(store, {'artifact_id': saved['id'], 'revision': 1},
        c['commission_ref'], passing_findings())
    assert reviewed['payload']['status'] == 'changes_required'
    assert any(row['code'] == 'locked_dialogue_changed' for row in reviewed['payload']['checks'])


@pytest.mark.parametrize('delivered', [False, True])
def test_saved_review_uses_revised_node_goal_without_withdrawn_dialogue_constraint(tmp_path, delivered):
    from test_production_requirements import commission_fixture
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.freezone.remix.production_review import evaluate_saved_production_review
    from novelvideo.freezone.remix.reference_commission import (
        AuthenticatedDecisionEvidence, advance_reference_commission,
    )
    store, commission, _ = commission_fixture(tmp_path, {'dialogue': {'mode': 'preserve_source'}})
    commission = advance_reference_commission(store, commission['commission_ref'],
        {'type': 'direction_confirmed', 'direction': {
            'target_goal': '保留原片台词', 'requirements': {'dialogue': {'mode': 'preserve_source'}}}},
        commission['commission_ref']['revision'],
        host_evidence=AuthenticatedDecisionEvidence('p', 'c', 'u', 'new-goal'))
    operation_store = RemixOperationStore(tmp_path, 'p')
    operation = operation_store.start(idempotency_key='edited-target', input_refs=[],
        producer_version='remix-process.v1',
        normalized_parameters={'action': 'generate_production_plan_draft',
                               'payload': {'topic': '改写台词，保留叙事节奏', 'requirements': {}}},
        node_revision=2, canvas_id='c', node_id='plan')
    fence = operation_store.claim_submission(operation['operation_id'], owner='worker')
    operation_store.bind_task(operation['operation_id'], task_id='task-1',
                              owner='worker', fencing_token=fence)
    payload = {**plan(), 'provenance': {'operation_id': operation['operation_id'], 'node_target': {
        'canvas_id': 'c', 'node_id': 'plan', 'node_revision': 2,
        'goal': '改写台词，保留叙事节奏'}, 'requirements': {}}}
    saved = store.create('production_plan.v2', payload, [])
    plan_ref = {'artifact_id': saved['id'], 'revision': 1}
    operation_store.publish_result_unverified(operation['operation_id'], output_refs=[plan_ref])
    if delivered:
        operation_store.confirm(operation['operation_id'], current_input_refs=[],
                                current_node_revision=2, canvas_id='c', node_id='plan')
    commission_artifact = store.read(commission['commission_ref']['artifact_id'],
                                     commission['commission_ref']['revision'])
    result = evaluate_saved_production_review(store, saved['payload'],
                                               commission_artifact, passing_findings(),
                                               plan_ref=plan_ref)
    assert not any(row['code'] == 'locked_dialogue_changed' for row in result['checks'])
    forged = store.revise(saved['id'], 1, {**saved['payload'], 'provenance': {
        **saved['payload']['provenance'], 'requirements': {'primary_shots': {'min': 1}}}}, [])
    with pytest.raises(ValueError, match='provenance changed'):
        evaluate_saved_production_review(store, forged['payload'], commission_artifact,
            passing_findings(), plan_ref={'artifact_id': saved['id'], 'revision': 2})


def plan():
    return {"schema_version": 2, "goal": "解释", "output": {"width": 1920, "height": 1080, "fps_num": 24, "fps_den": 1},
            "beats": [{"id": "intro", "purpose": "提问", "duration_frames": 48,
                       "turns": [{"id": "line", "text": "保留台词"}]}]}


def test_empty_author_plan_is_valid_but_not_creatively_reviewed():
    draft = {**plan(), "beats": []}
    assert ProductionPlanV2.model_validate(draft).beats == []
    from novelvideo.freezone.remix.production_review import evaluate_production_review
    result = evaluate_production_review(draft, {"preserve": {"turns": {"line": "保留台词"}}}, [])
    assert result["status"] == "changes_required"


def test_schema_valid_review_requires_concrete_category_findings():
    from novelvideo.freezone.remix import production_review as reviews
    assert hasattr(reviews, "evaluate_production_review")
    result = reviews.evaluate_production_review(plan(), {"goal": "解释"}, [])
    assert result["status"] == "changes_required"
    assert any(x["code"] == "creative_review_incomplete" for x in result["checks"])


def test_direction_preserved_dialogue_mismatch_blocks_agent_pass():
    from novelvideo.freezone.remix.production_review import evaluate_production_review, REVIEW_CATEGORIES
    findings = [{"category": category, "entity_kind": "plan", "entity_id": "plan", "verdict": "pass",
                 "reason": "已检查该部分并符合方向", "evidence": ["intro"]} for category in REVIEW_CATEGORIES]
    result = evaluate_production_review(plan(), {"preserve": {"turns": {"line": "用户确认的另一句"}}}, findings)
    assert result["status"] == "changes_required"
    assert any(x["code"] == "preserved_dialogue_changed" for x in result["checks"])


def test_confirmed_preservation_statements_need_semantic_review_without_crashing():
    from novelvideo.freezone.remix.production_review import evaluate_production_review

    reviewed = evaluate_production_review(
        plan(), {"preserve": ["Keep the question and answer structure"]}, passing_findings(),
    )

    assert reviewed["status"] == "changes_required"
    assert any(row["code"] == "semantic_requirement_review_missing"
               for row in reviewed["checks"])


def test_empty_optional_output_constraints_do_not_crash_review():
    from novelvideo.freezone.remix.production_review import evaluate_production_review
    reviewed = evaluate_production_review(plan(), {"output_requirements": None}, passing_findings())
    assert reviewed["status"] == "passed"


def test_paraphrased_goal_and_semantic_evidence_can_use_saved_entity_references():
    from novelvideo.freezone.remix.production_review import evaluate_production_review

    findings = passing_findings()
    findings[-1]["evidence_refs"] = [
        {"entity_kind": "plan", "entity_id": "plan", "field": "goal"},
        {"entity_kind": "beat", "entity_id": "intro", "field": "purpose"},
    ]
    findings.append({
        "category": "direction", "entity_kind": "beat", "entity_id": "intro",
        "requirement_id": "opening", "verdict": "pass",
        "reason": "The saved opening asks a question", "evidence": ["The opening is a question"],
        "evidence_refs": [{"entity_kind": "beat", "entity_id": "intro", "field": "purpose"}],
    })
    reviewed = evaluate_production_review(plan(), {
        "goal": "Explain the product with a question",
        "requirements": {"semantic": [{"id": "opening", "text": "Keep the question"}]},
    }, findings)
    assert reviewed["status"] == "passed"
    assert any(row["value"] == "提问" for row in reviewed["resolved_evidence"])


def test_missing_real_reference_does_not_pass_semantic_review():
    from novelvideo.freezone.remix.production_review import evaluate_production_review

    findings = passing_findings() + [{
        "category": "direction", "entity_kind": "beat", "entity_id": "intro",
        "requirement_id": "opening", "verdict": "pass", "reason": "The opening asks a question",
        "evidence": ["intro:提问"],
    }]
    reviewed = evaluate_production_review(plan(), {
        "requirements": {"semantic": [{"id": "opening", "text": "Keep the question"}]},
    }, findings)
    assert reviewed["status"] == "changes_required"
    assert any(row["code"] == "semantic_requirement_unsupported" for row in reviewed["checks"])


def test_decision_fingerprint_preserves_material_refinement_but_detects_dialogue():
    from novelvideo.freezone.remix.production_review import production_decision_fingerprint
    source = plan()
    refined = deepcopy(source)
    refined["resources"] = [{"id": "new", "media_hash": "a" * 64}]
    assert production_decision_fingerprint(source) == production_decision_fingerprint(refined)
    refined["beats"][0]["turns"][0]["text"] = "改变"
    assert production_decision_fingerprint(source) != production_decision_fingerprint(refined)


def test_decision_fingerprint_detects_planned_sound_owner_and_video_audio_flag():
    from test_production_audio_sources import planned_video_dialogue
    from novelvideo.freezone.remix.production_review import production_decision_fingerprint

    source = planned_video_dialogue()
    changed = deepcopy(source)
    changed['beats'][0]['turns'][0]['speech_placement_id'] = None
    assert production_decision_fingerprint(source) != production_decision_fingerprint(changed)
    changed = deepcopy(source)
    changed['placements'][0]['source_audio'] = None
    assert production_decision_fingerprint(source) != production_decision_fingerprint(changed)


def test_planned_video_sound_requires_grounded_source_finding():
    from test_production_audio_sources import planned_video_dialogue
    from novelvideo.freezone.remix.production_review import evaluate_production_review

    source = planned_video_dialogue()
    missing = evaluate_production_review(source, {}, passing_findings())
    assert missing['status'] == 'changes_required'
    assert any(row['code'] == 'planned_audio_source_review_missing'
               and row['entity_id'] == 'demo-video'
               and 'ask' in row['message'] and 'answer' in row['message']
               for row in missing['checks'])
    grounded = passing_findings() + [{
        'category': 'audio_visual_roles', 'entity_kind': 'placement', 'entity_id': 'demo-video',
        'verdict': 'pass', 'reason': 'One audible video is planned for both turns',
        'evidence': ['demo-video owns ask and answer'],
        'evidence_refs': [
            {'entity_kind': 'placement', 'entity_id': 'demo-video', 'field': 'description'},
            {'entity_kind': 'turn', 'entity_id': 'ask', 'field': 'text'},
            {'entity_kind': 'turn', 'entity_id': 'answer', 'field': 'text'},
        ],
    }]
    reviewed = evaluate_production_review(source, {}, grounded)
    assert reviewed['status'] == 'passed'


def test_sound_only_video_without_description_can_cite_owning_beat_purpose():
    from test_production_audio_sources import planned_video_dialogue
    from novelvideo.freezone.remix.production_review import evaluate_production_review

    source = planned_video_dialogue()
    source['beats'][0]['turns'] = []
    source['placements'][0].pop('description')
    source['beats'].append({'id': 'other', 'purpose': '无关段落', 'duration_frames': 30, 'turns': []})
    finding = {'category': 'audio_visual_roles', 'entity_kind': 'placement',
               'entity_id': 'demo-video', 'verdict': 'pass',
               'reason': 'The scene plans audible action', 'evidence': ['demo beat purpose'],
               'evidence_refs': [{'entity_kind': 'beat', 'entity_id': 'demo', 'field': 'purpose'}]}
    reviewed = evaluate_production_review(source, {}, passing_findings() + [finding])
    assert reviewed['status'] == 'passed'
    unrelated = deepcopy(finding)
    unrelated['evidence_refs'][0]['entity_id'] = 'other'
    reviewed = evaluate_production_review(source, {}, passing_findings() + [unrelated])
    assert reviewed['status'] == 'changes_required'
    assert any(row['code'] == 'planned_audio_source_review_missing' for row in reviewed['checks'])


def test_saved_review_rejects_stale_plan_and_unknown_entities(tmp_path: Path):
    from novelvideo.freezone.remix.production_review import review_production_plan, REVIEW_CATEGORIES
    from novelvideo.freezone.remix.reference_commission import create_reference_commission, advance_reference_commission, AuthenticatedDecisionEvidence
    from novelvideo.freezone.remix.store import RemixArtifactStore, RemixArtifactConflict
    store = RemixArtifactStore(tmp_path, "p")
    c = create_reference_commission(store, canvas_id="canvas", requester_user_id="user", analysis_node_id="analysis",
                                    goal="解释", analysis_evidence={"task_id": "t", "result_fingerprint": "h"})
    c = advance_reference_commission(store, c["commission_ref"], {"type": "direction_confirmed", "direction": {"target_goal": "解释"}}, 1,
                                     host_evidence=AuthenticatedDecisionEvidence("p", "canvas", "user", "message"))
    saved = store.create("production_plan.v2", plan(), [])
    ref = {"artifact_id": saved["id"], "revision": 1}
    findings = [{"category": category, "entity_kind": "beat", "entity_id": "intro", "verdict": "pass",
                 "reason": "提问结构符合用户解释目标", "evidence": ["intro:保留台词"]} for category in REVIEW_CATEGORIES]
    review = review_production_plan(store, ref, c["commission_ref"], findings)
    assert review["payload"]["status"] == "passed"
    assert review["payload"]["plan_content_hash"] == saved["content_hash"]
    with pytest.raises(ValueError, match="unknown entity"):
        review_production_plan(store, ref, c["commission_ref"], [{**findings[0], "entity_id": "missing"}])
    edited = deepcopy(saved["payload"])
    edited["beats"][0]["turns"][0]["text"] = "新目标"
    store.revise(saved["id"], 1, edited, [])
    with pytest.raises(RemixArtifactConflict, match="changed"):
        review_production_plan(store, ref, c["commission_ref"], findings)


def test_review_rejects_preserve_replace_conflict_and_creative_graphic_edits():
    from novelvideo.freezone.remix.production_review import evaluate_production_review, production_decision_fingerprint
    direction = {"preserve_replace": [{"entity_kind": "turn", "entity_id": "line", "action": "preserve", "value": "原句"}]}
    result = evaluate_production_review(plan(), direction, [])
    assert any(x["code"] == "preserved_dialogue_changed" for x in result["checks"])
    source = plan()
    source["placements"] = [{"id": "caption", "kind": "subtitle", "role": "subtitle", "text": "标题"}]
    changed = deepcopy(source)
    changed["placements"][0]["text"] = "另一主题"
    assert production_decision_fingerprint(source) != production_decision_fingerprint(changed)


def test_voiced_visual_plan_with_only_absolute_frame_anchors_cannot_pass_review():
    from novelvideo.freezone.remix.production_review import evaluate_production_review, REVIEW_CATEGORIES
    draft = plan()
    draft["placements"] = [{"id": "shot", "kind": "visual", "role": "primary", "track_id": "v1",
                            "description": "主持人提问，文字跟随台词", "start": {"kind": "program_frame", "frame": 0},
                            "end": {"kind": "program_frame", "frame": 48}}]
    findings = [{"category": category, "entity_kind": "plan", "entity_id": "plan", "verdict": "pass",
                 "reason": "已检查实际方案", "evidence": ["intro"]} for category in REVIEW_CATEGORIES]
    reviewed = evaluate_production_review(draft, {"goal": "解释"}, findings)
    assert reviewed["status"] == "changes_required"
    assert any(check["code"] == "dialogue_visual_anchor_missing" for check in reviewed["checks"])
