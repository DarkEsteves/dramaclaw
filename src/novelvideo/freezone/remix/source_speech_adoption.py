"""Trusted full-source observation adoption into the current saved speech take."""
from __future__ import annotations

from copy import deepcopy
from contextlib import nullcontext
import hashlib
import re
import unicodedata

from .store import RemixArtifactStore, RemixArtifactConflict
from .speech_correction import _spoken, preview_recognized_words_to_plan_cues

PRODUCER = 'remix-adopt-source-speech.v1'


class SourceAdoptionPending(TimeoutError):
    """A durable adoption may exist; reconcile without recognizing again."""


class SourceSpeechRemapRequired(ValueError):
    def __init__(self, targets):
        self.targets = targets
        super().__init__('speech anchors require explicit remapping: ' + ', '.join(
            row['entity_kind'] + ':' + row['entity_id'] + ':' + row['edge'] for row in targets))


class _UnlockedReads:
    """Read helpers reuse an already held artifact mutex without reacquiring it."""
    def __init__(self, store):
        self._store = store

    def __getattr__(self, name):
        return getattr(self._store, name)

    def read(self, artifact_id, revision):
        return self._store._read(artifact_id, revision)

    def read_latest(self, artifact_id):
        return self._store._read(artifact_id, self._store._latest_revision(artifact_id))


def _ref(saved):
    return {'artifact_id': saved['id'], 'revision': saved['revision']}


def _context(store, *, plan_ref, take_id, observation_ref, work_ref, scope,
             requester_user_id, correction_ref=None, require_current=False):
    from .material_work import _exact_ref, _fingerprint, validate_material_work_lineage
    from .material_review import validate_material_review_lineage
    from .source_speech import read_source_speech_observation
    for ref in (plan_ref, observation_ref, work_ref):
        _exact_ref(ref)
    if (scope.get('project_id') != store.project_id or not requester_user_id
            or scope.get('access_model') in {'private', 'user_private'} and scope.get('private_owner_id') != requester_user_id):
        raise ValueError('authorized observation adoption scope required')
    plan = store.read(plan_ref['artifact_id'], plan_ref['revision'])
    work = store.read(work_ref['artifact_id'], work_ref['revision'])
    if plan['schema'] != 'production_plan.v2' or work['schema'] != 'material_work_package.v1':
        raise ValueError('saved production plan and accepted material work required')
    if (work['payload']['plan_ref']['artifact_id'] != plan['id']
            or _fingerprint(store, plan['payload'], work['payload']['requirements']) != work['payload']['input_fingerprint']):
        raise ValueError('accepted material target changed')
    if require_current and (store.read_latest(plan['id'])['revision'] != plan['revision']
                            or store.read_latest(work['id'])['revision'] != work['revision']):
        raise RemixArtifactConflict('adoption plan or accepted work changed')
    take = next((row for row in plan['payload'].get('speech_takes', []) if row['id'] == take_id), None)
    if take is None:
        raise ValueError('saved speech take required')
    resource = next(row for row in plan['payload']['resources'] if row['id'] == take['resource_id'])
    targets = [row for row in work['payload']['requirements'] if take_id in row.get('take_ids', [])
               or take['origin_placement_id'] in row.get('placement_ids', [])]
    if not targets:
        raise ValueError('speech take has no accepted final material ownership')
    validate_material_work_lineage(store, work['payload'], work['upstream_refs'])
    steps = {row['id']: row for row in work['payload']['steps']}
    reviews = []
    for target in targets:
        step = steps[target['final_step_id']]
        if step['status'] != 'accepted' or step.get('accepted_resource') != resource:
            raise ValueError('speech material acceptance is missing or not bound')
        review = store.read(step['review_ref']['artifact_id'], step['review_ref']['revision'])
        payload = review['payload']
        if (review['schema'] != 'material_review.v1' or review['created_by'] != requester_user_id
                or payload['resource'] != resource or payload['step_id'] != step['id']
                or payload['work_ref']['artifact_id'] != work['id']
                or payload['technical']['status'] != 'passed' or payload['content']['status'] != 'passed'):
            raise ValueError('speech material requires actual passed review')
        if require_current and store.read_latest(review['id'])['revision'] != review['revision']:
            raise ValueError('accepted material review changed')
        decision = payload.get('user_decision') or {}
        if decision and decision.get('canvas_id') != scope.get('canvas_id'):
            raise ValueError('accepted material review belongs to another canvas')
        validate_material_review_lineage(store, payload, review['upstream_refs'])
        reviews.append(_ref(review))
    raw = store.read(observation_ref['artifact_id'], observation_ref['revision'])
    observation = read_source_speech_observation(store, observation_ref, scope=scope, resource=resource,
        language=raw['payload'].get('requested_language') or raw['payload']['language'], provider_signature=raw['payload']['provider_signature'])
    evidence = observation['payload']
    if evidence['status'] != 'completed' or evidence['timing_granularity'] != 'word':
        raise ValueError('measured word timing required for source adoption')
    all_turns = {row['id']: row for beat in plan['payload']['beats'] for row in beat.get('turns', [])}
    turns = [all_turns[key] for key in take['turn_ids']]
    words = deepcopy(evidence['recognized_words'])
    if correction_ref:
        from .candidate_speech_correction import validate_candidate_correction
        # Candidate receipt binds only immutable target identity, never current timing.
        target_turns = [{'id': t['id'], 'speaker': t['speaker'], 'text': t['text']} for t in turns]
        correction = validate_candidate_correction(store, correction_ref, observation=observation,
            target_turns=target_turns, scope=scope, requester_user_id=requester_user_id)
        if correction['payload']['work_ref']['artifact_id'] != work['id']:
            raise ValueError('candidate correction belongs to another work')
        words = deepcopy(correction['payload']['corrected_words'])
    # Recognition always covers the full source. A saved take's explicit cut
    # can select whole measured words, never invent a cut through a token.
    source_in = take['source_in_us']
    source_out = take.get('source_out_us') or resource['duration_us']
    if source_in or source_out != resource['duration_us']:
        if any(w['start_us'] < bound < w['end_us'] for w in words for bound in (source_in, source_out)):
            raise ValueError('explicit take crop crosses a measured source word')
        words = [w for w in words if source_in <= w['start_us'] and w['end_us'] <= source_out]
    projected = preview_recognized_words_to_plan_cues(words, turns, resource['duration_us'],
        source_in, source_out)
    return plan, take, resource, turns, projected, reviews, evidence['language']


