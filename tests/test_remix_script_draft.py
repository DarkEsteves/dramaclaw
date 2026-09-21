from __future__ import annotations

from types import SimpleNamespace
import json

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from novelvideo.freezone.remix.store import RemixArtifactStore


def blueprint_payload() -> dict:
    return {
        "narrative_role": "问题开场，演示产品，结尾行动号召",
        "events": [{"event_id": "hook", "narrative_role": "hook", "description": "早上没精神"}],
        "preserve_replace_rules": [], "observations": [], "inferences": [], "user_choices": [],
    }


def reviewed_blueprint_payload(evidence: dict, *, review_state: str = "reviewed") -> dict:
    evidence_ref = {"artifact_id": evidence["id"], "revision": evidence["revision"]}
    return {
        "narrative_role": "问题开场，演示产品，结尾行动号召",
        "evidence_ref": evidence_ref,
        "events": [{
            "event_id": "hook", "start_us": 0, "end_us": 1_000_000,
            "observed_frame": {"evidence_ref": evidence_ref, "pts": 0, "time_base": "1/1000", "time_us": 0},
            "spoken_range": None,
            "model": {"visual_observation": "早上没精神", "narrative_inference": "问题开场"},
            "user_override": {"narrative_role": "问题开场", "review_state": review_state},
        }],
        "preserve_replace_rules": [], "observations": [], "inferences": [], "user_choices": [],
    }


def create_evidence(store: RemixArtifactStore, marker: str = "video") -> dict:
    return store.create("reference_evidence.v1", {
        "media_hash": marker, "time_base": "1/1000", "duration_us": 1_000_000,
        "cuts": [], "representative_frames": [],
    }, [])


def test_generated_quotes_become_unicode_codepoint_selections() -> None:
    from novelvideo.freezone.remix.script_draft import draft_from_recipe_output

    draft = draft_from_recipe_output({
        "dialogue": "早上😴没精神？喝下咖啡，立刻清醒。",
        "segments": [],
        "selections": [{"label": "产品登场", "quote": "咖啡"}],
        "blueprint_mappings": [], "media_requirements": [],
    })

    assert draft["dialogue"] == "早上😴没精神？喝下咖啡，立刻清醒。"
    assert draft["selections"] == [{"id": "generated-1", "label": "产品登场", "start": 9, "end": 11}]


@pytest.mark.parametrize("quote", ["不存在", "咖啡"])
def test_generated_quote_must_identify_one_phrase(quote: str) -> None:
    from novelvideo.freezone.remix.script_draft import draft_from_recipe_output

    dialogue = "喝咖啡，再喝咖啡。" if quote == "咖啡" else "喝茶。"
    with pytest.raises(ValueError, match="关键句"):
        draft_from_recipe_output({"dialogue": dialogue, "selections": [{"label": "产品", "quote": quote}]})


def test_generated_script_requires_a_semantic_selection() -> None:
    from novelvideo.freezone.remix.script_draft import draft_from_recipe_output

    with pytest.raises(ValueError, match="关键句"):
        draft_from_recipe_output({"dialogue": "喝下新咖啡。", "selections": []})


@pytest.mark.asyncio
async def test_broll_plan_uses_v2_recipe_and_rejects_unknown_events(monkeypatch: pytest.MonkeyPatch) -> None:
    from novelvideo.freezone.remix import broll_plan

    calls = []

    async def generate(**kwargs):
        calls.append(kwargs)
        return json.dumps({
            "dialogue": "新品登场", "segments": [], "selections": [], "blueprint_mappings": [],
            "media_requirements": [{"event_id": "product", "required": True, "purpose": "展示产品", "suggested_query": "产品特写"}],
        }, ensure_ascii=False)

    monkeypatch.setattr(broll_plan, "generate_recipe_text", generate)
    script = {"dialogue": "新品登场", "segments": [], "selections": [], "events": [{"event_id": "product"}], "blueprint_mappings": [], "media_requirements": []}
    plan = await broll_plan.generate_broll_plan(script=script, username="alice")

    assert plan["media_requirements"][0]["event_id"] == "product"
    assert calls[0]["recipe_id"] == "reference-broll-plan"

    async def invalid(**_kwargs):
        return json.dumps({
            "dialogue": "新品登场", "segments": [], "selections": [], "blueprint_mappings": [],
            "media_requirements": [{"event_id": "missing", "required": True, "purpose": "展示产品", "suggested_query": "产品特写"}],
        }, ensure_ascii=False)

    monkeypatch.setattr(broll_plan, "generate_recipe_text", invalid)
    with pytest.raises(ValueError, match="event_id"):
        await broll_plan.generate_broll_plan(script=script, username="alice")


