"""Reference evidence is scoped to saved analysis and inspected project media."""
import hashlib
import subprocess
from types import SimpleNamespace

import pytest

from PIL import Image

from novelvideo.freezone.remix.reference_media import list_reference_media
from novelvideo.freezone.remix.store import RemixArtifactStore


def _media(tmp_path):
    source = tmp_path / 'source.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    'color=c=red:s=16x16:r=2:d=1', '-pix_fmt', 'yuv420p',
                    '-y', str(source)], check=True, capture_output=True)
    Image.new('RGB', (16, 16), 'blue').save(tmp_path / 'frame.png')
    return hashlib.sha256(source.read_bytes()).hexdigest()


def _canvas_and_store(tmp_path):
    digest = _media(tmp_path)
    store = RemixArtifactStore(tmp_path, project_id='project', created_by='alice')
    evidence = store.create('reference_evidence.v1', {'media_hash': digest,
        'time_base': '1/1000', 'duration_us': 1_000_000, 'cuts': [],
        'representative_frames': [{'time_us': 200_000, 'pts': 200}]}, [])
    evidence_ref = {'artifact_id': evidence['id'], 'revision': 1}
    frame_url = '/static/projects/project/frame.png'
    blueprint = store.create('reference_blueprint.v1', {'narrative_role': 'Product reveal',
        'events': [{'event_id': 'event-1', 'start_us': 0, 'end_us': 1_000_000,
            'observed_frame': {'evidence_ref': evidence_ref, 'time_us': 200_000,
                               'url': frame_url}, 'spoken_range': None,
            'model': {}, 'user_override': {'narrative_role': 'Reveal',
                                            'review_state': 'needs_review'}}],
        'preserve_replace_rules': [], 'observations': [], 'inferences': [],
        'user_choices': [], 'evidence_ref': evidence_ref}, [evidence_ref])
    blueprint_ref = {'artifact_id': blueprint['id'], 'revision': 1}
    analysis = store.create('reference_analysis.v1', {'media_path': 'source.mp4',
        'media_hash': 'sha256:' + digest, 'evidence_ref': evidence_ref,
        'blueprint_ref': blueprint_ref,
        'transcript_status': 'no_audio', 'story': {'frame_urls': [
            '/static/projects/project/frame.png'], 'frame_times_us': [200_000],
            'reference_events': [{'start_us': 0,
                                  'end_us': 1_000_000, 'observed_frame': {
                                      'url': '/static/projects/project/frame.png', 'time_us': 200_000}}]},
        'coverage': {}}, [evidence_ref, blueprint_ref])
    source_url = '/static/projects/project/source.mp4'
    canvas = {'project_id': 'project', 'nodes': [
        {'id': 'source', 'type': 'videoNode', 'data': {'videoUrl': source_url}},
        {'id': 'story', 'type': 'videoStoryNode', 'data': {
            'sourceVideoUrl': source_url,
            'referenceAnalysisRef': {'artifactId': analysis['id'], 'revision': 1},
            'referenceBlueprintRef': {'artifactId': blueprint['id'], 'revision': 1},
            'rows': [{'shotNumber': 1, 'keyframeUrl': frame_url}],
            'referenceEvents': [{'event_id': 'event-1', 'start_us': 0,
                                 'end_us': 1_000_000, 'frame_url': frame_url}] }},
        {'id': 'frame-node', 'type': 'uploadNode', 'data': {
            'imageUrl': frame_url, 'imageOnly': True, 'sourceStoryNodeId': 'story',
            'sourceStoryShotNumber': '1'}}],
        'edges': [{'source': 'source', 'target': 'story'}]}
    return store, canvas


def test_inventory_links_saved_video_frame_event_and_imported_image(tmp_path):
    store, canvas = _canvas_and_store(tmp_path)
    rows = list_reference_media(store=store, canvas=canvas, analysis_node_id='story')
    assert {row['media_kind'] for row in rows} == {'video', 'image'}
    source = next(row for row in rows if row['media_kind'] == 'video')
    frame = next(row for row in rows if row['media_kind'] == 'image')
    assert source['source_node_id'] == 'source' and source['existing_node_id'] == 'source'
    assert source['resource_ref']['media_path'] == 'source.mp4'
    assert source['event_id'] is None and source['start_us'] is None
    assert frame['event_id'] == 'event-1'
    assert frame['shot_number'] == '1'
    assert frame['start_us'] == 0 and frame['end_us'] == 1_000_000
    assert frame['existing_node_id'] == 'frame-node'
    assert frame['resource_ref']['media_kind'] == 'image'