def _word_char_ranges(text, words):
    positions = []
    for index, char in enumerate(text):
        for normal in unicodedata.normalize('NFKC', char).casefold():
            if normal.isalnum():
                positions.append(index)
    cursor = 0
    result = []
    for word in words:
        size = len(_spoken(word['text']))
        if size:
            start, end = positions[cursor], positions[cursor + size - 1] + 1
            cursor += size
        else:
            start = min(positions[max(0, cursor - 1)], len(text) - 1)
            end = start + 1
        result.append({**word, 'start_char': start, 'end_char': end})
    return result


def _timelines(context, *, plan_ref, observation_ref, work_ref, scope, requester_user_id, correction_ref=None):
    plan, take, resource, turns, projected, reviews, language = context
    output = []
    for turn, cue in zip(turns, projected, strict=True):
        words = _word_char_ranges(turn['text'], cue['words'])
        identities = {take['id'] + ':' + w['id']: w['id'] for w in words}
        words = [{**w, 'id': take['id'] + ':' + w['id']} for w in words]
        output.append({'media_hash': 'sha256:' + resource['media_hash'], 'language': language,
            'status': 'completed', 'transcript': turn['text'],
            'words': words, 'source_word_ids': identities,
            'plan_ref': plan_ref, 'take_id': take['id'], 'turn_id': turn['id'],
            'group_turn_ids': take['turn_ids'], 'placement_id': take['origin_placement_id'],
            'text_hash': hashlib.sha256(turn['text'].encode()).hexdigest(),
            'source_in_us': 0, 'source_out_us': resource['duration_us'],
            'observation_ref': observation_ref, 'work_ref': work_ref, 'review_refs': reviews,
            'authorization_scope': scope, 'requester_user_id': requester_user_id,
            **({'correction_ref': correction_ref} if correction_ref else {})})
    return output