@pytest.mark.asyncio
async def test_broll_plan_cannot_publish_model_changes_to_saved_script(monkeypatch: pytest.MonkeyPatch) -> None:
    from novelvideo.freezone.remix import broll_plan

    saved = {
        "dialogue": "新品登场", "segments": [{"id": "saved-segment"}],
        "selections": [{"id": "product", "start": 0, "end": 4}],
        "events": [{"event_id": "product", "required": True, "caption": "新品登场"}],
        "blueprint_mappings": [{"source_event_id": "old", "target_event_id": "product"}],
        "media_requirements": [],
    }

    async def malicious(**_kwargs):
        return json.dumps({
            "dialogue": saved["dialogue"],
            "segments": [{"id": "model-overwrite"}],
            "selections": [{"id": "model-overwrite", "start": 0, "end": 1}],
            "blueprint_mappings": [],
            "media_requirements": [{"event_id": "product", "required": True, "purpose": "展示产品", "suggested_query": "产品特写"}],
        }, ensure_ascii=False)

    monkeypatch.setattr(broll_plan, "generate_recipe_text", malicious)
    plan = await broll_plan.generate_broll_plan(script=saved, username="alice")

    assert plan == {
        **saved,
        "media_requirements": [{"event_id": "product", "required": True, "purpose": "展示产品", "suggested_query": "产品特写"}],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "requirements, message",
    [
        ([], "every saved script event"),
        ([{"event_id": "hook", "required": True, "purpose": "开场", "suggested_query": "清晨"}], "every saved script event"),
        ([{"event_id": "hook", "required": True, "purpose": "开场", "suggested_query": "清晨"}, {"event_id": "hook", "required": False, "purpose": "开场", "suggested_query": "清晨"}], "unique"),
        ([{"event_id": "hook", "required": True, "purpose": "开场", "suggested_query": "清晨"}, {"event_id": "product", "purpose": "产品", "suggested_query": "咖啡"}], "output_schema"),
    ],
)
async def test_broll_plan_requires_one_explicit_decision_for_every_saved_event(
    monkeypatch: pytest.MonkeyPatch,
    requirements: list[dict[str, object]],
    message: str,
) -> None:
    from novelvideo.freezone.remix import broll_plan

    script = {
        "dialogue": "起床，喝咖啡。",
        "segments": [],
        "selections": [],
        "events": [{"event_id": "hook"}, {"event_id": "product"}],
        "blueprint_mappings": [],
        "media_requirements": [],
    }

    async def generate(**_kwargs):
        return json.dumps({key: value for key, value in script.items() if key != "events"} | {"media_requirements": requirements}, ensure_ascii=False)

    monkeypatch.setattr(broll_plan, "generate_recipe_text", generate)
    with pytest.raises(ValueError, match=message):
        await broll_plan.generate_broll_plan(script=script, username="alice")


