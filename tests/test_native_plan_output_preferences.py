import pytest

from novelvideo.api.routes.remix import _action_inputs
from novelvideo.freezone.remix.store import RemixArtifactStore


def test_native_plan_action_keeps_structured_source_clip_preferences(tmp_path):
    store = RemixArtifactStore(tmp_path, "project")
    payload = {"topic": "保留访谈节奏，替换产品", "output_preferences": {
        "aspect_ratio": "9:16", "video_aspect_ratio": "9:16", "source_clip_duration_seconds": 5,
    }}

    _refs, normalized = _action_inputs(store, "generate_production_plan_draft", payload)

    assert normalized["payload"]["topic"] == "保留访谈节奏，替换产品"
    assert normalized["payload"]["output_preferences"] == payload["output_preferences"]


@pytest.mark.parametrize("preferences", [
    {"source_clip_duration_seconds": -1},
    {"source_clip_duration_seconds": "five"},
    {"unexpected": "ignored"},
])
def test_native_plan_rejects_invalid_output_preferences(tmp_path, preferences):
    store = RemixArtifactStore(tmp_path, "project")
    with pytest.raises(ValueError):
        _action_inputs(store, "generate_production_plan_draft", {
            "topic": "coffee", "output_preferences": preferences,
        })
