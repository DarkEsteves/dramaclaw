from copy import deepcopy

import pytest

from novelvideo.freezone.remix.direction_contract import resolve_direction_review_contract


def test_list_preservation_is_reviewable_without_mutating_confirmed_direction():
    direction = {
        "goal": "Keep the interview format",
        "preserve": ["Keep the opening question", "Keep the closing reveal"],
        "requirements": {"semantic": [{"id": "product", "text": "Show the new product"}]},
    }
    original = deepcopy(direction)

    result = resolve_direction_review_contract(direction)

    assert direction == original
    assert result == resolve_direction_review_contract(direction)
    assert result["exact_turns"] == {}
    assert {row["text"] for row in result["requirements"]["semantic"]} == {
        "Show the new product", "Keep the opening question", "Keep the closing reveal",
    }


def test_exact_dialogue_and_output_constraints_stay_strict():
    result = resolve_direction_review_contract({
        "preserve": {"turns": {"line-1": "Keep this exact line"}},
        "output_requirements": {"width": 1080},
    })
    assert result["exact_turns"] == {"line-1": "Keep this exact line"}
    assert result["requirements"]["output_requirements"] == {"width": 1080}


@pytest.mark.parametrize("value", [17, "keep this", [17], {"turns": []}])
def test_invalid_preserve_reports_its_field(value):
    with pytest.raises(ValueError, match="preserve"):
        resolve_direction_review_contract({"preserve": value})


def test_conflicting_output_constraints_are_rejected():
    with pytest.raises(ValueError, match="output_requirements"):
        resolve_direction_review_contract({
            "output_requirements": {"width": 1080},
            "requirements": {"output_requirements": {"width": 720}},
        })


def test_legacy_preserve_replace_statements_remain_reviewable():
    result = resolve_direction_review_contract({
        'preserve_replace': {'preserve': ['Keep the interview'], 'replace': ['Use a new product']},
    })
    assert {row['text'] for row in result['requirements']['semantic']} == {
        'Keep the interview', 'Use a new product',
    }


def test_legacy_choices_do_not_modify_a_preserve_list():
    direction = {
        'preserve': ['Keep the original opening'],
        'preserve_replace': {'replace': ['Show the new product']},
    }
    original = deepcopy(direction)

    result = resolve_direction_review_contract(direction)

    assert direction == original
    assert {row['text'] for row in result['requirements']['semantic']} == {
        'Keep the original opening', 'Show the new product',
    }


def test_structured_choices_expose_reviewable_ids_without_mutating_direction():
    direction = {'preserve_replace': [
        {'id': 'pr-scene', 'entity_kind': 'scene', 'entity_id': '咖啡馆双人访谈场景',
         'action': 'preserve', 'evidence': '其他保持'},
        {'id': 'pr-product', 'entity_kind': 'product', 'entity_id': '补剂换成黑咖啡',
         'action': 'replace', 'evidence': '产品换成黑咖啡'},
    ]}
    original = deepcopy(direction)
    result = resolve_direction_review_contract(direction)
    assert result['requirements']['semantic'] == [
        {'id': 'pr-scene', 'text': 'preserve scene: 咖啡馆双人访谈场景'},
        {'id': 'pr-product', 'text': 'replace product: 补剂换成黑咖啡'},
    ]
    assert direction == original
    assert result == resolve_direction_review_contract(direction)


def test_literal_choice_stays_an_exact_dialogue_constraint():
    choice = {'entity_kind': 'turn', 'entity_id': 'line', 'action': 'preserve', 'value': '原句'}
    result = resolve_direction_review_contract({'preserve_replace': [choice]})
    assert result['requirements'] == {}
    assert result['preserve_replace'] == [choice]


@pytest.mark.parametrize('choices', [
    [{'entity_kind': 'scene', 'entity_id': 'Cafe', 'action': 'preserve'}],
    [{'id': 'scene', 'entity_kind': 'scene', 'entity_id': ' ', 'action': 'preserve'}],
    [{'id': 'scene', 'entity_kind': 'scene', 'entity_id': 'Cafe', 'action': 'ignore'}],
    ['Keep the cafe'],
])
def test_invalid_structured_choice_is_rejected(choices):
    with pytest.raises(ValueError, match='preserve_replace'):
        resolve_direction_review_contract({'preserve_replace': choices})


def test_duplicate_choice_ids_are_rejected():
    choice = {'id': 'scene', 'entity_kind': 'scene', 'entity_id': 'Cafe', 'action': 'preserve'}
    with pytest.raises(ValueError, match='unique'):
        resolve_direction_review_contract({'preserve_replace': [choice, dict(choice)]})


def test_choice_id_cannot_alias_another_semantic_requirement():
    with pytest.raises(ValueError, match='identity collision'):
        resolve_direction_review_contract({
            'requirements': {'semantic': [{'id': 'scene', 'text': 'Different requirement'}]},
            'preserve_replace': [{'id': 'scene', 'entity_kind': 'scene', 'entity_id': 'Cafe',
                                  'action': 'preserve'}],
        })


def test_invalid_nested_output_requirements_has_a_validation_error():
    with pytest.raises(ValueError, match='output_requirements'):
        resolve_direction_review_contract({
            'requirements': {'output_requirements': 17},
            'output_requirements': {'width': 1080},
        })