def test_inventory_accepts_project_cache_version_without_cross_project_leak(tmp_path):
    from novelvideo.freezone.remix.reference_media import _project_relative

    store, canvas = _canvas_and_store(tmp_path)
    assert _project_relative(store, '/static/projects/project/frame.png?v=123') == 'frame.png'
    assert _project_relative(store, '/static/projects/other/frame.png?v=123') is None
    assert _project_relative(store, 'https://outside.test/frame.png?v=123') is None
    canvas['nodes'][1]['data']['sourceVideoUrl'] += '?v=123'
    canvas['nodes'][0]['data']['videoUrl'] += '?v=123'
    canvas['nodes'][1]['data']['rows'][0]['keyframeUrl'] += '?v=123'
    assert {row['media_kind'] for row in list_reference_media(
        store=store, canvas=canvas, analysis_node_id='story')} == {'video', 'image'}


def test_inventory_drops_foreign_missing_or_mismatched_evidence(tmp_path):
    store, canvas = _canvas_and_store(tmp_path)
    canvas['nodes'][1]['data']['rows'][0]['keyframeUrl'] = '/static/projects/foreign/frame.png'
    canvas['nodes'][1]['data']['referenceEvents'][0]['frame_url'] = '/static/projects/foreign/frame.png'
    # The saved story still provides the actual frame; imported node cannot
    # claim a different URL or a different source story.
    canvas['nodes'][2]['data']['sourceStoryNodeId'] = 'another-story'
    rows = list_reference_media(store=store, canvas=canvas, analysis_node_id='story')
    assert next(row for row in rows if row['media_kind'] == 'image')['existing_node_id'] is None
    canvas['nodes'][0]['data']['videoUrl'] = '/static/projects/foreign/source.mp4'
    assert list_reference_media(store=store, canvas=canvas, analysis_node_id='story') == []


def test_inventory_rejects_modified_source_and_wrong_canvas(tmp_path):
    store, canvas = _canvas_and_store(tmp_path)
    assert list_reference_media(store=store, canvas={**canvas, 'project_id': 'other'},
                                analysis_node_id='story') == []
    (tmp_path / 'source.mp4').write_bytes(b'changed')
    assert list_reference_media(store=store, canvas=canvas, analysis_node_id='story') == []


def test_imported_frame_shot_identity_must_match_story_import(tmp_path):
    store, canvas = _canvas_and_store(tmp_path)
    canvas['nodes'][2]['data']['sourceStoryShotNumber'] = '99'
    rows = list_reference_media(store=store, canvas=canvas, analysis_node_id='story')
    frame = next(row for row in rows if row['media_kind'] == 'image')
    assert frame['shot_number'] == '1'
    assert frame['existing_node_id'] is None


def test_inventory_includes_only_matching_imported_event_clip(tmp_path):
    store, canvas = _canvas_and_store(tmp_path)
    (tmp_path / 'clip.mp4').write_bytes((tmp_path / 'source.mp4').read_bytes())
    canvas['nodes'].append({'id': 'clip-node', 'type': 'videoNode', 'data': {
        'videoUrl': '/static/projects/project/clip.mp4', 'sourceStoryNodeId': 'story',
        'sourceStoryEventId': 'event-1', 'sourceStartMs': 0, 'sourceEndMs': 1000}})
    rows = list_reference_media(store=store, canvas=canvas, analysis_node_id='story')
    clip = next(row for row in rows if row['existing_node_id'] == 'clip-node')
    assert clip['media_kind'] == 'video' and clip['event_id'] == 'event-1'
    assert (clip['start_us'], clip['end_us']) == (0, 1_000_000)
    canvas['nodes'][-1]['data']['sourceStoryEventId'] = 'invented-event'
    assert all(row['existing_node_id'] != 'clip-node' for row in list_reference_media(
        store=store, canvas=canvas, analysis_node_id='story'))


@pytest.mark.asyncio
async def test_inventory_route_reads_authenticated_canvas(tmp_path, monkeypatch):
    from novelvideo.api.routes import reference_material as routes, reference_director
    store, canvas = _canvas_and_store(tmp_path)
    async def context(*args):
        return SimpleNamespace(state_dir=tmp_path), canvas, store
    monkeypatch.setattr(reference_director, '_context', context)
    result = await routes.reference_media_inventory('project', 'canvas',
        routes.ReferenceMediaBody(analysis_node_id='story'), {})
    assert result['data'][0]['resource_ref']['media_kind'] == 'video'