@pytest.mark.asyncio
async def test_broll_plan_records_optional_events_as_required_false(monkeypatch: pytest.MonkeyPatch) -> None:
    from novelvideo.freezone.remix import broll_plan

    script = {
        "dialogue": "起床，喝咖啡。", "segments": [], "selections": [],
        "events": [{"event_id": "hook"}, {"event_id": "product"}],
        "blueprint_mappings": [], "media_requirements": [],
    }

    async def generate(**_kwargs):
        return json.dumps({key: value for key, value in script.items() if key != "events"} | {"media_requirements": [
            {"event_id": "hook", "required": False, "purpose": "保留人物", "suggested_query": "无需素材"},
            {"event_id": "product", "required": True, "purpose": "展示产品", "suggested_query": "咖啡特写"},
        ]}, ensure_ascii=False)

    monkeypatch.setattr(broll_plan, "generate_recipe_text", generate)
    plan = await broll_plan.generate_broll_plan(script=script, username="alice")
    assert plan["media_requirements"][0]["required"] is False


@pytest.mark.asyncio
async def test_generator_uses_builtin_recipe_and_validates_model_json(monkeypatch: pytest.MonkeyPatch) -> None:
    from novelvideo.freezone.remix import script_draft

    calls = []

    async def generate(**kwargs):
        calls.append(kwargs)
        return json.dumps({"dialogue": "新咖啡来了。", "segments": [], "selections": [{"label": "产品登场", "quote": "新咖啡"}], "blueprint_mappings": [], "media_requirements": []}, ensure_ascii=False)

    monkeypatch.setattr(script_draft, "generate_recipe_text", generate)
    draft = await script_draft.generate_script_draft(blueprint=blueprint_payload(), topic="新咖啡", username="alice")

    assert draft["selections"] == [{"id": "generated-1", "label": "产品登场", "start": 0, "end": 3}]
    assert calls[0]["recipe_id"] == "reference-remix-script"
    assert calls[0]["user_goal"] == "新咖啡"
    assert "问题开场" in calls[0]["upstream_text"]

    async def invalid(**kwargs):
        return '{"dialogue":"新咖啡来了。","segments":[],"selections":[{"label":"产品登场","quote":"不存在"}],"blueprint_mappings":[],"media_requirements":[]}'

    monkeypatch.setattr(script_draft, "generate_recipe_text", invalid)
    with pytest.raises(ValueError, match="关键句"):
        await script_draft.generate_script_draft(blueprint=blueprint_payload(), topic="新咖啡", username="alice")


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, tmp_path):
    from novelvideo.api.auth import get_api_user
    from novelvideo.api.routes import remix as routes

    async def resolve(*, user, project_id, required_role):
        if project_id != "demo":
            raise HTTPException(403, "Access denied")
        return SimpleNamespace(output_dir=str(tmp_path), project_id="demo", is_home_node=True)

    monkeypatch.setattr(routes, "resolve_project_context", resolve)
    monkeypatch.setattr(routes, "require_project_home_node", lambda ctx, **kwargs: None)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    app.dependency_overrides[get_api_user] = lambda: {"username": "alice"}
    store = RemixArtifactStore(tmp_path, "demo")
    evidence = create_evidence(store)
    evidence_ref = {"artifact_id": evidence["id"], "revision": evidence["revision"]}
    blueprint = store.create("reference_blueprint.v1", reviewed_blueprint_payload(evidence), [evidence_ref])
    return TestClient(app), blueprint