def validate_adopted_timeline(store, payload, refs):
    """Recompute measured words and exact ordered turn assignment from source."""
    context = _context(_UnlockedReads(store), plan_ref=payload.get('plan_ref'), take_id=payload.get('take_id'),
        observation_ref=payload.get('observation_ref'), work_ref=payload.get('work_ref'),
        scope=payload.get('authorization_scope') or {}, requester_user_id=payload.get('requester_user_id'),
        correction_ref=payload.get('correction_ref'))
    expected = _timelines(context, plan_ref=payload['plan_ref'], observation_ref=payload['observation_ref'],
        work_ref=payload['work_ref'], scope=payload['authorization_scope'],
        requester_user_id=payload['requester_user_id'], correction_ref=payload.get('correction_ref'))
    if payload not in expected:
        raise ValueError('derived timeline differs from measured source observation')
    dependencies = [payload['plan_ref'], payload['observation_ref'], payload['work_ref'], *payload['review_refs']]
    if payload.get('correction_ref'):
        dependencies.append(payload['correction_ref'])
    if any(not any(r['artifact_id'] == d['artifact_id'] and r['revision'] == d['revision'] for r in refs) for d in dependencies):
        raise ValueError('derived timeline lacks exact observation adoption dependencies')


def _cue_words(cue, words):
    """Resolve a cue only to an explicit or unique whole measured word range."""
    text = _spoken(cue['text'])
    if not text:
        return None
    if text == _spoken(''.join(w['text'] for w in words)):
        return words
    identities = [w['id'] for w in words]
    selected = cue.get('word_ids') or []
    if selected and selected[0] in identities:
        start = identities.index(selected[0])
        end = start + len(selected)
        if identities[start:end] == selected and text == _spoken(''.join(w['text'] for w in words[start:end])):
            return words[start:end]
    matches = []
    for start in range(len(words)):
        spoken = ''
        for end in range(start, len(words)):
            spoken += _spoken(words[end]['text'])
            if spoken == text:
                matches.append(words[start:end + 1])
            if len(spoken) > len(text):
                break
    return matches[0] if len(matches) == 1 else None


def _adopted_cues(plan, take, timelines):
    """Keep cue identities and report uncertain ranges before publication."""
    by_turn = {t['payload']['turn_id']: t for t in timelines}
    cues, affected = [], set()
    existing_turns = set()
    for cue in plan.get('speech_cues', []):
        if cue['take_id'] != take['id']:
            cues.append(deepcopy(cue))
            continue
        existing_turns.add(cue['turn_id'])
        timeline = by_turn[cue['turn_id']]
        words = _cue_words(cue, timeline['payload']['words'])
        if words is None:
            affected.add(cue['id'])
            continue
        cues.append({**cue, 'source_start_us': min(w['start_us'] for w in words),
            'source_end_us': max(w['end_us'] for w in words),
            'word_ids': [w['id'] for w in words], 'timing_source': 'asr',
            'timing_status': 'ready', 'evidence_ref': _ref(timeline)})
    targets = [{'entity_kind': 'cue', 'entity_id': c['id'], 'edge': 'range', 'cue_id': c['id']}
               for c in plan.get('speech_cues', []) if c['id'] in affected]
    used_ids = {c['id'] for c in plan.get('speech_cues', [])}
    for turn_id, timeline in by_turn.items():
        if turn_id in existing_turns:
            continue
        identity = 'cue:' + turn_id
        suffix = 1
        while identity in used_ids:
            identity = 'cue:' + turn_id + ':' + str(suffix)
            suffix += 1
        used_ids.add(identity)
        payload = timeline['payload']
        words = payload['words']
        cues.append({'id': identity, 'take_id': take['id'], 'turn_id': turn_id,
            'text': payload['transcript'], 'source_start_us': min(w['start_us'] for w in words),
            'source_end_us': max(w['end_us'] for w in words),
            'word_ids': [w['id'] for w in words], 'timing_source': 'asr',
            'timing_status': 'ready', 'evidence_ref': _ref(timeline)})
    return cues, affected, targets


