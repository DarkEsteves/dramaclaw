import pytest
import json
from pathlib import Path

from novelvideo.freezone.agent_catalog_schema import validate_agent_recipe_config, validate_agent_skill_config
from novelvideo.freezone.recipe_runtime import RecipeRuntimeError, validate_structured_recipe_output
from novelvideo.freezone.recipe_runtime import publish_structured_recipe_output


def recipe() -> dict:
    return {"schema_version": "dramaclaw.recipe.v2", "id": "reference-blueprint", "enabled": True, "version": "1", "name": "Blueprint", "output_kind": "text", "action_keys": ["reference-blueprint"], "system_prompt": "Return JSON", "planning_prompt": "Plan", "result_summary": "Blueprint", "input_contract": {"required": ["evidence_ref"]}, "output_schema": {"type": "object", "required": ["narrative_role"], "properties": {"narrative_role": {"type": "string"}}}}


def test_v2_recipe_round_trips_contracts() -> None:
    saved = validate_agent_recipe_config(recipe())
    assert saved["input_contract"]["required"] == ["evidence_ref"]
    assert saved["output_schema"]["required"] == ["narrative_role"]


def test_v2_recipe_save_and_import_preserve_contracts(tmp_path, monkeypatch) -> None:
    from novelvideo.freezone import agent_config_store
    recipe_dir = tmp_path / "recipes"
    monkeypatch.setattr(agent_config_store, "user_agent_config_dir", lambda *_: recipe_dir)
    payload = {**recipe(), "id": "v2-contract-test", "action_keys": ["v2-contract-test"]}
    saved = agent_config_store.save_user_agent_config_item(username="alice", kind="recipes", payload=payload)
    loaded = agent_config_store._read_agent_config_items(recipe_dir)
    restored = next(item for item in loaded if item["id"] == "v2-contract-test")
    assert saved["output_schema"] == restored["output_schema"]


def test_v2_structured_output_rejects_invalid_json_or_schema_mismatch() -> None:
    saved = validate_agent_recipe_config(recipe())
    assert validate_structured_recipe_output(saved, '{"narrative_role":"hook"}') == {"narrative_role": "hook"}
    with pytest.raises(RecipeRuntimeError):
        validate_structured_recipe_output(saved, "not json")
    with pytest.raises(RecipeRuntimeError):
        validate_structured_recipe_output(saved, "{}")


def test_validated_blueprint_output_is_published_as_an_artifact(tmp_path) -> None:
    blueprint = {**recipe(), "output_schema": {"type": "object", "required": ["narrative_role", "events", "preserve_replace_rules", "observations", "inferences", "user_choices"], "properties": {"narrative_role": {"type": "string"}, "events": {"type": "array"}, "preserve_replace_rules": {"type": "array"}, "observations": {"type": "array"}, "inferences": {"type": "array"}, "user_choices": {"type": "array"}}}}
    saved = publish_structured_recipe_output(recipe=blueprint, content='{"narrative_role":"hook","events":[],"preserve_replace_rules":[],"observations":[],"inferences":[],"user_choices":[]}', project_dir=tmp_path, project_id="demo", created_by="alice", upstream_refs=[])
    assert saved["schema"] == "reference_blueprint.v1"


@pytest.mark.parametrize("recipe_id", ["reference-remix-script", "reference-broll-plan"])
def test_script_and_broll_v2_outputs_publish_as_remix_script(tmp_path, recipe_id) -> None:
    structured = {**recipe(), "id": recipe_id, "action_keys": [recipe_id], "output_schema": {"type": "object", "required": ["dialogue", "segments", "selections", "blueprint_mappings", "media_requirements"]}}
    saved = publish_structured_recipe_output(recipe=structured, content='{"dialogue":"x","segments":[],"selections":[],"blueprint_mappings":[],"media_requirements":[]}', project_dir=tmp_path, project_id="demo", created_by="alice", upstream_refs=[])
    assert saved["schema"] == "remix_script.v1"


def test_agent_product_delivery_adapter_publishes_before_finish(tmp_path, monkeypatch) -> None:
    from novelvideo.freezone import agent_product_operations as operations
    calls = []
    monkeypatch.setattr(operations, "read_agent_product_operation", lambda **_: {"project_id": "demo", "product_kind": "recipe_result"})
    monkeypatch.setattr(operations, "finish_agent_product_operation", lambda **kwargs: calls.append(kwargs) or kwargs)
    result = operations.deliver_structured_recipe_result(operation_project_dir=tmp_path, artifact_project_dir=tmp_path, operation_id="op", expected_task_id="task", recipe={**recipe(), "output_schema": {"type": "object", "required": ["narrative_role", "events", "preserve_replace_rules", "observations", "inferences", "user_choices"]}}, content='{"narrative_role":"hook","events":[],"preserve_replace_rules":[],"observations":[],"inferences":[],"user_choices":[]}', created_by="alice", upstream_refs=[])
    assert result["result_ref"]["kind"] == "remix_artifact"
    assert calls


def test_builtin_reference_remix_skill_declares_review_and_rerun_contract() -> None:
    root = Path(__file__).parents[1] / "src/novelvideo/freezone/agent_catalog/builtins"
    skill = json.loads((root / "skills/reference-video-remix.json").read_text())
    skill = validate_agent_skill_config(skill)
    assert skill["allowed_recipe_ids"] == [
        "reference-blueprint",
        "reference-remix-script",
        "reference-broll-plan",
    ]
    rules = "\n".join(skill["planning"]["conduct_rules"])
    assert "注册动作" in rules
    assert "人工确认" in rules
    assert "只重跑" in rules
    assert "shell" not in json.dumps(skill).lower()


def test_reference_creative_recipes_are_structured_v2_and_legacy_v1_is_unchanged() -> None:
    root = Path(__file__).parents[1] / "src/novelvideo/freezone/agent_catalog/builtins/recipes"
    for recipe_id in ("reference-blueprint", "reference-remix-script", "reference-broll-plan"):
        payload = validate_agent_recipe_config(json.loads((root / f"{recipe_id}.json").read_text()))
        assert payload["schema_version"] == "dramaclaw.recipe.v2"
        assert payload["output_schema"]["type"] == "object"
        assert payload["input_contract"]["required"]

    legacy = validate_agent_recipe_config(json.loads((root / "general-text.json").read_text()))
    assert legacy["schema_version"] == "dramaclaw.recipe.v1"
    assert "output_schema" not in legacy


def test_reference_script_recipe_prefers_reviewed_user_corrections() -> None:
    root = Path(__file__).parents[1] / "src/novelvideo/freezone/agent_catalog/builtins/recipes"
    payload = validate_agent_recipe_config(json.loads((root / "reference-remix-script.json").read_text()))
    assert "二者冲突时以 user_override" in payload["system_prompt"]
    assert "角色：台词" in payload["system_prompt"]
    assert "每句独立一行" in payload["system_prompt"]
