"""A verified retry may settle only the same scoped operation."""

from novelvideo.chat.operation_outcome import (
    logical_operation_key, recovered_attempt_ids, reduce_operation_outcomes,
)


def test_retry_key_ignores_transport_id_but_keeps_business_target():
    base = {"commission_ref": {"artifact_id": "commission-a", "revision": 3},
            "kind": "direction", "proposal": {"product": "tea"}}
    first = logical_operation_key("freezone_prepare_reference_decision",
                                  {**base, "command_id": "first"},
                                  project="p", canvas="c", agent="main")
    second = logical_operation_key("freezone_prepare_reference_decision",
                                   {**base, "command_id": "second"},
                                   project="p", canvas="c", agent="main")
    assert first == second
    assert first != logical_operation_key("freezone_prepare_reference_decision",
                                          {**base, "proposal": {"product": "water"}},
                                          project="p", canvas="c", agent="main")
    assert first != logical_operation_key("freezone_prepare_reference_decision",
                                          {**base, "commission_ref": {"artifact_id": "commission-b", "revision": 3}},
                                          project="p", canvas="c", agent="main")


def test_recovery_requires_a_key_and_only_clears_prior_matching_attempts():
    key = "one"
    result = recovered_attempt_ids(successful_call_id="latest", successful_key=key,
                                   attempt_states={"older": "failed", "other": "failed",
                                                   "latest": "succeeded", "after": "failed"},
                                   attempt_keys={"older": key, "other": "two", "latest": key, "after": key})
    assert result == ["older"]
    assert recovered_attempt_ids(successful_call_id="latest", successful_key=None,
                                 attempt_states={"older": "failed", "latest": "succeeded"},
                                 attempt_keys={"older": key, "latest": None}) == []


def test_verified_retry_recovers_only_same_scoped_operation_and_current_revision():
    common = {"scope_key": "p/c/u", "target_key": "plan-a", "logical_operation_id": "draft-a"}
    attempts = [
        {**common, "attempt_id": "a", "input_revision": 3, "status": "failed",
         "side_effects": "none", "verified_receipt": False, "error": "missing_duration"},
        {**common, "attempt_id": "b", "input_revision": 3, "status": "completed",
         "retry_of": "a", "verified_receipt": True, "task_id": "task-a"},
        {**common, "attempt_id": "c", "logical_operation_id": "material-b",
         "input_revision": 4, "status": "failed", "verified_receipt": False,
         "error": "invalid_media_kind"},
    ]
    result = reduce_operation_outcomes(attempts)
    assert result["status"] == "partial"
    assert result["recovered_attempt_ids"] == ["a"]
    assert [row["attempt_id"] for row in result["blocking_errors"]] == ["c"]


def test_success_without_verified_receipt_or_with_older_revision_cannot_clear_failure():
    common = {"scope_key": "p/c/u", "target_key": "work-a", "logical_operation_id": "material-a"}
    attempts = [
        {**common, "attempt_id": "a", "input_revision": 4, "status": "failed",
         "verified_receipt": False, "error": "invalid_reference"},
        {**common, "attempt_id": "b", "input_revision": 4, "status": "completed",
         "verified_receipt": False},
        {**common, "attempt_id": "c", "input_revision": 3, "status": "completed",
         "verified_receipt": True},
    ]
    result = reduce_operation_outcomes(attempts)
    assert result["recovered_attempt_ids"] == []
    assert [row["attempt_id"] for row in result["blocking_errors"]] == ["a"]


def test_incremental_retry_reducer_keeps_newer_revision_failure():
    assert recovered_attempt_ids(
        successful_call_id="success", successful_key="same-target",
        attempt_states={"failure": "failed", "success": "succeeded"},
        attempt_keys={"failure": "same-target", "success": "same-target"},
        successful_revision=3, attempt_revisions={"failure": 4, "success": 3},
    ) == []


