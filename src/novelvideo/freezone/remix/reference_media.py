"""Read verified original media and imported frames from a saved analysis."""
from __future__ import annotations

import re
from urllib.parse import quote, unquote, urlsplit


def _artifact_ref(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    artifact_id = value.get('artifactId', value.get('artifact_id'))
    revision = value.get('revision')
    if not isinstance(artifact_id, str) or type(revision) is not int or revision < 1:
        return None
    return {'artifact_id': artifact_id, 'revision': revision}


def _project_relative(store, url: object) -> str | None:
    if not isinstance(url, str) or not url:
        return None
    parsed = urlsplit(url)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        return None
    if parsed.query and not re.fullmatch(r'v=[A-Za-z0-9._-]{1,128}', parsed.query):
        return None
    project = quote(store.project_id, safe='')
    prefixes = (f'/api/v1/projects/{project}/media/', f'/static/projects/{project}/')
    for prefix in prefixes:
        if parsed.path.startswith(prefix):
            return unquote(parsed.path[len(prefix):])
    return None


def _inspected(store, relative: str | None, kind: str) -> dict | None:
    if not relative:
        return None
    try:
        from novelvideo.api.routes.remix_media import _inspect_existing_media
        result = _inspect_existing_media(store.project_dir, relative)
    except (OSError, ValueError):
        return None
    return result if result['media_kind'] == kind else None


def _frame_candidates(analysis: dict, events: list[dict]) -> list[dict]:
    story = analysis.get('story') or {}
    candidates = []
    for event in events:
        if not isinstance(event, dict):
            continue
        observed = event.get('observed_frame') or {}
        url = observed.get('url') or event.get('frame_url')
        candidates.append({'url': url, 'event_id': event.get('event_id'),
            'start_us': event.get('start_us'), 'end_us': event.get('end_us')})
    for url in story.get('frame_urls') or []:
        if not any(row['url'] == url for row in candidates):
            candidates.append({'url': url, 'event_id': None, 'start_us': None, 'end_us': None})
    return candidates


def list_reference_media(*, store, canvas: dict, analysis_node_id: str) -> list[dict]:
    """Return readable source evidence in one authorized project canvas.

    The saved analysis owns the original media hash and frame candidates. Canvas
    nodes provide current identity and imported-frame links, never new evidence.
    """
    if not isinstance(canvas, dict) or canvas.get('project_id') != store.project_id:
        return []
    nodes = [node for node in canvas.get('nodes', []) if isinstance(node, dict)]
    story_nodes = [node for node in nodes if node.get('id') == analysis_node_id and node.get('type') == 'videoStoryNode']
    if len(story_nodes) != 1:
        return []
    story_data = story_nodes[0].get('data') or {}
    if story_data.get('isAnalyzing') or story_data.get('analysisError') or story_data.get('blueprintDirty'):
        return []
    analysis_ref = _artifact_ref(story_data.get('referenceAnalysisRef'))
    if analysis_ref is None:
        return []
    try:
        artifact = store.read(analysis_ref['artifact_id'], analysis_ref['revision'])
    except (OSError, ValueError):
        return []
    if artifact['schema'] != 'reference_analysis.v1' or artifact.get('created_by') != store.created_by:
        return []
    analysis = artifact['payload']
    events = (analysis.get('story') or {}).get('reference_events') or []
    saved_blueprint_ref = _artifact_ref(analysis.get('blueprint_ref'))
    current_blueprint_ref = _artifact_ref(story_data.get('referenceBlueprintRef'))
    if saved_blueprint_ref:
        if (current_blueprint_ref is None or
                current_blueprint_ref['artifact_id'] != saved_blueprint_ref['artifact_id']):
            return []
        try:
            blueprint = store.read(current_blueprint_ref['artifact_id'], current_blueprint_ref['revision'])
        except (OSError, ValueError):
            return []
        if (blueprint['schema'] != 'reference_blueprint.v1' or
                blueprint.get('created_by') != store.created_by or
                _artifact_ref(blueprint['payload'].get('evidence_ref')) != _artifact_ref(analysis.get('evidence_ref'))):
            return []
        events = blueprint['payload'].get('events') or []
    source_relative = analysis.get('media_path')
    source_media = _inspected(store, source_relative, 'video')
    if (source_media is None or analysis.get('media_hash') != 'sha256:' + source_media['media_hash']
            or _project_relative(store, story_data.get('sourceVideoUrl')) != source_relative):
        return []
    source_ids = {edge.get('source') for edge in canvas.get('edges', [])
                  if isinstance(edge, dict) and edge.get('target') == analysis_node_id}
    source_nodes = [node for node in nodes if node.get('id') in source_ids and node.get('type') == 'videoNode'
                    and _project_relative(store, (node.get('data') or {}).get('videoUrl')) == source_relative]
    if len(source_nodes) != 1:
        return []
    source_id = source_nodes[0]['id']
    common = {'source_node_id': source_id, 'analysis_node_id': analysis_node_id,
              'analysis_ref': analysis_ref}
    rows = [{**common, 'event_id': None, 'media_kind': 'video',
             'resource_ref': source_media, 'start_us': None, 'end_us': None,
             'existing_node_id': source_id}]
    frame_by_relative: dict[str, dict] = {}
    for candidate in _frame_candidates(analysis, events):
        relative = _project_relative(store, candidate.get('url'))
        if relative is None or relative in frame_by_relative:
            continue
        inspected = _inspected(store, relative, 'image')
        if inspected is None:
            continue
        frame_by_relative[relative] = {**candidate, 'resource_ref': inspected}
    for frame_index, (relative, candidate) in enumerate(frame_by_relative.items(), start=1):
        shot = None
        for index, row in enumerate(story_data.get('rows') or []):
            if isinstance(row, dict) and _project_relative(store, row.get('keyframeUrl')) == relative:
                shot = str(row.get('shotNumber') if row.get('shotNumber') is not None else index + 1)
                break
        if shot is None:
            for index, event in enumerate(story_data.get('referenceEvents') or []):
                if isinstance(event, dict) and (_project_relative(store, event.get('frame_url')) == relative
                        or _project_relative(store, (event.get('observed_frame') or {}).get('url')) == relative):
                    shot = str(index + 1)
                    break
        if shot is None:
            shot = str(frame_index)
        imported = [node for node in nodes if node.get('type') == 'uploadNode'
            and (node.get('data') or {}).get('sourceStoryNodeId') == analysis_node_id
            and shot is not None and str((node.get('data') or {}).get('sourceStoryShotNumber')) == shot
            and _project_relative(store, (node.get('data') or {}).get('imageUrl')) == relative
            and not (node.get('data') or {}).get('workflowResultStale')]
        # The browser's import button records a shot number. The saved frame
        # proves media identity; a duplicate or ambiguous import is not reused.
        existing_id = imported[0]['id'] if len(imported) == 1 else None
        event_id = candidate.get('event_id')
        if not isinstance(event_id, str) or not event_id:
            event_id = None
        start, end = candidate.get('start_us'), candidate.get('end_us')
        if type(start) is not int or type(end) is not int or start < 0 or end <= start:
            start = end = None
        rows.append({**common, 'event_id': event_id, 'media_kind': 'image',
            'resource_ref': candidate['resource_ref'], 'start_us': start,
            'end_us': end, 'shot_number': shot, 'existing_node_id': existing_id})
    for event in events:
        if not isinstance(event, dict):
            continue
        event_id, start, end = event.get('event_id'), event.get('start_us'), event.get('end_us')
        if not isinstance(event_id, str) or not event_id or type(start) is not int or type(end) is not int or not 0 <= start < end:
            continue
        imported = [node for node in nodes if node.get('type') == 'videoNode'
            and (node.get('data') or {}).get('sourceStoryNodeId') == analysis_node_id
            and (node.get('data') or {}).get('sourceStoryEventId') == event_id
            and type((node.get('data') or {}).get('sourceStartMs')) in (int, float)
            and type((node.get('data') or {}).get('sourceEndMs')) in (int, float)
            and abs((node.get('data') or {})['sourceStartMs'] * 1000 - start) <= 1000
            and abs((node.get('data') or {})['sourceEndMs'] * 1000 - end) <= 1000
            and not (node.get('data') or {}).get('workflowResultStale')]
        if len(imported) != 1:
            continue
        node = imported[0]
        clip = _inspected(store, _project_relative(store, (node.get('data') or {}).get('videoUrl')), 'video')
        if clip is not None:
            rows.append({**common, 'event_id': event_id, 'media_kind': 'video',
                         'resource_ref': clip, 'start_us': start, 'end_us': end,
                         'existing_node_id': node['id']})
    return rows
