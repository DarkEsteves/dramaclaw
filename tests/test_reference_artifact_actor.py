import json
import hashlib
import sqlite3

import pytest

from novelvideo.freezone.paths import canvas_path
from novelvideo.freezone.remix.operations import RemixOperationStore
from novelvideo.freezone.remix.store import RemixArtifactStore
from novelvideo.freezone.remix.artifact_actor_repair import repair_blank_artifact_actor
from novelvideo.freezone.remix.production_store import production_store
from test_reference_remix_workflow_dispatch import context


def _historical_plan(tmp_path):
    output, state = tmp_path / "output", tmp_path / "state"
    store = RemixArtifactStore(output, "project")
    plan = store.create("production_plan.v2", {
        "schema_version": 2, "goal": "sample",
        "output": {"width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1},
        "beats": [{"id": "opening", "purpose": "setup", "duration_frames": 90, "turns": []}],
        "resources": [], "placements": [], "provenance": {},
    }, [])
    ops = RemixOperationStore(output, "project")
    op = ops.start(idempotency_key="native-plan", input_refs=[],
        producer_version="remix-process.v1", normalized_parameters={"action": "generate_production_plan_draft"},
        node_revision=1, canvas_id="canvas", node_id="plan")
    fence = ops.claim_submission(op["operation_id"], owner="submit")
    ops.bind_task(op["operation_id"], task_id="task-a", owner="submit", fencing_token=fence)
    ops.publish_result_unverified(op["operation_id"], output_refs=[{"artifact_id": plan["id"], "revision": 1}])
    path = canvas_path(state, "canvas")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"project_id": "project", "canvas_id": "canvas",
        "owner_principal_id": "actor", "nodes": [{"id": "plan"}]}))
    state.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(state / "data.db") as conn:
        conn.execute("CREATE TABLE task_states (task_id TEXT, task_type TEXT, project_id TEXT, "
                     "requester_user_id TEXT, status TEXT, result_json TEXT)")
        conn.execute("INSERT INTO task_states VALUES (?, ?, ?, ?, ?, ?)", (
            "task-a", "remix_generate_production_plan_draft", "project", "actor", "completed",
            json.dumps({"artifact_ref": {"artifact_id": plan["id"], "revision": 1}})))
    return output, state, plan, op


def test_actor_repair_requires_verified_native_task_and_defaults_to_dry_run(tmp_path):
    output, state, plan, op = _historical_plan(tmp_path)
    kwargs = dict(output_dir=output, state_dir=state, project_id="project", canvas_id="canvas",
                  artifact_id=plan["id"], expected_revision=1, operation_id=op["operation_id"])
    dry = repair_blank_artifact_actor(**kwargs)
    assert dry["status"] == "would_repair"
    assert RemixArtifactStore(output, "project").read(plan["id"], 1)["created_by"] == ""
    result = repair_blank_artifact_actor(**kwargs, apply=True)
    assert result["status"] == "repaired"
    saved = RemixArtifactStore(output, "project").read(plan["id"], 1)
    assert saved["created_by"] == "actor"
    assert saved["content_hash"] == plan["content_hash"]
    assert saved["revision"] == 1
    assert repair_blank_artifact_actor(**kwargs, apply=True)["status"] == "already_repaired"


def test_actor_repair_rejects_unproven_identity_and_revision(tmp_path):
    output, state, plan, op = _historical_plan(tmp_path)
    kwargs = dict(output_dir=output, state_dir=state, project_id="project", canvas_id="canvas",
                  artifact_id=plan["id"], expected_revision=1, operation_id=op["operation_id"])
    with pytest.raises(ValueError, match="artifact"):
        repair_blank_artifact_actor(**{**kwargs, "expected_revision": 2}, apply=True)
    with sqlite3.connect(state / "data.db") as conn:
        conn.execute("UPDATE task_states SET requester_user_id = 'other'")
    with pytest.raises(ValueError, match="owner"):
        repair_blank_artifact_actor(**kwargs, apply=True)
    assert RemixArtifactStore(output, "project").read(plan["id"], 1)["created_by"] == ""


