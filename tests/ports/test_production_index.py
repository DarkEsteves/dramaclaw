from concurrent.futures import ThreadPoolExecutor

import pytest


def scope(tmp_path, **values):
    return {"project_dir": tmp_path, "project_id": "p", "canvas_id": "c",
            "requester_user_id": "u", "agent_id": "main", **values}


def record(commission="commission", phase="preparing_materials"):
    return {"commission_ref": {"artifact_id": commission, "revision": 1},
            "plan_ref": {"artifact_id": "plan", "revision": 1},
            "work_ref": {"artifact_id": "work", "revision": 1}, "phase": phase,
            "execution_status": "waiting_user",
            "progress": {"materials_total": 9, "materials_accepted": 5}}


def test_index_is_scope_isolated_and_idempotent(tmp_path):
    from novelvideo.ports.local.production_index import LocalProductionIndex

    index = LocalProductionIndex()
    current = index.publish(scope(tmp_path), record(), expected_revision=None)
    assert index.read_current(scope(tmp_path), "commission") == current
    assert index.publish(scope(tmp_path), record(), expected_revision="old") == current
    for changed in ({"project_id": "other"}, {"canvas_id": "other"},
                    {"requester_user_id": "other"}, {"agent_id": "other"}, {"tenant_id": "other"}):
        assert index.read_current(scope(tmp_path, **changed), "commission") is None


def test_conflicting_publish_does_not_replace_current(tmp_path):
    from novelvideo.ports.local.production_index import LocalProductionIndex, ProductionIndexConflict

    index = LocalProductionIndex()
    first = index.publish(scope(tmp_path), record(), expected_revision=None)
    with pytest.raises(ProductionIndexConflict):
        index.publish(scope(tmp_path), record(phase="composing"), expected_revision="wrong")
    assert index.read_current(scope(tmp_path), "commission") == first


def test_same_expected_revision_has_only_one_writer(tmp_path):
    from novelvideo.ports.local.production_index import LocalProductionIndex, ProductionIndexConflict

    index = LocalProductionIndex()
    first = index.publish(scope(tmp_path), record(), expected_revision=None)
    def write(phase):
        try:
            return index.publish(scope(tmp_path), record(phase=phase),
                                 expected_revision=first["index_revision"])
        except ProductionIndexConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ["aligning_speech", "composing"]))
    assert sum(value is not None for value in results) == 1


def test_history_pagination_is_stable_and_cursor_scope_bound(tmp_path):
    from novelvideo.ports.local.production_index import LocalProductionIndex

    index = LocalProductionIndex()
    current = index.publish(scope(tmp_path), record(), expected_revision=None)
    for phase in ("aligning_speech", "composing", "reviewing_output"):
        current = index.publish(scope(tmp_path), record(phase=phase),
                                expected_revision=current["index_revision"])
    first = index.history(scope(tmp_path), "commission", cursor=None, limit=2)
    assert len(first["items"]) == 2 and first["next_cursor"]
    second = index.history(scope(tmp_path), "commission", cursor=first["next_cursor"], limit=2)
    assert len(second["items"]) == 2 and second["next_cursor"] is None
    assert {item["phase"] for item in first["items"] + second["items"]} == {
        "preparing_materials", "aligning_speech", "composing", "reviewing_output"}
    with pytest.raises(ValueError, match="cursor"):
        index.history(scope(tmp_path, canvas_id="other"), "commission",
                      cursor=first["next_cursor"], limit=2)


def test_hot_read_does_not_enumerate_artifact_directories(tmp_path, monkeypatch):
    from pathlib import Path
    from novelvideo.ports.local.production_index import LocalProductionIndex

    index = LocalProductionIndex()
    current = index.publish(scope(tmp_path), record(), expected_revision=None)
    def forbidden(*args, **kwargs):
        raise AssertionError("hot index read must not enumerate history")
    monkeypatch.setattr(Path, "iterdir", forbidden)
    monkeypatch.setattr(Path, "glob", forbidden)
    assert index.read_current(scope(tmp_path), "commission") == current


def test_symlink_storage_is_rejected(tmp_path):
    from novelvideo.ports.local.production_index import LocalProductionIndex

    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "freezone").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        LocalProductionIndex().publish(scope(tmp_path), record(), expected_revision=None)


