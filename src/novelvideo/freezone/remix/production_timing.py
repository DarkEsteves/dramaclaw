"""Pure source-to-program projection. A playback instance occupies time once."""
from fractions import Fraction
from typing import Any


def project_speech_mainline(plan: Any) -> dict[str, Any]:
    from .production_plan_v2 import _speech_take

    fps = Fraction(plan.output.fps_num, plan.output.fps_den)
    resources = {row.id: row for row in plan.resources}
    placements = {row.id: row for row in plan.placements}
    edges, beats, takes, cues, ranges, issues = {}, {}, [], [], {}, []
    takes_by_beat: dict[str, list[Any]] = {}
    cues_by_take: dict[str, list[Any]] = {}
    for take in plan.speech_takes:
        takes_by_beat.setdefault(take.beat_id, []).append(take)
    for cue in plan.speech_cues:
        cues_by_take.setdefault(cue.take_id, []).append(cue)
    cursor = 0
    owners: dict[str, str] = {}
    for beat in plan.beats:
        start = cursor
        local = 0
        originals = {turn.id: (turn.start_frame if turn.start_frame is not None else beat.duration_frames * i // len(beat.turns),
                              turn.end_frame if turn.end_frame is not None else beat.duration_frames * (i + 1) // len(beat.turns))
                     for i, turn in enumerate(beat.turns)}
        explicit = takes_by_beat.get(beat.id, [])
        original_cursor = 0
        assigned: dict[str, tuple[int, int]] = {}
        if explicit:
            for take in explicit:
                resource = resources[take.resource_id]
                out = take.source_out_us or resource.duration_us
                frames = round(Fraction(out - take.source_in_us, 1_000_000) * fps * take.speed_den / take.speed_num)
                local += take.lead_in_frames
                a, b = start + local, start + local + frames
                local += frames
                edges[("take", take.id, "start")] = a
                edges[("take", take.id, "end")] = b
                takes.append({**take.model_dump(mode="json"), "start_frame": a, "end_frame": b, "source_out_us": out})
                ranges[take.origin_placement_id] = (a, b, out)
                for turn_id in take.turn_ids:
                    assigned[turn_id] = (a, b)
                for cue in cues_by_take.get(take.id, []):
                    ca = cb = None
                    if cue.timing_status == "ready" and cue.source_start_us is not None and cue.source_end_us is not None:
                        ca = a + round(Fraction(cue.source_start_us - take.source_in_us, 1_000_000) * fps * take.speed_den / take.speed_num)
                        cb = a + round(Fraction(cue.source_end_us - take.source_in_us, 1_000_000) * fps * take.speed_den / take.speed_num)
                        if not a <= ca < cb <= b:
                            ca = cb = None
                    if ca is not None:
                        edges[("cue", cue.id, "start")] = ca
                        edges[("cue", cue.id, "end")] = cb
                    cues.append({**cue.model_dump(mode="json"), "start_frame": ca, "end_frame": cb})
                    if ca is None:
                        issues.append({"take_id": take.id, "cue_id": cue.id, "code": "unresolved_speech_timing", "message": f"Speech cue {cue.id} needs accepted timing"})
                for turn_id in take.turn_ids:
                    if not any(cue.take_id == take.id and cue.turn_id == turn_id for cue in cues_by_take.get(take.id, [])):
                        issues.append({"take_id": take.id, "turn_id": turn_id, "code": "unresolved_speech_timing", "message": f"Speech turn {turn_id} needs an accepted cue"})
                for turn in beat.turns:
                    if turn.id not in take.turn_ids or turn.speech_timing is None:
                        continue
                    timing = turn.speech_timing
                    if timing.text != turn.text or timing.media_hash != resource.media_hash or timing.placement_id != take.origin_placement_id:
                        continue
                    accepted_words = {word_id for cue in cues_by_take.get(take.id, []) if cue.turn_id == turn.id and cue.timing_status == "ready" for word_id in cue.word_ids}
                    for word in timing.words:
                        if word.id not in accepted_words:
                            continue
                        if take.source_in_us <= word.start_us < word.end_us <= out:
                            edges[("word", word.id, "start")] = a + round(Fraction(word.start_us - take.source_in_us, 1_000_000) * fps * take.speed_den / take.speed_num)
                            edges[("word", word.id, "end")] = a + round(Fraction(word.end_us - take.source_in_us, 1_000_000) * fps * take.speed_den / take.speed_num)
            for turn in beat.turns:
                if turn.id not in assigned and beat.timing_mode != "fixed":
                    issues.append({"beat_id": beat.id, "turn_id": turn.id, "code": "unresolved_speech_timing", "message": f"Speech turn {turn.id} needs an assigned take"})
                own = [cue for cue in cues if cue["turn_id"] == turn.id and cue["start_frame"] is not None]
                if own:
                    assigned[turn.id] = (min(cue["start_frame"] for cue in own), max(cue["end_frame"] for cue in own))
        else:
            candidates = {turn.id: _speech_take(beat, turn, placements, resources, fps) for turn in beat.turns}
            reflow = any(candidates.values())
            seen: dict[str, tuple[int, int]] = {}
            last_source = None
            for turn in beat.turns:
                os, oe = originals[turn.id]
                take = candidates.get(turn.id)
                if take:
                    pid = take[0].id
                    if pid in owners and owners[pid] != beat.id:
                        issues.append({"placement_id": pid, "code": "ambiguous_speech_instance", "message": "Speech placement is shared across beats; assign explicit takes"})
                    owners[pid] = beat.id
                    if pid in seen:
                        if last_source != pid:
                            issues.append({"placement_id": pid, "code": "ambiguous_speech_instance", "message": "Interleaved speech placement needs explicit playback instances"})
                        assigned[turn.id] = seen[pid]
                        original_cursor = oe
                        continue
                    lead = max(0, take[3] - os) if take[0].start.kind == "beat" else take[3]
                    local += max(0, os - original_cursor) + lead
                    a, b = start + local, start + local + take[2]
                    local += take[2]
                    assigned[turn.id] = seen[pid] = (a, b)
                    ranges[pid] = (a, b, take[1])
                    last_source = pid
                else:
                    if reflow:
                        local += max(0, os - original_cursor)
                        assigned[turn.id] = (start + local, start + local + oe - os)
                        local += oe - os
                    else:
                        assigned[turn.id] = (start + os, start + oe)
                original_cursor = oe
            local = local + max(0, beat.duration_frames - original_cursor) if reflow else beat.duration_frames
        if beat.duration_source_placement_id is not None:
            placement = placements[beat.duration_source_placement_id]
            resource = resources.get(placement.resource_id)
            if resource is None or resource.duration_us == 0:
                issues.append({"beat_id": beat.id, "placement_id": placement.id,
                               "code": "unresolved_media_duration",
                               "message": f"Beat {beat.id} duration source needs bound media"})
            else:
                source_out = placement.source_out_us or resource.duration_us
                local = round(Fraction(source_out - placement.source_in_us, 1_000_000)
                              * fps * placement.speed_den / placement.speed_num)
                if local < 1:
                    issues.append({"beat_id": beat.id, "placement_id": placement.id,
                                   "code": "unresolved_media_duration",
                                   "message": f"Beat {beat.id} duration source is shorter than one frame"})
                    local = beat.duration_frames
        if beat.timing_mode == "fixed":
            if explicit and local > beat.duration_frames:
                issues.append({"beat_id": beat.id, "code": "speech_exceeds_fixed_beat", "message": "Speech takes exceed fixed beat duration"})
            local = beat.duration_frames
        for turn in beat.turns:
            a, b = assigned.get(turn.id, (start + originals[turn.id][0], start + originals[turn.id][1]))
            edges[("turn", turn.id, "start")] = a
            edges[("turn", turn.id, "end")] = b
            for word in ([] if explicit else turn.words):
                edges[("word", word.id, "start")] = a + word.start_frame - originals[turn.id][0]
                edges[("word", word.id, "end")] = a + word.end_frame - originals[turn.id][0]
        cursor = start + local
        edges[("beat", beat.id, "start")] = start
        edges[("beat", beat.id, "end")] = cursor
        beats[beat.id] = (start, cursor)
    word_windows = [{"id": key[1], "start_frame": value, "end_frame": edges[("word", key[1], "end")]} for key, value in edges.items() if key[0] == "word" and key[2] == "start"]
    return {"word_windows": word_windows, "edges": edges, "beat_ranges": beats, "speech_take_ranges": ranges, "speech_takes": takes,
            "speech_cues": cues, "duration_frames": cursor, "issues": issues}
