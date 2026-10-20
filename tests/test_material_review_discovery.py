from copy import deepcopy
import pytest
from PIL import Image
from test_material_work import setup_work
from novelvideo.freezone.remix.material_execution import (
    read_material_state, review_material_candidate, confirm_material_review,
)


def fixture(tmp_path):
    store, _, prepared = setup_work(tmp_path)
    saved = store.read_latest(prepared['work_ref']['artifact_id'])
    work = saved['payload']
    work['steps'][0].update(source='upload')
    work['steps'][0].pop('recipe_id')
    saved = store.revise(saved['id'], 1, work, saved['upstream_refs'])
    ref = {'artifact_id': saved['id'], 'revision': saved['revision']}
    Image.new('RGB', (16, 12), 'red').save(tmp_path / 'picture.png')
    canvas = {'nodes': [{'id': 'n', 'data': {'imageUrl': '/static/projects/project/picture.png'}}]}
    review = review_material_candidate(store, ref, 'image', canvas, 'n')
    return store, ref, canvas, review


def discover(store, ref, canvas, canvas_id='canvas'):
    return read_material_state(store, ref, canvas=canvas, canvas_id=canvas_id)


def test_latest_human_confirmation_and_original_state_preserved(tmp_path):
    store, ref, canvas, pending = fixture(tmp_path)
    original = read_material_state(store, ref)
    assert discover(store, ref, canvas)['material_reviews'][0]['content']['status'] == 'unavailable'
    confirmed = confirm_material_review(store, pending['review_ref'], canvas, canvas_id='canvas', requester_user_id='alice')
    state = discover(store, ref, canvas)
    row, = state['material_reviews']
    assert row['review_ref'] == confirmed['review_ref']
    assert row['resource'] == confirmed['review']['resource']
    assert row['content']['status'] == 'passed'
    assert row['matches_current_work_revision'] is True
    assert all(state[key] == value for key, value in original.items() if key != 'material_reviews')
    assert store.read_latest(ref['artifact_id'])['revision'] == ref['revision']


@pytest.mark.parametrize('change', ['canvas', 'user', 'resource', 'lineage', 'project'])
def test_discovery_rejects_other_scope_or_stale_lineage(tmp_path, change):
    store, ref, canvas, pending = fixture(tmp_path)
    confirmed = confirm_material_review(store, pending['review_ref'], canvas, canvas_id='canvas', requester_user_id='alice')
    canvas_id = 'canvas'
    if change == 'canvas': canvas_id = 'other'
    if change == 'user': store.created_by = 'bob'
    if change == 'resource': Image.new('RGB', (16, 12), 'blue').save(tmp_path / 'picture.png')
    if change == 'work_revision':
        saved = store.read_latest(ref['artifact_id'])
        store.revise(saved['id'], saved['revision'], saved['payload'], saved['upstream_refs'])
    if change in {'lineage', 'project'}:
        import json
        path = store._path(confirmed['review_ref']['artifact_id'], confirmed['review_ref']['revision'], '.json')
        header = json.loads(path.read_text())
        if change == 'lineage': header['upstream_refs'] = []
        else: header['project_id'] = 'foreign'
        path.write_text(json.dumps(header))
    assert discover(store, ref, canvas, canvas_id)['material_reviews'] == []


def test_rejected_latest_is_discovered_without_old_pass(tmp_path):
    store, ref, canvas, pending = fixture(tmp_path)
    saved = store.read_latest(pending['review_ref']['artifact_id'])
    payload = deepcopy(saved['payload']); payload['content']['status'] = 'rejected'
    current = store.revise(saved['id'], saved['revision'], payload, saved['upstream_refs'])
    row, = discover(store, ref, canvas)['material_reviews']
    assert row['review_ref']['revision'] == current['revision']
    assert row['content']['status'] == 'rejected'