@pytest.mark.parametrize("limit", [0, 101, True])
def test_history_is_bounded(tmp_path, limit):
    from novelvideo.ports.local.production_index import LocalProductionIndex

    with pytest.raises(ValueError):
        LocalProductionIndex().history(scope(tmp_path), "commission", cursor=None, limit=limit)


def test_optional_index_does_not_install_local_storage_for_ee(monkeypatch):
    import novelvideo.ports as ports

    class PortNotRegistered(RuntimeError):
        pass
    def missing(name):
        raise PortNotRegistered(name)
    monkeypatch.setattr(ports, "get_port", missing)
    monkeypatch.setattr(ports.runtime_env, "edition", lambda: "ee")
    assert ports.get_production_index() is None
    monkeypatch.setattr(ports.runtime_env, "edition", lambda: "ce")
    assert ports.get_production_index() is not None


def test_projection_identity_is_preserved_separately_from_index_cas(tmp_path):
    from novelvideo.ports.local.production_index import LocalProductionIndex

    index = LocalProductionIndex()
    state = {**record(), "projection_revision": "evidence-digest"}
    saved = index.publish(scope(tmp_path), state, expected_revision=None)
    assert saved["projection_revision"] == "evidence-digest"
    assert saved["index_revision"] != saved["projection_revision"]
    assert index.read_current(scope(tmp_path), "commission") == saved


@pytest.mark.parametrize("replay", [False, True])
def test_lost_lease_cannot_publish_or_claim_idempotent_success(tmp_path, monkeypatch, replay):
    from contextlib import contextmanager
    from novelvideo.ports.local.production_index import LocalProductionIndex

    index = LocalProductionIndex()
    if replay:
        saved = index.publish(scope(tmp_path), record(), expected_revision=None)
    else:
        saved = None

    class Guard:
        def reassert(self):
            raise RuntimeError("fixture lease lost")

    class Mutex:
        @contextmanager
        def write_mutex(self, *args):
            yield Guard()

    monkeypatch.setattr("novelvideo.ports.get_canvas_write_mutex", lambda: Mutex())
    with pytest.raises(RuntimeError, match="lease lost"):
        index.publish(scope(tmp_path), record(), expected_revision=None)
    assert index.read_current(scope(tmp_path), "commission") == saved


def test_tenants_publish_same_ids_without_sharing_history_or_cas(tmp_path):
    from novelvideo.ports.local.production_index import LocalProductionIndex

    index = LocalProductionIndex()
    first_scope, second_scope = scope(tmp_path, tenant_id="tenant-a"), scope(tmp_path, tenant_id="tenant-b")
    first = index.publish(first_scope, record(), expected_revision=None)
    second = index.publish(second_scope, record(phase="composing"), expected_revision=None)
    assert index.read_current(first_scope, "commission") == first
    assert index.read_current(second_scope, "commission") == second
    assert index.history(first_scope, "commission", cursor=None, limit=100)["items"] == [first]
    assert index.history(second_scope, "commission", cursor=None, limit=100)["items"] == [second]


