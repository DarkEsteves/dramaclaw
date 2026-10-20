import json
from pathlib import Path
import pytest

F = json.loads(
    (Path(__file__).parent / "fixtures/canvas-context-read.json").read_text()
)


def read(requests, canvas=None, **kwargs):
    from novelvideo.freezone.canvas_context_reader import read_saved_context

    return read_saved_context(
        canvas or F["canvas"],
        project_id="p",
        canvas_id="c",
        requests=requests,
        **kwargs,
    )


def test_parity():
    result = read([{"type": "node_detail", "node_id": key} for key in F["parity"]])
    assert result["source"] == "saved_canvas"
    assert result["canvas_revision"] == "r7"
    assert "receipt" not in result
    for r in result["responses"]:
        for k, v in F["parity"][r["node_id"]].items():
            assert r["data"]["nodes"][0][k] == v
        assert "secret" not in json.dumps(r)
    assert result["coverage"]["excluded"] == F["coverage"]["excluded"]


def test_missing_unsupported_browser_empty():
    result = read(
        [
            {"type": "node_detail", "node_id": "deleted"},
            {"type": "canvas_ontology"},
            {"type": "selection_detail"},
            {"type": "canvas_summary"},
        ],
        {**F["canvas"], "nodes": [], "edges": []},
    )
    assert result["responses"][0]["data"] is None
    assert result["responses"][1]["status"] == "unsupported"
    assert result["responses"][2]["status"] == "requires_browser"
    assert result["responses"][3]["data"]["node_count"] == 0


def test_limits_and_revision():
    with pytest.raises(ValueError, match="revision_conflict"):
        read([], expected_revision="old")
    with pytest.raises(ValueError, match="20"):
        read([{"type": "canvas_summary"}] * 21)
    with pytest.raises(ValueError, match="depth"):
        read([{"type": "neighbor_graph", "node_id": "video", "depth": 3}])


def test_pagination_and_neighbors():
    result = read([{"type": "neighbor_graph", "node_id": "video", "depth": 2}])
    assert {n["node_id"] for n in result["responses"][0]["data"]["nodes"]} == {
        "video",
        "analysis",
        "plan",
        "compose",
    }
    c = {
        **F["canvas"],
        "nodes": [{**F["canvas"]["nodes"][0], "id": str(i)} for i in range(105)],
        "edges": [],
    }
    r = read([{"type": "canvas_summary"}], c)["responses"][0]
    assert len(r["data"]["nodes"]) == 100
    assert r["coverage"]["complete"] is False
    assert (
        len(
            read([{"type": "canvas_summary", "cursor": r["cursor"]}], c)["responses"][
                0
            ]["data"]["nodes"]
        )
        == 5
    )
    with pytest.raises(ValueError, match="cursor"):
        read(
            [{"type": "canvas_summary", "cursor": r["cursor"]}],
            {**c, "revision": "new"},
        )


def test_size_limit():
    c = {
        **F["canvas"],
        "nodes": [
            {
                **F["canvas"]["nodes"][1],
                "data": {
                    **F["canvas"]["nodes"][1]["data"],
                    "referenceBlueprintRef": {
                        "artifact_id": "x" * 140000,
                        "revision": 2,
                    },
                },
            }
        ],
    }
    r = read([{"type": "node_detail", "node_id": "analysis"}], c)
    assert len(json.dumps(r).encode()) <= 128 * 1024
    assert r["responses"][0]["status"] == "response_too_large"
    assert r["responses"][0]["coverage"]["complete"] is False


def test_durable_binding_rejects_expired_lease_and_replaces_expired_file(tmp_path):
    import sqlite3
    import time
    from types import SimpleNamespace
    from novelvideo.freezone.canvas_context_reader import (
        saved_context_execution,
        saved_turn_binding,
    )

    db = tmp_path / "continuations.db"
    with sqlite3.connect(db) as connection:
        connection.execute(
            "CREATE TABLE reference_remix_continuations (run_id TEXT, requester_user_id TEXT, project_id TEXT, canvas_id TEXT, agent_id TEXT, lease_owner TEXT, lease_until REAL, payload_json TEXT)"
        )
        connection.execute(
            "INSERT INTO reference_remix_continuations VALUES (?,?,?,?,?,?,?,?)",
            (
                "run",
                "u",
                "p",
                "c",
                "main",
                "owner",
                time.time() + 60,
                json.dumps({"resume_turn_id": "turn"}),
            ),
        )
    identity = SimpleNamespace(
        surface="freezone",
        requester_user_id="u",
        project_id="p",
        canvas_id="c",
        agent_id="main",
    )
    with saved_context_execution(
        execution_context=identity,
        turn_id="turn",
        state_dir=tmp_path,
        coordinator_lease={"db_path": str(db), "run_id": "run", "owner_token": "owner"},
    ):
        assert saved_turn_binding(tmp_path, "turn")["agent_id"] == "main"
        binding_path = next((tmp_path / "freezone/context_read_turns").glob("*.json"))
        assert binding_path.stat().st_mode & 0o777 == 0o600
        with sqlite3.connect(db) as connection:
            connection.execute("UPDATE reference_remix_continuations SET lease_until=0")
        assert saved_turn_binding(tmp_path, "turn") is None
    with saved_context_execution(
        execution_context=identity, turn_id="turn", state_dir=tmp_path
    ):
        binding_path = next((tmp_path / "freezone/context_read_turns").glob("*.json"))
        expired = json.loads(binding_path.read_text())
        expired["expires_at"] = 0
    binding_path.write_text(json.dumps(expired))
    with saved_context_execution(
        execution_context=identity, turn_id="turn", state_dir=tmp_path
    ):
        assert saved_turn_binding(tmp_path, "turn")


def test_saved_read_rejects_unbounded_request_identifiers():
    with pytest.raises(ValueError, match="request type"):
        read([{"type": "x" * 140000}])
    with pytest.raises(ValueError, match="node_id"):
        read([{"type": "node_detail", "node_id": "x" * 140000}])


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("depth", [0, 1, 2])
def test_neighbor_depth_is_independent_of_edge_order(reverse, depth):
    nodes = [
        {
            "id": str(index),
            "type": "videoNode",
            "data": {},
            "position": {"x": index, "y": 0},
        }
        for index in range(5)
    ]
    edges = [
        {"id": str(index), "source": str(index), "target": str(index + 1)}
        for index in range(4)
    ]
    canvas = {
        **F["canvas"],
        "nodes": nodes,
        "edges": list(reversed(edges)) if reverse else edges,
    }
    result = read([{"type": "neighbor_graph", "node_id": "0", "depth": depth}], canvas)
    assert {node["node_id"] for node in result["responses"][0]["data"]["nodes"]} == {
        str(index) for index in range(depth + 1)
    }


def test_decoded_object_cursor_is_a_validation_error():
    import base64

    cursor = base64.urlsafe_b64encode(b"{}").decode()
    with pytest.raises(ValueError, match="invalid or stale cursor"):
        read([{"type": "canvas_summary", "cursor": cursor}])
