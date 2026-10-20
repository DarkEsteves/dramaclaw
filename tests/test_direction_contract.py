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


def test_invalid_nested_output_requirements_has_a_validation_error():
    with pytest.raises(ValueError, match='output_requirements'):
        resolve_direction_review_contract({
            'requirements': {'output_requirements': 17},
            'output_requirements': {'width': 1080},
        })