def test_other_work_is_not_discovered_and_output_is_bounded(tmp_path):
    store, ref, canvas, pending = fixture(tmp_path)
    saved = store.read_latest(ref['artifact_id'])
    other = store.create('material_work_package.v1', saved['payload'], saved['upstream_refs'])
    assert discover(store, {'artifact_id': other['id'], 'revision': 1}, canvas)['material_reviews'] == []
    for _ in range(51):
        review_material_candidate(store, ref, 'image', canvas, 'n')
    state = discover(store, ref, canvas)
    assert len(state['material_reviews']) == 50
    assert state['material_reviews_truncated'] is True
    import json
    assert len(json.dumps(state['material_reviews'], ensure_ascii=False).encode()) <= 65536


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['get', 'post'])
async def test_scoped_read_routes_expose_latest_confirmation(tmp_path, monkeypatch, method):
    from novelvideo.api.routes import reference_material as route
    store, ref, canvas, pending = fixture(tmp_path)
    confirmed = confirm_material_review(store, pending['review_ref'], canvas, canvas_id='canvas', requester_user_id='alice')
    async def scope(project, canvas_id, user, work_ref, role):
        assert (project, canvas_id, role) == ('project', 'canvas', 'viewer')
        return None, canvas, store, read_material_state(store, work_ref)
    monkeypatch.setattr(route, '_scope', scope)
    if method == 'get':
        result = await route.material_state_get('project', 'canvas', ref['artifact_id'], ref['revision'], {})
    else:
        result = await route.material_state('project', 'canvas', route.WorkBody(work_ref=ref), {})
    assert result['data']['material_reviews'][0]['review_ref'] == confirmed['review_ref']


def test_plugin_preserves_saved_review_and_exact_resource(tmp_path, monkeypatch):
    import json
    from test_freezone_plugin import _load_plugin_module
    store, ref, canvas, pending = fixture(tmp_path)
    confirmed = confirm_material_review(store, pending['review_ref'], canvas, canvas_id='canvas', requester_user_id='alice')
    state = discover(store, ref, canvas)
    plugin = _load_plugin_module()
    monkeypatch.setenv('DRAMACLAW_PROJECT_ID', 'project')
    monkeypatch.setenv('DRAMACLAW_CANVAS_ID', 'canvas')
    monkeypatch.setattr(plugin, '_request', lambda *a, **kw: {'ok': True, 'data': state})
    _, schema, handler = next(t for t in plugin.TOOLS if t[0] == 'freezone_get_material_work')
    result = handler({'work_ref': ref})
    assert result['material_reviews'] == state['material_reviews']
    assert result['material_reviews'][0]['resource'] == confirmed['review']['resource']
    assert result['material_reviews_truncated'] is False
    assert 'host-certified review_compatible_with_current_work=true' in schema['description']
    assert 'never rewrite its basis or infer compatibility yourself' in schema['description']


def test_scan_is_bounded_and_reports_incomplete_results(tmp_path, monkeypatch):
    store, ref, canvas, pending = fixture(tmp_path)
    # Feed 513 non-artifact entries without relying on filesystem ordering.
    from pathlib import Path
    original = Path.iterdir
    def entries(path):
        if path == store.root:
            return iter([store.root / '_skip'] * 513)
        return original(path)
    monkeypatch.setattr(Path, 'iterdir', entries)
    state = discover(store, ref, canvas)
    assert state['material_reviews'] == []
    assert state['material_reviews_truncated'] is True


def test_historical_same_work_review_is_evidence_not_current_acceptance(tmp_path):
    from novelvideo.freezone.remix.material_work import accept_material_candidate
    store, ref, canvas, pending = fixture(tmp_path)
    confirmed = confirm_material_review(store, pending['review_ref'], canvas, canvas_id='canvas', requester_user_id='alice')
    saved = store.read_latest(ref['artifact_id'])
    current = store.revise(saved['id'], saved['revision'], saved['payload'], saved['upstream_refs'])
    current_ref = {'artifact_id': current['id'], 'revision': current['revision']}
    row, = discover(store, current_ref, canvas)['material_reviews']
    assert row['work_ref'] == ref
    assert row['review_ref'] == confirmed['review_ref']
    assert row['matches_current_work_revision'] is False
    with pytest.raises(ValueError):
        accept_material_candidate(store, current_ref, 'image', row['resource'], row['review_ref'], 'stale-proof')
    assert store.read_latest(current['id'])['revision'] == current['revision']