def test_script_draft_endpoint_reads_exact_blueprint_and_does_not_publish(api, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from novelvideo.api.routes import remix as routes

    client, blueprint = api
    calls = []

    async def generate(*, blueprint, topic, username):
        calls.append((blueprint, topic, username))
        return {"dialogue": "用新咖啡开始一天。", "selections": [{"id": "generated-1", "label": "产品登场", "start": 2, "end": 5}]}

    monkeypatch.setattr(routes, "generate_script_draft", generate)
    response = client.post("/api/v1/projects/demo/freezone/remix/script-drafts", json={
        "blueprint_ref": {"artifact_id": blueprint["id"], "revision": 1}, "new_topic": "新咖啡",
    })

    assert response.status_code == 200
    assert response.json()["data"]["dialogue"] == "用新咖啡开始一天。"
    assert calls == [(blueprint["payload"], "新咖啡", "alice")]
    assert len(list((tmp_path / "freezone" / "_remix_artifacts").iterdir())) == 2


def test_broll_plan_endpoint_publishes_validated_plan_from_exact_script(api, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from novelvideo.api.routes import remix as routes

    client, blueprint = api
    store = RemixArtifactStore(tmp_path, "demo")
    script = store.create("remix_script.v1", {
        "dialogue": "新品登场", "segments": [], "selections": [], "events": [{"event_id": "product"}],
        "blueprint_mappings": [], "media_requirements": [],
    }, [{"artifact_id": blueprint["id"], "revision": blueprint["revision"]}])

    async def generate(*, script, username):
        assert username == "alice"
        return {
            "dialogue": script["dialogue"], "segments": [], "selections": [], "blueprint_mappings": [],
            "media_requirements": [{"event_id": "product", "required": True, "purpose": "展示产品", "suggested_query": "产品特写"}],
        }

    monkeypatch.setattr(routes, "generate_broll_plan", generate)
    response = client.post("/api/v1/projects/demo/freezone/remix/broll-plans", json={
        "script_ref": {"artifact_id": script["id"], "revision": script["revision"]},
    })

    assert response.status_code == 200
    artifact = response.json()["data"]
    assert artifact["schema"] == "remix_script.v1"
    assert artifact["upstream_refs"] == [{"artifact_id": script["id"], "revision": 1, "project_id": "demo"}]
    assert artifact["payload"]["media_requirements"][0]["required"] is True


def test_script_draft_endpoint_rejects_wrong_schema_and_other_project(api, tmp_path) -> None:
    client, _ = api
    wrong = RemixArtifactStore(tmp_path, "demo").create("remix_script.v1", {
        "dialogue": "hello", "segments": [], "selections": [], "blueprint_mappings": [], "media_requirements": [],
    }, [])
    wrong_response = client.post("/api/v1/projects/demo/freezone/remix/script-drafts", json={
        "blueprint_ref": {"artifact_id": wrong["id"], "revision": 1}, "new_topic": "咖啡",
    })
    assert wrong_response.status_code == 422
    response = client.post("/api/v1/projects/other/freezone/remix/script-drafts", json={
        "blueprint_ref": {"artifact_id": "missing", "revision": 1}, "new_topic": "咖啡",
    })
    assert response.status_code == 403


def test_script_draft_endpoint_rejects_blank_topic(api) -> None:
    client, blueprint = api
    response = client.post("/api/v1/projects/demo/freezone/remix/script-drafts", json={
        "blueprint_ref": {"artifact_id": blueprint["id"], "revision": 1}, "new_topic": "   ",
    })
    assert response.status_code == 422


@pytest.mark.parametrize("kind", ["legacy", "unreviewed", "broken_reference"])
def test_script_draft_endpoint_rejects_blueprint_that_is_not_script_ready(api, tmp_path, kind: str) -> None:
    client, _ = api
    store = RemixArtifactStore(tmp_path, "demo")
    if kind == "legacy":
        blueprint = store.create("reference_blueprint.v1", blueprint_payload(), [])
    else:
        evidence = create_evidence(store, kind)
        declared = {"artifact_id": evidence["id"], "revision": evidence["revision"]}
        payload = reviewed_blueprint_payload(evidence, review_state="needs_review" if kind == "unreviewed" else "reviewed")
        upstream = [declared]
        if kind == "broken_reference":
            other = create_evidence(store, "other")
            upstream = [{"artifact_id": other["id"], "revision": other["revision"]}]
        blueprint = store.create("reference_blueprint.v1", payload, upstream)

    response = client.post("/api/v1/projects/demo/freezone/remix/script-drafts", json={
        "blueprint_ref": {"artifact_id": blueprint["id"], "revision": blueprint["revision"]},
        "new_topic": "咖啡",
    })

    assert response.status_code == 422
    assert "blueprint" in response.json()["detail"].lower()
