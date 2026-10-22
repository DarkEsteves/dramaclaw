"""Interpret confirmed direction consistently for drafting and plan review.

This is a read-only view. Saved direction artifacts and their fingerprints never
change when older direction shapes are read.
"""
from __future__ import annotations

import hashlib
from typing import Any


def resolve_direction_review_contract(direction: dict[str, Any]) -> dict[str, Any]:
    from .production_requirements import normalize_requirements

    if not isinstance(direction, dict):
        raise ValueError('direction must be an object')
    goal = direction.get('target_goal', direction.get('goal'))
    if goal is not None and (not isinstance(goal, str) or not goal.strip()):
        raise ValueError('direction.goal must be nonblank text')

    raw_preserve = direction.get('preserve', [])
    exact_turns: dict[str, str] = {}
    statements: list[str] = []
    if isinstance(raw_preserve, list):
        if any(not isinstance(row, str) or not row.strip() for row in raw_preserve):
            raise ValueError('direction.preserve requires nonblank statements')
        statements = list(raw_preserve)
    elif isinstance(raw_preserve, dict):
        if set(raw_preserve) != {'turns'} or not isinstance(raw_preserve['turns'], dict):
            raise ValueError('direction.preserve requires a turns mapping')
        exact_turns = raw_preserve['turns']
        if any(not isinstance(key, str) or not key.strip()
               or not isinstance(value, str) or not value.strip()
               for key, value in exact_turns.items()):
            raise ValueError('direction.preserve.turns requires nonblank text')
    else:
        raise ValueError('direction.preserve must be statements or a turns mapping')

    raw_requirements = direction.get('requirements', {})
    if not isinstance(raw_requirements, dict):
        raise ValueError('direction.requirements must be an object')
    requirements = dict(raw_requirements)
    nested_output = requirements.get('output_requirements')
    if nested_output is not None and not isinstance(nested_output, dict):
        raise ValueError('direction.requirements.output_requirements must be an object')
    output = direction.get('output_requirements')
    if output is not None:
        if not isinstance(output, dict):
            raise ValueError('direction.output_requirements must be an object')
        combined = dict(nested_output or {})
        for key, value in output.items():
            if key in combined and combined[key] != value:
                raise ValueError(f'direction.output_requirements.{key} conflicts with requirements')
            combined[key] = value
        requirements['output_requirements'] = combined

    semantic = list(requirements.get('semantic') or [])
    by_id = {row.get('id'): row.get('text') for row in semantic if isinstance(row, dict)}
    choices = direction.get('preserve_replace', [])
    if choices is None:
        choices = []
    if isinstance(choices, dict):
        if set(choices) - {'preserve', 'replace'}:
            raise ValueError('direction.preserve_replace contains unknown choices')
        for action in ('preserve', 'replace'):
            values = choices.get(action, [])
            if not isinstance(values, list):
                raise ValueError(f'direction.preserve_replace.{action} must be a list')
            statements.extend(values)
        choices = []
    if not isinstance(choices, list):
        raise ValueError('direction.preserve_replace must be a list')
    for statement in statements:
        if not isinstance(statement, str) or not statement.strip():
            raise ValueError('direction.preserve_replace requires nonblank statements')
        if statement in (row.get('text') for row in semantic if isinstance(row, dict)):
            continue
        identity = 'direction-preserve-' + hashlib.sha256(statement.encode('utf-8')).hexdigest()[:16]
        if identity in by_id and by_id[identity] != statement:
            raise ValueError('direction.preserve requirement identity collision')
        semantic.append({'id': identity, 'text': statement})
        by_id[identity] = statement
    choice_ids: set[str] = set()
    for choice in choices:
        if not isinstance(choice, dict):
            raise ValueError('direction.preserve_replace requires structured choices')
        if (choice.get('action') not in {'preserve', 'replace'}
                or not isinstance(choice.get('entity_kind'), str) or not choice['entity_kind'].strip()
                or not isinstance(choice.get('entity_id'), str) or not choice['entity_id'].strip()):
            raise ValueError('direction.preserve_replace requires an action and concrete entity')
        if choice['entity_kind'] == 'turn' and 'value' in choice:
            continue
        identity = choice.get('id')
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError('direction.preserve_replace requires a stable choice id')
        if identity in choice_ids:
            raise ValueError('direction.preserve_replace choice ids must be unique')
        choice_ids.add(identity)
        text = f"{choice['action']} {choice['entity_kind']}: {choice['entity_id']}"
        if identity in by_id and by_id[identity] != text:
            raise ValueError('direction.preserve_replace requirement identity collision')
        if identity not in by_id:
            semantic.append({'id': identity, 'text': text})
            by_id[identity] = text
    if semantic:
        requirements['semantic'] = semantic
    return {
        'goal': goal,
        'requirements': normalize_requirements(requirements),
        'exact_turns': dict(exact_turns),
        'preserve_replace': list(choices),
    }