def adopt_source_observation(store, *, plan_ref: dict, take_id: str, observation_ref: dict,
                             command_id: str, work_ref: dict, scope: dict,
                             correction_ref: dict | None = None, requester_user_id: str | None = None,
                             _canvas_guard=None, _artifact_guard=None) -> dict:
    """Publish real per-turn word timelines and revise only the selected take."""
    from .contracts import validate_payload
    if not isinstance(command_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', command_id):
        raise ValueError('invalid adoption command_id')
    actor = requester_user_id or store.created_by
    if actor != store.created_by:
        raise ValueError('host requester differs from artifact store')
    producer = RemixArtifactStore(store.project_dir, store.project_id, created_by=actor, producer_version=PRODUCER)
    request = {'plan_ref': plan_ref, 'take_id': take_id, 'observation_ref': observation_ref,
               'work_ref': work_ref, 'scope': scope, 'correction_ref': correction_ref, 'requester_user_id': actor}
    with (producer._locked() if _artifact_guard is None else nullcontext(_artifact_guard)) as artifact_guard:
        from .reference_director import _MaterialPublicationGuard
        guard = _MaterialPublicationGuard(artifact_guard, _canvas_guard)
        guard.reassert()
        reads = _UnlockedReads(producer)
        receipt_dir = producer.root / '_source_speech_adoptions'
        if receipt_dir.is_symlink():
            raise ValueError('invalid adoption receipt directory')
        receipt_dir.mkdir(exist_ok=True)
        key = hashlib.sha256(producer._canonical([scope, actor, command_id])).hexdigest()
        receipt_path = receipt_dir / (key + '.json')
        if receipt_path.exists():
            receipt = producer._read_json(receipt_path)
            if receipt['request'] != request:
                raise ValueError('adoption command reused with different inputs')
            result = receipt['result']
            try:
                saved = reads.read(result['plan_ref']['artifact_id'], result['plan_ref']['revision'])
            except FileNotFoundError:
                saved = None
            if saved is not None:
                if saved['content_hash'] != receipt['content_hash']:
                    raise ValueError('adoption result was replaced')
                return result
        context = _context(reads, **{k: v for k, v in request.items() if k != 'command_id'}, require_current=True)
        plan, take, resource, turns, projected, reviews, language = context
        timelines = _timelines(context, **{k: request[k] for k in ('plan_ref', 'observation_ref', 'work_ref', 'scope', 'requester_user_id', 'correction_ref')})
        refs = producer._reference_list([plan_ref, observation_ref, work_ref, *reviews] + ([correction_ref] if correction_ref else []), store.project_id)
        producer._validate_upstream_refs(refs)
        prepared_timelines = []
        for timeline in timelines:
            typed = validate_payload('word_timeline.v1', timeline)
            validate_adopted_timeline(producer, typed, refs)
            identity = hashlib.sha256(producer._canonical([key, request, timeline['turn_id']])).hexdigest()
            prepared_timelines.append({'id': identity, 'revision': 1, 'payload': typed})
        updated = deepcopy(plan['payload'])
        updated['speech_cues'], affected_cues, targets = _adopted_cues(updated, take, prepared_timelines)
        old_words = {w['id'] for t in turns for w in [*t.get('words', []), *(t.get('speech_timing') or {}).get('words', [])]}
        new_words = {w['id'] for timeline in timelines for w in timeline['words']}
        removed = old_words - new_words
        for field, kind in (('placements', 'placement'), ('selections', 'selection')):
            for entity in plan['payload'].get(field, []):
                for edge in ('start', 'end'):
                    anchor = entity.get(edge) or {}
                    if anchor.get('kind') == 'word' and anchor.get('ref_id') in removed:
                        targets.append({'entity_kind': kind, 'entity_id': entity['id'], 'edge': edge, 'word_id': anchor['ref_id']})
                    elif anchor.get('kind') == 'cue' and anchor.get('ref_id') in affected_cues:
                        targets.append({'entity_kind': kind, 'entity_id': entity['id'], 'edge': edge, 'cue_id': anchor['ref_id']})
        if targets:
            raise SourceSpeechRemapRequired(targets)
        all_turns = {t['id']: t for b in updated['beats'] for t in b.get('turns', [])}
        for prepared in prepared_timelines:
            timeline, evidence_ref = prepared['payload'], _ref(prepared)
            turn = all_turns[timeline['turn_id']]
            turn['words'] = []
            turn['timing_basis'] = 'estimated'
            turn.pop('start_frame', None)
            turn.pop('end_frame', None)
            turn['speech_timing'] = {'placement_id': take['origin_placement_id'], 'media_hash': resource['media_hash'],
                'text': turn['text'], 'text_hash': timeline['text_hash'], 'evidence_ref': evidence_ref,
                'source_in_us': 0, 'source_out_us': resource['duration_us'], 'words': deepcopy(timeline['words'])}
        # Every cue/word mapping and plan reference must validate before any
        # timeline, plan, or durable adoption intent is published.
        updated = validate_payload('production_plan.v2', updated)
        saved_timelines = []
        for prepared in prepared_timelines:
            identity, typed = prepared['id'], prepared['payload']
            try:
                saved = producer._read(identity, 1)
                if saved['payload'] != typed or saved['producer_version'] != PRODUCER:
                    raise ValueError('adoption timeline identity changed')
            except FileNotFoundError:
                saved = producer._publish(artifact_id=identity, schema='word_timeline.v1', revision=1,
                    payload=typed, upstream_refs=refs, created_by=actor, guard=guard)
            saved_timelines.append(saved)
        plan_refs = producer._reference_list([*plan['upstream_refs'], *[_ref(t) for t in saved_timelines]], store.project_id)
        producer._validate_v2_lineage('production_plan.v2', updated, plan_refs, target_artifact_id=plan['id'], previous_plan=plan)
        new_revision = plan['revision'] + 1
        result = {'original_plan_ref': plan_ref, 'plan_ref': {'artifact_id': plan['id'], 'revision': new_revision},
                  'timeline_refs': [_ref(t) for t in saved_timelines], 'take_id': take['id'],
                  'observation_ref': observation_ref, 'work_ref': work_ref,
                  'source_signature': adoption_source_signature(plan['payload'], take['id'])}
        # The intent receipt precedes plan publication. A crash after either
        # write recovers the exact result without repeating recognition.
        producer._atomic_json(receipt_path, {'request': request, 'result': result,
            'content_hash': 'sha256:' + hashlib.sha256(producer._canonical(updated)).hexdigest()}, guard)
        producer._publish(artifact_id=plan['id'], schema='production_plan.v2', revision=new_revision,
            payload=updated, upstream_refs=plan_refs, created_by=plan['created_by'], guard=guard)
        return result


def prepare_source_observation_adoption(store, ctx, canvas: dict, *, canvas_id: str,
                                       plan_ref: dict, take_id: str, observation_ref: dict,
                                       work_ref: dict, correction_ref: dict | None = None) -> dict:
    """Resolve adoption exclusively from current authenticated host state."""
    from pathlib import Path
    from .source_speech import candidate_scope
    from .material_execution import canvas_material_resource, validate_material_native_execution
    scope = candidate_scope(ctx, canvas, canvas_id)
    if (Path(ctx.output_dir).resolve() != store.project_dir or ctx.project_id != store.project_id
            or ctx.requester_user_id != store.created_by):
        raise ValueError('adoption store differs from host project context')
    node = next((n for n in canvas.get('nodes', []) if n.get('type') == 'scriptNode'
        and (n.get('data', {}).get('productionPlanV2Ref') or {}).get('artifactId') == plan_ref.get('artifact_id')), None)
    if (not node or node['data']['productionPlanV2Ref'].get('revision') != plan_ref.get('revision')
            or node['data'].get('productionPlanV2Dirty') or node['data'].get('productionPlanV2Conflict')):
        raise ValueError('adoption requires the current saved canvas plan')
    context = _context(store, plan_ref=plan_ref, take_id=take_id, observation_ref=observation_ref,
        work_ref=work_ref, scope=scope, requester_user_id=ctx.requester_user_id,
        correction_ref=correction_ref, require_current=True)
    _, take, resource, _, _, _, _ = context
    if canvas_material_resource(store, canvas, resource['node_id'], resource['media_kind']) != resource:
        raise ValueError('adopted source differs from current canvas material')
    work = store.read(work_ref['artifact_id'], work_ref['revision'])
    step_ids = {r['final_step_id'] for r in work['payload']['requirements'] if take_id in r.get('take_ids', [])
                or take['origin_placement_id'] in r.get('placement_ids', [])}
    for step in work['payload']['steps']:
        if step['id'] in step_ids and step['source'] == 'generate':
            validate_material_native_execution(store, canvas, step, resource['node_id'], require_complete=True,
                workflow_state_dir=ctx.state_dir, work_ref=work_ref)
    payload = {'canvas_id': canvas_id, 'plan_ref': plan_ref, 'take_id': take_id,
        'observation_ref': observation_ref, 'work_ref': work_ref, 'scope': scope,
        'requester_user_id': ctx.requester_user_id,
        **({'correction_ref': correction_ref} if correction_ref else {})}
    return {'input_refs': [plan_ref, observation_ref, work_ref] + ([correction_ref] if correction_ref else []),
        'normalized_parameters': {'action': 'adopt_source_observation', 'payload': payload},
        'task_payload': payload, 'task_type': 'remix_adopt_source_speech', 'queue_kind': 'default',
        'producer_version': PRODUCER, 'scope': '', 'input_media_hashes': ['sha256:' + resource['media_hash']]}


def adoption_source_signature(plan: dict, take_id: str) -> str:
    from .store import _speech_source_signature
    signature = _speech_source_signature(plan, take_id)
    if signature is None:
        raise ValueError('saved adoption source required')
    return hashlib.sha256(RemixArtifactStore._canonical(signature)).hexdigest()


def read_source_adoption_result(store, *, scope: dict, requester_user_id: str, command_id: str,
                                 request: dict) -> dict | None:
    """Recover a committed adoption after native notification interruption."""
    directory = store.root / '_source_speech_adoptions'
    if directory.is_symlink():
        raise ValueError('invalid adoption receipt directory')
    key = hashlib.sha256(store._canonical([scope, requester_user_id, command_id])).hexdigest()
    path = directory / (key + '.json')
    if not path.is_file():
        return None
    with store._locked():
        receipt = store._read_json(path)
        if receipt['request'] != request:
            raise ValueError('adoption recovery inputs changed')
        result = receipt['result']
        try:
            saved = store._read(result['plan_ref']['artifact_id'], result['plan_ref']['revision'])
        except FileNotFoundError:
            return None
        if saved['content_hash'] != receipt['content_hash']:
            raise ValueError('adoption result was replaced')
        return result


def _has_adoption_intent(store, *, request, scope, actor, command_id):
    """Require saved native intent or a deterministic trusted partial timeline."""
    directory = store.root / '_source_speech_adoptions'
    if directory.is_symlink():
        raise ValueError('invalid adoption receipt directory')
    key = hashlib.sha256(store._canonical([scope, actor, command_id])).hexdigest()
    path = directory / (key + '.json')
    with store._locked():
        if path.is_file():
            receipt = store._read_json(path)
            if receipt['request'] != request:
                raise ValueError('adoption recovery inputs changed')
            timelines = receipt['result'].get('timeline_refs') or []
        else:
            # Publication of the first timeline proves the actual native worker
            # began this exact operation. Rebuild the remaining pure derivation
            # only from its immutable observation; no task or ASR is submitted.
            plan = store._read(request['plan_ref']['artifact_id'], request['plan_ref']['revision'])
            take = next((t for t in plan['payload'].get('speech_takes', []) if t['id'] == request['take_id']), None)
            if take is None:
                return False
            timelines = []
            for turn_id in take['turn_ids']:
                identity = hashlib.sha256(store._canonical([key, request, turn_id])).hexdigest()
                if store._path(identity, 1, '.json').is_file():
                    timelines.append({'artifact_id': identity, 'revision': 1})
        if not timelines:
            return False
        for ref in timelines:
            artifact = store._read(ref['artifact_id'], ref['revision'])
            payload = artifact['payload']
            if (artifact['schema'] != 'word_timeline.v1' or artifact['producer_version'] != PRODUCER
                    or artifact['created_by'] != actor or payload.get('plan_ref') != request['plan_ref']
                    or payload.get('observation_ref') != request['observation_ref']
                    or payload.get('take_id') != request['take_id'] or payload.get('authorization_scope') != scope):
                raise ValueError('adoption recovery requires trusted saved timeline intent')
        return True


def reconcile_source_speech_adoption(ctx, operation: dict) -> dict | None:
    """Finish saved native intent/result without submitting another task or ASR."""
    from pathlib import Path
    from novelvideo.freezone import canvas_store
    from .source_speech import candidate_scope
    from .operations import RemixOperationStore
    if (operation.get('producer_version') != PRODUCER or not operation.get('task_id')
            or operation.get('status') not in {'pending', 'pending_reconciliation'} or operation.get('cancel_reason')):
        return None
    expected = operation.get('normalized_parameters', {}).get('payload') or {}
    operations = RemixOperationStore(Path(ctx.output_dir), ctx.project_id)
    canvas_id = expected.get('canvas_id', '')
    with canvas_store.get_canvas_write_mutex().write_mutex(ctx.state_dir, canvas_id) as canvas_guard:
        canvas = canvas_store.read_canvas(Path(ctx.state_dir), canvas_id)
        scope = candidate_scope(ctx, canvas, canvas_id)
        refs = [expected.get('plan_ref'), expected.get('observation_ref'), expected.get('work_ref')]
        if expected.get('correction_ref'):
            refs.append(expected['correction_ref'])
        if (scope != expected.get('scope') or ctx.requester_user_id != expected.get('requester_user_id')
                or operation['normalized_parameters'].get('action') != 'adopt_source_observation'
                or operation['input_refs'] != [{**r, 'project_id': ctx.project_id} for r in refs]):
            raise ValueError('source adoption recovery ownership changed')
        store = RemixArtifactStore(Path(ctx.output_dir), ctx.project_id, created_by=ctx.requester_user_id)
        request = {k: expected.get(k) for k in ('plan_ref', 'take_id', 'observation_ref', 'work_ref',
            'scope', 'requester_user_id', 'correction_ref')}

        class RecoveryGuard:
            def reassert(self):
                canvas_guard.reassert()
                from .production_automation import assert_operation_automation_current
                assert_operation_automation_current(ctx, operation['operation_id'])
                # Avoid the inverse of result publication's operation ->
                # artifact lock order; the operation snapshot is atomic.
                current = operations._read(operation['operation_id'])
                if (current['status'] not in {'pending', 'pending_reconciliation'} or current.get('cancel_reason')
                        or current.get('task_id') != operation['task_id']):
                    raise ValueError('source adoption recovery is no longer active')

        recovery_guard = RecoveryGuard()
        canvas_guard.reassert()
        result = read_source_adoption_result(store, scope=scope, requester_user_id=ctx.requester_user_id,
            command_id=operation['operation_id'], request=request)
        if result is None:
            recovery_guard.reassert()
            if not _has_adoption_intent(store, request=request, scope=scope, actor=ctx.requester_user_id,
                                        command_id=operation['operation_id']):
                return None
            prepare_source_observation_adoption(store, ctx, canvas, canvas_id=canvas_id,
                plan_ref=expected['plan_ref'], take_id=expected['take_id'], observation_ref=expected['observation_ref'],
                work_ref=expected['work_ref'], correction_ref=expected.get('correction_ref'))
            result = adopt_source_observation(store, **request, command_id=operation['operation_id'],
                _canvas_guard=recovery_guard)
        canvas_guard.reassert()
        return operations.publish_result_unverified(operation['operation_id'],
            output_refs=[result['plan_ref'], *result['timeline_refs']])
