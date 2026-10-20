from __future__ import annotations

import json
from pathlib import Path

from novelvideo.freezone import agent_config_store
from novelvideo.freezone.agent_catalog_schema import (
    validate_agent_recipe_config,
    validate_agent_skill_config,
)
from novelvideo.freezone.agent_workflows import catalog, registry


CATALOG_ROOT = (
    Path(__file__).parents[1]
    / "src/novelvideo/freezone/agent_catalog/builtins"
)


def test_builtin_skill_is_discoverable_and_resolves_current_recipe_package(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(agent_config_store, "project_agent_catalog_dir", lambda _kind: tmp_path / "project")
    monkeypatch.setattr(agent_config_store, "user_agent_config_dir", lambda _user, _kind: tmp_path / "user")
    monkeypatch.setattr(catalog, "list_user_agent_config_items", agent_config_store.list_user_agent_config_items)
    agent_config_store._CATALOG_CACHE.clear()

    result = registry.search_catalog(
        username="reference-skill-test", kind="skills", query="参考视频复刻"
    )
    assert [item["id"] for item in result] == ["reference-video-remix"]
    with catalog.workflow_catalog_scope("reference-skill-test"):
        package = catalog.get_workflow_skill(
            {"skill_id": "reference-video-remix", "compact": True}
        )
    assert package["ok"] is True
    assert package["skill"]["version"] == "2.1.0"
    assert {item["id"] for item in package["available_recipes"]} >= {
        "reference-blueprint",
        "reference-production-plan",
    }
    assert "reference-remix-script" not in {
        item["id"] for item in package["available_recipes"]
    }


def test_user_hidden_or_disabled_builtin_is_not_available_to_agent(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(agent_config_store, "project_agent_catalog_dir", lambda _kind: tmp_path / "project")
    monkeypatch.setattr(agent_config_store, "user_agent_config_dir", lambda _user, _kind: tmp_path / "user" / _kind)
    monkeypatch.setattr(catalog, "list_user_agent_config_items", agent_config_store.list_user_agent_config_items)
    agent_config_store._CATALOG_CACHE.clear()
    username = "reference-skill-test"
    skill = next(
        item for item in agent_config_store.list_user_agent_config_items(username, "skills")
        if item["id"] == "reference-video-remix"
    )
    disabled = {key: value for key, value in skill.items() if not key.startswith("_catalog_")}
    disabled["enabled"] = False
    agent_config_store.save_user_agent_config_item(
        username=username, kind="skills", payload=disabled
    )
    with catalog.workflow_catalog_scope(username):
        assert catalog.get_workflow_skill({"skill_id": "reference-video-remix"})["ok"] is False
    assert not registry.search_catalog(
        username=username, kind="skills", query="参考视频复刻"
    )
    agent_config_store.delete_user_agent_config_item(
        username=username, kind="skills", item_id="reference-video-remix"
    )
    with catalog.workflow_catalog_scope(username):
        assert catalog.get_workflow_skill({"skill_id": "reference-video-remix"})["ok"] is False
    assert not registry.search_catalog(
        username=username, kind="skills", query="参考视频复刻"
    )


def test_user_customization_keeps_priority_over_builtin_upgrade(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(agent_config_store, "project_agent_catalog_dir", lambda _kind: tmp_path / "project")
    monkeypatch.setattr(agent_config_store, "user_agent_config_dir", lambda _user, _kind: tmp_path / "user" / _kind)
    username = "reference-skill-test"
    skill = next(
        item for item in agent_config_store.list_user_agent_config_items(username, "skills")
        if item["id"] == "reference-video-remix"
    )
    custom = {key: value for key, value in skill.items() if not key.startswith("_catalog_")}
    custom["name"] = "我的参考视频技能"
    agent_config_store.save_user_agent_config_item(
        username=username, kind="skills", payload=custom
    )
    loaded = next(
        item for item in agent_config_store.list_user_agent_config_items(username, "skills")
        if item["id"] == "reference-video-remix"
    )
    assert loaded["name"] == "我的参考视频技能"
    assert loaded["_catalog_source"] == "user"


def test_builtin_production_recipe_keeps_plan_draft_separate_from_evidence() -> None:
    recipe = validate_agent_recipe_config(json.loads(
        (CATALOG_ROOT / "recipes/reference-production-plan.json").read_text()
    ))
    prompt = recipe["system_prompt"]
    assert "本地测时" in prompt
    assert "语音对齐结果" in prompt
    assert "不编造" in prompt
    assert "无台词" in prompt
    assert "锁定台词" in prompt
    skill = validate_agent_skill_config(json.loads(
        (CATALOG_ROOT / "skills/reference-video-remix.json").read_text()
    ))
    assert "1080x1920" not in json.dumps(skill)
    assert "60 秒" not in json.dumps(skill, ensure_ascii=False)


def test_native_production_contract_does_not_offer_removed_direct_plan_tool() -> None:
    from novelvideo.freezone.agent_workflows.reference_production import NATIVE_PRODUCTION_CONTRACT

    assert NATIVE_PRODUCTION_CONTRACT["prepare_tool"] == "freezone_prepare_production_nodes"
    assert "direct_author_tool" not in NATIVE_PRODUCTION_CONTRACT


def test_native_production_contract_distinguishes_node_creation_and_draft_action() -> None:
    from novelvideo.freezone.agent_workflows.reference_production import NATIVE_PRODUCTION_CONTRACT

    contract = NATIVE_PRODUCTION_CONTRACT
    assert contract["intent_example"]["external_inputs"][0]["media_kind"] == "reference_analysis"
    assert "planner" not in contract["intent_example"]
    assert contract["draft_action"] == {
        "tool": "freezone_run_node_action",
        "action": "generate_production_plan_draft",
        "node_type": "scriptNode",
        "remix_role": "production_plan",
        "node_id_source": "native_plan_stage.slots[].plan_node_id",
    }
    assert contract["accepted_next_action"] == "await_completion_event"


def test_hermes_reference_remix_guide_is_available_under_its_registered_skill_name() -> None:
    root = Path(__file__).parents[1]
    native = root / ".hermes/skills/workflows"
    guide = native / "references/reference-video-remix.md"
    canonical = root / "src/novelvideo/agent_skills/dramaclaw-workflows/references/reference-video-remix.md"
    assert guide.read_text() == canonical.read_text()
    instructions = (native / "SKILL.md").read_text()
    assert 'skill_view(name="workflows", file_path="references/reference-video-remix.md")' in instructions
    assert "freezone_get_production_state" in instructions
    assert "freezone_prepare_production_edit" in instructions


def test_reference_guide_scopes_native_draft_to_the_confirmed_commission() -> None:
    root = Path(__file__).parents[1]
    guide = (root / "src/novelvideo/agent_skills/dramaclaw-workflows/references/reference-video-remix.md").read_text()
    assert "commission_ref" in guide
    assert "freezone_prepare_workflow_draft" in guide
    assert "await_completion_event" in guide
    assert "4–15 second" in guide


def test_native_guide_uses_bound_stage_then_submits_action_and_waits_for_host() -> None:
    root = Path(__file__).parents[1]
    guide = (root / "src/novelvideo/agent_skills/dramaclaw-workflows/references/reference-video-remix.md").read_text()
    package = catalog.get_workflow_skill({"skill_id": "reference-video-remix", "compact": True})
    instruction = package["agent_instruction"]
    for marker in ("native_plan_stage", "generate_production_plan_draft", "await_completion_event",
                   "freezone_run_node_action", "read the saved plan"):
        assert marker in guide or marker in instruction
    assert "It runs\nonly plan generation initially" not in guide
    assert "read it and attach its draft and exact ref" not in guide
    assert "freezone_get_production_progress" in guide
    assert "source clip" in guide
    assert "source frames" in guide


def test_reference_guide_keeps_material_sources_and_async_boundaries_explicit() -> None:
    root = Path(__file__).parents[1]
    guide = (root / "src/novelvideo/agent_skills/dramaclaw-workflows/references/reference-video-remix.md").read_text()
    for marker in ("freezone_list_reference_media", "freezone_revise_material_strategy",
                   "import_reference_keyframes", "shot_number", "existing_node_id",
                   "视频证据", "图片参考", "重新读取", "不自动生成", "不自动重试",
                   "保留", "替换", "新增", "不要循环轮询"):
        assert marker in guide


def test_reference_guide_separates_analysis_events_from_production_shots() -> None:
    root = Path(__file__).parents[1]
    copies = [root / name / "references/reference-video-remix.md" for name in (
        "src/novelvideo/agent_skills/dramaclaw-workflows",
        "agent-kit/skills/dramaclaw-workflows", ".hermes/skills/workflows",
    )]
    assert len({path.read_bytes() for path in copies}) == 1
    guide = copies[0].read_text()
    for marker in (
        "原片分析事件/抽帧窗口只表示原片的观察证据",
        "不等于复刻视频必须生成同样数量的镜头、beat、素材或单镜时长",
        "事件数量可以作为导演编排的参考",
        "镜头数量和编排由 Agent 根据新目标",
        "向用户复述分析时使用“原片分析事件/证据窗口”",
        "1.5–2.5 秒只能作为原片节奏观察",
    ):
        assert marker in guide


def test_hermes_catalog_wrapper_routes_existing_plan_edits_to_native_operations() -> None:
    from novelvideo.chat.hermes_workspace import _render_workflow_skill
    skill = json.loads((CATALOG_ROOT / "skills/reference-video-remix.json").read_text())
    content, _ = _render_workflow_skill(skill)
    assert 'skill_view(name="workflows", file_path="references/reference-video-remix.md")' in content
    assert "已有作品方案的局部编辑" in content
    assert "freezone_get_production_state" in content
    assert "freezone_prepare_production_edit" in content
    assert "不是新建工作流" in content


def test_freezone_native_plan_edit_instructions_do_not_require_a_portable_skill_namespace() -> None:
    root = Path(__file__).parents[1]
    instructions = (root / ".hermes/skills/freezone/SKILL.md").read_text()
    assert "已有作品方案的局部编辑" in instructions
    assert "freezone_get_production_state" in instructions
    assert "freezone_prepare_production_edit" in instructions
    assert 'name="dramaclaw-workflows"' not in instructions


def test_reference_guide_routes_saved_reads_browser_blockers_and_conflicts() -> None:
    root = Path(__file__).parents[1]
    copies = [root / name / 'references/reference-video-remix.md' for name in (
        'src/novelvideo/agent_skills/dramaclaw-workflows',
        'agent-kit/skills/dramaclaw-workflows', '.hermes/skills/workflows')]
    assert len({path.read_bytes() for path in copies}) == 1
    guide = copies[0].read_text()
    # Retrieval cases: offline analysis, dynamic selection, user edit conflict,
    # pending human decision and paid task. All require distinct next actions.
    for marker in ('read_consistency="saved"', 'requires_browser',
                   'canvas_revision_conflict', 'last_successful_context_receipt',
                   'execution_blocker', 'no_connection'):
        assert marker in guide


def test_reference_guide_requires_actual_saved_placement_review():
    root = Path(__file__).parents[1]
    for directory in ("src/novelvideo/agent_skills/dramaclaw-workflows", "agent-kit/skills/dramaclaw-workflows", ".hermes/skills/workflows"):
        guide = (root / directory / "references/reference-video-remix.md").read_text()
        assert "before `upsert_placement`" in guide
        assert "untargeted fields and anchors" in guide
        assert "numeric `duration_frames`/`timing_mode`" in guide
        assert "Any truncated" in guide


def test_material_author_guide_example_is_native_and_mirrored():
    from novelvideo.freezone.remix.material_work import MaterialRequirement, MaterialStep, _exact_ref
    root = Path(__file__).parents[1]
    copies = [root / name / "references/reference-video-remix.md" for name in (
        "src/novelvideo/agent_skills/dramaclaw-workflows", "agent-kit/skills/dramaclaw-workflows", ".hermes/skills/workflows")]
    assert len({p.read_bytes() for p in copies}) == 1
    guide = copies[0].read_text()
    assert "<!-- native-material-author-example -->" in guide
    example = guide.split("<!-- native-material-author-example -->", 1)[1].split("```json", 1)[1].split("```", 1)[0]
    args = json.loads(example)
    _exact_ref(args["plan_ref"])
    requirements = [MaterialRequirement.model_validate(r) for r in args["requirements"]]
    steps = [MaterialStep.model_validate(s) for s in args["steps"]]
    assert requirements[0].final_step_id == steps[1].id
    assert steps[1].references[0].step_id == steps[0].id
    assert "replace with actual saved" in guide
    assert "output_kind" in guide and "server owned" in guide



def test_material_guide_requires_real_wave_admission_and_execution_policy():
    root = Path(__file__).resolve().parents[1]
    copies = [root / name / "references/reference-video-remix.md" for name in (
        "src/novelvideo/agent_skills/dramaclaw-workflows",
        "agent-kit/skills/dramaclaw-workflows", ".hermes/skills/workflows",
    )]
    assert len({p.read_bytes() for p in copies}) == 1
    guide = copies[0].read_text()
    material_section = guide.split("Plan how media is made before generating it.", 1)[1].split("For speech,", 1)[0]
    assert "freezone_begin_agent_product_generation" in material_section
    assert "workflow_result" in material_section
    assert "copy its exact operation_id" in material_section
    assert "run_after_create=true" in material_section
    assert "each ready wave" in material_section
    assert "Never invent" in material_section


def test_guides_use_current_projection_and_candidate_speech_without_auto_approval():
    root = Path(__file__).resolve().parents[1]
    copies = [root / name / "references/reference-video-remix.md" for name in (
        "src/novelvideo/agent_skills/dramaclaw-workflows",
        "agent-kit/skills/dramaclaw-workflows", ".hermes/skills/workflows",
    )]
    assert len({p.read_bytes() for p in copies}) == 1
    guide = copies[0].read_text()
    for marker in (
        'freezone_get_production_progress', 'freezone_advance_production',
        'freezone_select_material_work', 'expected_projection_revision',
        'execution_status', 'ready_system_actions', 'result_registration_pending',
        'freezone_get_material_speech_review', 'analyze_material_speech',
        'adopt_source_observation', 'correction_ref', 'authenticated user saves',
        'raw ASR', 'Do not claim to have listened',
    ):
        assert marker in guide
    assert 'only the host' in guide
    assert 'already saved direction and generation parameters' in guide
    assert 'Do not automatically upgrade' in guide
    zh = (root / 'docs/zh/guides/reference-video-remix.md').read_text()
    for marker in ('当前选定', '已生成', '检查中', '待审看', '需返工', '已采用', '候选', '原始 ASR'):
        assert marker in zh