def test_stage_advance_retry_identifies_the_same_event_without_merging_other_events():
    common = {"commission_ref": {"artifact_id": "commission-a", "revision": 4},
              "event": {"type": "materials_ready",
                        "work_ref": {"artifact_id": "work-a", "revision": 2}}}
    first = logical_operation_key("freezone_advance_reference_production", common,
                                  project="p", canvas="c", agent="main")
    retry = logical_operation_key("freezone_advance_reference_production",
                                  {**common, "commission_ref": {"artifact_id": "commission-a",
                                                               "revision": 5}},
                                  project="p", canvas="c", agent="main")
    other = logical_operation_key("freezone_advance_reference_production",
                                  {**common, "event": {"type": "materials_ready",
                                                       "work_ref": {"artifact_id": "work-b",
                                                                    "revision": 2}}},
                                  project="p", canvas="c", agent="main")
    assert first and first == retry and first != other
def test_review_completion_recovers_only_the_same_plan_precondition():
    from novelvideo.chat.operation_outcome import recovered_plan_precondition_attempts

    scope = {'project_id': 'p', 'canvas_id': 'c', 'requester_user_id': 'u',
             'commission_id': 'commission',
             'plan_ref': {'artifact_id': 'plan', 'revision': 1, 'project_id': 'p'},
             'direction_revision': 1}
    attempts = [
        {'call_id': 'wrong-plan', 'tool': 'freezone_prepare_reference_decision',
         'status': 'failed', 'blocker': {**scope, 'code': 'plan_review_required',
                                      'plan_ref': {**scope['plan_ref'], 'revision': 2}}},
        {'call_id': 'early', 'tool': 'freezone_prepare_reference_decision',
         'status': 'failed', 'blocker': {**scope, 'code': 'plan_review_required'},
         'proposal': {'audit_notes': 'early audit'}},
        {'call_id': 'other', 'tool': 'freezone_prepare_reference_decision',
         'status': 'failed', 'blocker': {**scope, 'code': 'permission_denied'}},
    ]
    settlement = {**scope, 'verified': True, 'review_status': 'passed',
                  'decision_status': 'pending'}
    assert recovered_plan_precondition_attempts(attempts, settlement) == ['early']
    assert recovered_plan_precondition_attempts(attempts, {**settlement, 'verified': False}) == []


def test_production_edit_requires_explicit_link_exact_revision_and_no_side_effects():
    identity = {'scope_key': 'p/c/u/turn', 'logical_operation_id': 'edit-a',
                'target_key': 'plan-a/shot-a', 'input_revision': 3}
    failures = [
        {**identity, 'attempt_id': 'bad-a', 'status': 'failed', 'side_effects': 'none'},
        {**identity, 'attempt_id': 'bad-b', 'status': 'failed', 'side_effects': 'none'},
        {**identity, 'attempt_id': 'unknown', 'status': 'failed', 'side_effects': 'unknown'},
        {**identity, 'attempt_id': 'timeout', 'status': 'timeout', 'side_effects': 'none'},
        {**identity, 'logical_operation_id': 'review-a', 'attempt_id': 'review',
         'status': 'failed', 'side_effects': 'none'},
    ]
    success = {**identity, 'attempt_id': 'good', 'status': 'completed',
               'retry_of': ['bad-a', 'bad-b', 'unknown', 'timeout', 'review'],
               'verified_receipt': True}
    result = reduce_operation_outcomes([*failures, success])
    assert result['recovered_attempt_ids'] == ['bad-a', 'bad-b']
    assert [row['attempt_id'] for row in result['blocking_errors']] == ['unknown', 'timeout', 'review']
    assert result['status'] == 'partial'
    assert reduce_operation_outcomes([failures[0], {**success, 'retry_of': None}])['recovered_attempt_ids'] == []
    assert reduce_operation_outcomes([failures[0], {**success, 'input_revision': 4}])['recovered_attempt_ids'] == []


def test_accepted_retry_does_not_complete_edit_or_recover_failure():
    identity = {'scope_key': 'p/c/u/turn', 'logical_operation_id': 'edit-a',
                'target_key': 'plan-a/shot-a', 'input_revision': 3}
    failed = {**identity, 'attempt_id': 'bad', 'status': 'failed', 'side_effects': 'none'}
    accepted = {**identity, 'attempt_id': 'queued', 'status': 'accepted',
                'retry_of': 'bad', 'verified_receipt': True}
    result = reduce_operation_outcomes([failed, accepted])
    assert result['recovered_attempt_ids'] == []
    assert result['status'] == 'partial'
