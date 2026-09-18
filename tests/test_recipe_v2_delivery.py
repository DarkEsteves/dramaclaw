from types import SimpleNamespace

import pytest

from novelvideo.api.schemas import FreezoneRecipeCompileRequest
from novelvideo.freezone.recipe_runtime import RecipeCompileResult


@pytest.mark.asyncio
@pytest.mark.parametrize("schema_version", ["dramaclaw.recipe.v1", "dramaclaw.recipe.v2"])
async def test_text_delivery_keeps_v1_and_uses_v2_artifact_adapter(monkeypatch, tmp_path, schema_version):
    from novelvideo.api.routes import freezone

    calls = []
    async def resolve(*_args, **_kwargs):
        return SimpleNamespace(project_id="demo", output_dir=str(tmp_path)), "alice", "demo", tmp_path, tmp_path
    monkeypatch.setattr(freezone, "_resolve_freezone_project", resolve)
    monkeypatch.setattr(freezone, "_canvas_state_project_dir", lambda _ctx, path: path)
    monkeypatch.setattr(freezone, "read_agent_product_operation", lambda **_: {"product_kind": "recipe_result", "status": "running", "task_id": "task", "metadata": {}})
    monkeypatch.setattr(freezone, "bind_agent_product_model_execution", lambda **_: None)
    async def reconcile(**_kwargs):
        return None
    monkeypatch.setattr(freezone, "_reconcile_recipe_delivery", reconcile)
    monkeypatch.setattr(freezone, "get_recipe_for_runtime", lambda **_: {"schema_version": schema_version})
    monkeypatch.setattr(freezone, "finish_agent_product_operation", lambda **kwargs: calls.append(("v1", kwargs)) or {"status": "delivered"})
    monkeypatch.setattr(freezone, "deliver_structured_recipe_result", lambda **kwargs: calls.append(("v2", kwargs)) or {"status": "delivered"})
    body = FreezoneRecipeCompileRequest(project_id="demo", product_operation_id="op", recipe_id="recipe", node_kind="text")
    await freezone._record_recipe_compile_product_evidence(body=body, compiled=RecipeCompileResult('{"x":1}', "model", ("recipe",), model_call_id="call", executed_at=1.0), user={"username": "alice"}, deliver_text=True)

    assert calls[0][0] == ("v2" if schema_version.endswith("v2") else "v1")