def test_lease_loss_after_history_write_keeps_previous_current_revision(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from novelvideo.ports.local.production_index import LocalProductionIndex

    index = LocalProductionIndex()
    saved = index.publish(scope(tmp_path), record(), expected_revision=None)

    class Guard:
        checks = 0

        def reassert(self):
            self.checks += 1
            if self.checks == 2:
                raise RuntimeError("fixture lease lost before head publication")

    class Mutex:
        @contextmanager
        def write_mutex(self, *args):
            yield Guard()

    monkeypatch.setattr("novelvideo.ports.get_canvas_write_mutex", lambda: Mutex())
    with pytest.raises(RuntimeError, match="lease lost"):
        index.publish(scope(tmp_path), record(phase="composing"), expected_revision=saved["index_revision"])
    assert index.read_current(scope(tmp_path), "commission") == saved
    assert index.history(scope(tmp_path), "commission", cursor=None, limit=100)["items"] == [saved]


def test_corrupt_recovery_preserves_valid_publisher_cas(tmp_path):
    import json
    from novelvideo.ports.local.production_index import LocalProductionIndex, ProductionIndexConflict

    index = LocalProductionIndex()
    first = index.publish(scope(tmp_path), record(), expected_revision=None)
    folder, _, _ = index._location(scope(tmp_path), 'commission')
    path = folder / 'head.json'
    envelope = json.loads(path.read_text())
    envelope['record']['index_revision'] = 'corrupted'
    path.write_text(json.dumps(envelope))
    assert hasattr(index, 'recover_current'), 'optional index needs scoped recovery under injected mutex'
    assert index.recover_current(scope(tmp_path), 'commission') is None
    winner = index.publish(scope(tmp_path), record(phase='composing'), expected_revision=None)
    assert index.recover_current(scope(tmp_path), 'commission') == winner
    with pytest.raises(ProductionIndexConflict):
        index.publish(scope(tmp_path), record(phase='aligning_speech'), expected_revision=None)
    assert index.read_current(scope(tmp_path), 'commission') == winner


def test_legacy_history_cursor_remains_readable_after_valid_publication(tmp_path):
    import hashlib
    import json
    from novelvideo.ports.local.production_index import LocalProductionIndex

    index = LocalProductionIndex()
    first = index.publish(scope(tmp_path), record(), expected_revision=None)
    folder, _, scope_hash = index._location(scope(tmp_path), 'commission')
    path = folder / 'head.json'
    head = json.loads(path.read_text())
    head.pop('history_generation')
    path.write_text(json.dumps(head))
    # Honest legacy caches omitted the generation from every envelope.
    old_path = folder / '1.json'
    old_envelope = json.loads(old_path.read_text())
    old_envelope.pop('history_generation')
    old_path.write_text(json.dumps(old_envelope))
    index.publish(scope(tmp_path), record(phase='composing'), expected_revision=first['index_revision'])
    legacy_cursor = hashlib.sha256(f'{scope_hash}:commission'.encode()).hexdigest()[:24] + ':1'
    assert index.history(scope(tmp_path), 'commission', cursor=legacy_cursor, limit=1)['items'] == [first]


def test_history_rejects_replacement_generation_after_head_read(tmp_path, monkeypatch):
    import json
    import novelvideo.ports.local.production_index as module

    index = module.LocalProductionIndex()
    first = index.publish(scope(tmp_path), record(phase='old-1'), expected_revision=None)
    index.publish(scope(tmp_path), record(phase='old-2'), expected_revision=first['index_revision'])
    cursor = index.history(scope(tmp_path), 'commission', cursor=None, limit=1)['next_cursor']
    folder, _, _ = index._location(scope(tmp_path), 'commission')
    read = module._read
    replaced = False

    def recovery_after_old_head(path):
        nonlocal replaced
        value = read(path)
        if path == folder / 'head.json' and not replaced:
            replaced = True
            bad = json.loads(path.read_text())
            bad['record']['index_revision'] = 'corrupted'
            path.write_text(json.dumps(bad))
            assert index.recover_current(scope(tmp_path), 'commission') is None
            index.publish(scope(tmp_path), record(phase='replacement'), expected_revision=None)
        return value

    monkeypatch.setattr(module, '_read', recovery_after_old_head)
    with pytest.raises(ValueError, match='history|generation'):
        index.history(scope(tmp_path), 'commission', cursor=cursor, limit=1)
    assert replaced
    assert index.read_current(scope(tmp_path), 'commission')['phase'] == 'replacement'


@pytest.mark.parametrize('field,value', [('history_generation', []), ('history_generation', 1),
                                        ('history_generation', 'invalid'), ('sequence', '2')])
def test_malformed_history_head_is_a_controlled_error(tmp_path, field, value):
    import json
    from novelvideo.ports.local.production_index import LocalProductionIndex

    index = LocalProductionIndex()
    first = index.publish(scope(tmp_path), record(), expected_revision=None)
    index.publish(scope(tmp_path), record(phase='composing'), expected_revision=first['index_revision'])
    cursor = index.history(scope(tmp_path), 'commission', cursor=None, limit=1)['next_cursor']
    folder, _, _ = index._location(scope(tmp_path), 'commission')
    path = folder / 'head.json'
    head = json.loads(path.read_text())
    head[field] = value
    path.write_text(json.dumps(head))
    with pytest.raises(ValueError):
        index.history(scope(tmp_path), 'commission', cursor=cursor, limit=1)
