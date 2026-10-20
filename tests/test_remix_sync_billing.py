"""Billing boundary for the three legacy synchronous Remix model routes."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from novelvideo.freezone.remix.store import RemixArtifactStore
from novelvideo.shared.billing_errors import InsufficientCreditsError
from test_remix_script_draft import create_evidence, reviewed_blueprint_payload


TASK_TYPES = {
    "script": "remix_generate_script_draft",
    "production": "remix_generate_production_plan_draft",
    "broll": "remix_plan_broll",
}


class Meter:
    def __init__(self, events: list[str], *, reject: bool = False) -> None:
        self.events = events
        self.reject = reject
        self.reserve_calls: list[dict] = []
        self.contexts: list[dict] = []
        self.confirm_calls: list[tuple[str, dict]] = []
        self.review_calls: list[tuple[str, dict]] = []
        self.refund_calls: list[tuple[str, dict]] = []

    async def reserve_feature_start_credits(self, **kwargs):
        self.events.append("reserve")
        self.reserve_calls.append(kwargs)
        if self.reject:
            raise InsufficientCreditsError(user_id=kwargs["user_id"], cost=9, balance=0)
        return {"id": "reserved-plan", "cost": 9}

    def set_llm_usage_context(self, user_id, **kwargs):
        self.events.append("set-context")
        self.contexts.append({"user_id": user_id, **kwargs})

    def clear_llm_usage_context(self):
        self.events.append("clear-context")

    async def settle_feature_credit_reservation(self, reservation_id, *, action, metadata=None):
        self.events.append(action)
        assert action == "confirm"
        self.confirm_calls.append((reservation_id, metadata))

    async def mark_feature_credit_settlement_for_review(self, reservation_id, *, metadata=None):
        self.events.append("review")
        self.review_calls.append((reservation_id, metadata))

    async def settle_cancelled_feature_credit_reservation(self, reservation_id, *, metadata=None):
        self.events.append("refund")
        self.refund_calls.append((reservation_id, metadata))


@pytest.fixture
def billed_client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    from novelvideo.api.auth import get_api_user
    from novelvideo.api import egress_binding
    from novelvideo.api.routes import remix

    events: list[str] = []
    meter = Meter(events)

    async def resolve(*, user, project_id, required_role):
        assert project_id == "demo"
        assert required_role == "editor"
        return SimpleNamespace(
            output_dir=tmp_path, project_id="trusted-project",
            requester_user_id="trusted-user", is_home_node=True,
        )

    @asynccontextmanager
    async def scope(**kwargs):
        events.append("scope-enter")
        assert kwargs == {
            "requester_user_id": "trusted-user",
            "project_id": "trusted-project",
            "task_type": expected_task[0],
        }
        try:
            yield SimpleNamespace(is_organization=True)
        finally:
            events.append("scope-exit")

    expected_task = [""]
    monkeypatch.setattr(remix, "resolve_project_context", resolve)
    monkeypatch.setattr(remix, "require_project_home_node", lambda ctx, **kwargs: None)
    monkeypatch.setattr(remix, "get_usage_meter", lambda: meter, raising=False)
    monkeypatch.setattr(egress_binding, "request_egress_scope", scope)
    app = FastAPI()
    app.include_router(remix.router, prefix="/api/v1")
    app.dependency_overrides[get_api_user] = lambda: {"id": "untrusted-alias", "username": "alice"}

    store = RemixArtifactStore(tmp_path, "trusted-project")
    evidence = create_evidence(store)
    evidence_ref = {"artifact_id": evidence["id"], "revision": evidence["revision"]}
    blueprint = store.create("reference_blueprint.v1", reviewed_blueprint_payload(evidence), [evidence_ref])
    script = store.create("remix_script.v1", {
        "dialogue": "新品登场", "segments": [], "selections": [],
        "events": [{"event_id": "product"}], "blueprint_mappings": [], "media_requirements": [],
    }, [])
    return TestClient(app), remix, meter, events, expected_task, blueprint, script


def _request(client, kind, blueprint, script):
    base = "/api/v1/projects/demo/freezone/remix"
    if kind == "script":
        return client.post(f"{base}/script-drafts", json={
            "blueprint_ref": {"artifact_id": blueprint["id"], "revision": 1},
            "new_topic": "新咖啡",
        })
    if kind == "production":
        return client.post(f"{base}/production-plan-drafts", json={"topic": "新咖啡"})
    return client.post(f"{base}/broll-plans", json={
        "script_ref": {"artifact_id": script["id"], "revision": 1},
    })


def _install_model(monkeypatch, remix, kind, events, *, fail=False):
    async def generate(**kwargs):
        events.append("model")
        if fail:
            raise ValueError("model output invalid")
        if kind == "broll":
            return {
                **kwargs["script"],
                "media_requirements": [{
                    "event_id": "product", "required": True,
                    "purpose": "展示产品", "suggested_query": "产品特写",
                }],
            }
        return {"dialogue": "新咖啡"} if kind == "script" else {"title": "新咖啡"}

    name = {
        "script": "generate_script_draft",
        "production": "generate_production_plan_draft",
        "broll": "generate_broll_plan",
    }[kind]
    monkeypatch.setattr(remix, name, generate)


def test_legacy_production_draft_requires_canvas_task_without_billing(billed_client, monkeypatch):
    client, remix, meter, events, expected_task, blueprint, script = billed_client
    _install_model(monkeypatch, remix, "production", events)
    response = _request(client, "production", blueprint, script)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "production_plan_canvas_action_required"
    assert "model" not in events
    assert meter.reserve_calls == meter.confirm_calls == meter.review_calls == []


@pytest.mark.parametrize("kind", ["script", "broll"])
def test_sync_remix_route_reserves_and_confirms_once(billed_client, monkeypatch, kind):
    client, remix, meter, events, expected_task, blueprint, script = billed_client
    expected_task[0] = TASK_TYPES[kind]
    _install_model(monkeypatch, remix, kind, events)

    response = _request(client, kind, blueprint, script)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    if kind == "broll":
        assert data["schema"] == "remix_script.v1"
        assert data["payload"]["media_requirements"][0]["required"] is True
    else:
        assert data == ({"dialogue": "新咖啡"} if kind == "script" else {"title": "新咖啡"})
    assert len(meter.reserve_calls) == 1
    reservation = meter.reserve_calls[0]
    assert {key: reservation[key] for key in (
        "user_id", "feature_key", "product_surface", "project_id",
        "resource_kind", "task_type", "require_price_rule", "require_positive_cost",
    )} == {
        "user_id": "trusted-user", "feature_key": "freezone.remix_plan",
        "product_surface": "freezone", "project_id": "trusted-project",
        "resource_kind": "script", "task_type": TASK_TYPES[kind],
        "require_price_rule": True, "require_positive_cost": True,
    }
    assert reservation["quantity"] == 1
    assert reservation["task_id"].startswith("remix-sync-")
    billing = meter.contexts[0]["billing_metadata"]
    assert meter.contexts[0]["user_id"] == "trusted-user"
    assert billing["model_call_credit_policy"] == "feature_included"
    assert billing["feature_key"] == "freezone.remix_plan"
    assert billing["feature_credit_reservation_id"] == "reserved-plan"
    assert meter.confirm_calls[0][0] == "reserved-plan"
    assert events.count("clear-context") == 1
    assert meter.review_calls == meter.refund_calls == []
    assert events.index("scope-enter") < events.index("reserve") < events.index("model")
    assert events.index("model") < events.index("confirm") < events.index("scope-exit")


@pytest.mark.parametrize("kind", ["script", "broll"])
def test_sync_remix_route_balance_rejection_prevents_model_call(billed_client, monkeypatch, kind):
    client, remix, meter, events, expected_task, blueprint, script = billed_client
    expected_task[0] = TASK_TYPES[kind]
    meter.reject = True
    _install_model(monkeypatch, remix, kind, events)

    with pytest.raises(InsufficientCreditsError):
        _request(client, kind, blueprint, script)

    assert "model" not in events
    assert meter.confirm_calls == meter.review_calls == meter.refund_calls == []


@pytest.mark.parametrize("kind", ["script", "broll"])
def test_sync_remix_route_post_start_failure_keeps_reservation_for_review(
    billed_client, monkeypatch, kind,
):
    client, remix, meter, events, expected_task, blueprint, script = billed_client
    expected_task[0] = TASK_TYPES[kind]
    _install_model(monkeypatch, remix, kind, events, fail=True)

    response = _request(client, kind, blueprint, script)

    assert response.status_code == 422, response.text
    assert "model" in events
    assert meter.confirm_calls == meter.refund_calls == []
    assert meter.review_calls[0][0] == "reserved-plan"
    assert events.index("model") < events.index("review") < events.index("scope-exit")


def test_broll_publication_failure_keeps_reservation_for_review(billed_client, monkeypatch):
    client, remix, meter, events, expected_task, blueprint, script = billed_client
    expected_task[0] = TASK_TYPES["broll"]
    _install_model(monkeypatch, remix, "broll", events)

    def fail_publish(*args, **kwargs):
        raise OSError("publication outcome unknown")

    monkeypatch.setattr(RemixArtifactStore, "create", fail_publish)
    response = _request(client, "broll", blueprint, script)

    assert response.status_code == 503
    assert events.index("model") < events.index("review")
    assert meter.review_calls[0][0] == "reserved-plan"
    assert meter.confirm_calls == meter.refund_calls == []


def test_broll_invalid_saved_events_reject_before_reservation(
    billed_client, monkeypatch, tmp_path,
):
    client, remix, meter, events, expected_task, blueprint, _ = billed_client
    expected_task[0] = TASK_TYPES["broll"]
    _install_model(monkeypatch, remix, "broll", events)
    invalid_script = RemixArtifactStore(tmp_path, "trusted-project").create(
        "remix_script.v1",
        {
            "dialogue": "新品登场", "segments": [], "selections": [],
            "events": [], "blueprint_mappings": [], "media_requirements": [],
        },
        [],
    )

    response = _request(client, "broll", blueprint, invalid_script)

    assert response.status_code == 422
    assert meter.reserve_calls == meter.review_calls == meter.refund_calls == []
    assert "model" not in events


@pytest.mark.parametrize("kind", ["script", "broll"])
def test_missing_recipe_rejects_before_reservation(billed_client, monkeypatch, kind):
    client, remix, meter, events, expected_task, blueprint, script = billed_client
    expected_task[0] = TASK_TYPES[kind]
    _install_model(monkeypatch, remix, kind, events)

    def missing_recipe(**kwargs):
        raise ValueError("recipe is unavailable")

    monkeypatch.setattr(remix, "get_recipe_for_runtime", missing_recipe, raising=False)
    response = _request(client, kind, blueprint, script)

    assert response.status_code == 422
    assert meter.reserve_calls == meter.review_calls == meter.refund_calls == []
    assert "model" not in events