def test_actor_repair_rejects_backup_directory_symlink(tmp_path):
    output, state, plan, op = _historical_plan(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    store = RemixArtifactStore(output, "project")
    (store.root / "_actor_repair_backups").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="backup directory"):
        repair_blank_artifact_actor(
            output_dir=output, state_dir=state, project_id="project", canvas_id="canvas",
            artifact_id=plan["id"], expected_revision=1,
            operation_id=op["operation_id"], apply=True,
        )
    assert list(outside.iterdir()) == []


def test_production_store_uses_requester_id_and_refuses_blank_actor(tmp_path):
    from dataclasses import replace
    ctx = replace(context(tmp_path, actor="user-id"), requester_username="display-name")
    assert ctx.requester_username != ctx.requester_user_id
    assert production_store(ctx).created_by == "user-id"
    with pytest.raises(ValueError, match="requester"):
        production_store(replace(ctx, requester_user_id=""))


def test_legacy_reference_chain_repairs_only_with_original_task_and_media_hash(tmp_path):
    output, state = tmp_path / 'output', tmp_path / 'state'
    store = RemixArtifactStore(output, 'project')
    store.project_dir.mkdir(parents=True, exist_ok=True)
    source = store.project_dir / 'source.mp4'
    source.write_bytes(b'original project video')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    evidence = store.create('reference_evidence.v1', {
        'media_hash': digest, 'time_base': '1/1000', 'duration_us': 1_000_000,
        'cuts': [], 'representative_frames': [],
    }, [])
    evidence_ref = {'artifact_id': evidence['id'], 'revision': 1}
    blueprint = store.create('reference_blueprint.v1', {
        'narrative_role': 'opening', 'events': [], 'preserve_replace_rules': [],
        'observations': [], 'inferences': [], 'user_choices': [],
        'evidence_ref': evidence_ref,
    }, [evidence_ref])
    blueprint_ref = {'artifact_id': blueprint['id'], 'revision': 1}
    analysis = store.create('reference_analysis.v1', {
        'media_path': 'source.mp4', 'media_hash': 'sha256:' + digest,
        'evidence_ref': evidence_ref, 'blueprint_ref': blueprint_ref,
        'transcript_status': 'no_audio', 'story': {}, 'coverage': {},
    }, [evidence_ref, blueprint_ref])
    analysis_ref = {'artifact_id': analysis['id'], 'revision': 1}
    ops = RemixOperationStore(output, 'project')
    op = ops.start(idempotency_key='joint-reference', input_refs=[],
        producer_version='remix-process.v1',
        normalized_parameters={'action': 'joint_reference_analysis'},
        node_revision=1, canvas_id='canvas', node_id='story')
    fence = ops.claim_submission(op['operation_id'], owner='submit')
    ops.bind_task(op['operation_id'], task_id='task-a', owner='submit', fencing_token=fence)
    ops.publish_result_unverified(op['operation_id'], output_refs=[analysis_ref])
    path = canvas_path(state, 'canvas')
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'project_id': 'project', 'canvas_id': 'canvas',
        'owner_principal_id': 'actor', 'nodes': [{'id': 'story'}]}))
    with sqlite3.connect(state / 'data.db') as conn:
        conn.execute('CREATE TABLE task_states (task_id TEXT, task_type TEXT, project_id TEXT, '
                     'requester_user_id TEXT, status TEXT, result_json TEXT)')
        conn.execute('INSERT INTO task_states VALUES (?, ?, ?, ?, ?, ?)', (
            'task-a', 'remix_joint_reference_analysis', 'project', 'actor', 'completed',
            json.dumps({'artifact_ref': analysis_ref, 'evidence_ref': evidence_ref,
                        'blueprint_ref': blueprint_ref})))
    kwargs = dict(output_dir=output, state_dir=state, project_id='project',
                  canvas_id='canvas', expected_revision=1, operation_id=op['operation_id'])
    for artifact in (evidence, blueprint, analysis):
        assert repair_blank_artifact_actor(**kwargs, artifact_id=artifact['id'])['status'] == 'would_repair'
    assert repair_blank_artifact_actor(**kwargs, artifact_id=evidence['id'], apply=True)['status'] == 'repaired'
    assert store.read(evidence['id'], 1)['created_by'] == 'actor'
    source.write_bytes(b'changed')
    with pytest.raises(ValueError, match='media hash'):
        repair_blank_artifact_actor(**kwargs, artifact_id=evidence['id'], apply=True)
