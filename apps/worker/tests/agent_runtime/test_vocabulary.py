"""Tests for the plain data the workflow and the activities share: limits, texts and failure types.

A limit that would make Temporal retry for ever is refused when it is made, and the types that
cross between the workflow and the activities are shown to survive the data converter that
carries them, with annotations that are types and not text (the sandbox's converter needs that).
Core's ``ActionPolicy``, which is never a workflow's input, is checked in core's tests.
"""

import dataclasses
import uuid
from typing import Any

import pytest
from temporalio.converter import DataConverter

from openmarketer_core.agent_runtime.actions import ActionOutcome, ActionRequest
from openmarketer_core.agent_runtime.gate import RunScope
from openmarketer_core.agent_runtime.steps import GraphRunRef, StepOutcome
from openmarketer_worker.agent_runtime.vocabulary import (
    RUN_ENDING_ACTION_FAILURES,
    WORDED_BY_THE_RUNTIME,
    DatabaseWritePolicy,
    FailureTexts,
    GraphRunPolicy,
    PreparedRun,
    PrepareRunInput,
    StepInput,
)

RUN = GraphRunRef(kind="scripted-agent", run_id=uuid.uuid4())
OUTCOME = ActionOutcome("scripted-agent:1:2", succeeded=True, result={"id": 7}, error="")


def converted[T](value: T) -> T:
    """``value`` as the default data converter carries it: encoded, then decoded as its own type."""
    converter = DataConverter.default.payload_converter
    (payload,) = converter.to_payloads([value])
    (decoded,) = converter.from_payloads([payload], [type(value)])
    return decoded


# ------------------------------------------------------------------ policies
GRAPH_RUN_LIMITS: dict[str, Any] = {
    "attempt_timeout_seconds": 30,
    "heartbeat_timeout_seconds": 60,
    "max_attempts": 5,
    "retry_after_seconds": 60,
    "max_actions": 200,
}
DATABASE_WRITE_LIMITS: dict[str, Any] = {
    "attempt_timeout_seconds": 30,
    "retry_after_seconds": 1,
    "max_retry_wait_seconds": 60,
    "max_attempts": 5,
    "give_up_after_seconds": 3600,
}
# What Temporal cannot make a duration or a count of: it fails the workflow task, again and again.
NOT_A_LIMIT = [0, -1, float("nan"), float("inf"), True]


def with_limit(limits: dict[str, Any], name: str, value: Any) -> dict[str, Any]:
    """``limits`` with the limit ``name`` set to ``value``."""
    return {**limits, name: value}


@pytest.mark.parametrize("limit", list(GRAPH_RUN_LIMITS))
@pytest.mark.parametrize("value", NOT_A_LIMIT, ids=repr)
def test_graph_run_policy_with_a_limit_that_is_not_a_positive_number_is_refused(limit, value):
    with pytest.raises(ValueError, match=limit):
        GraphRunPolicy(**with_limit(GRAPH_RUN_LIMITS, limit, value))


@pytest.mark.parametrize("count", ["max_attempts", "max_actions"])
def test_graph_run_policy_with_a_count_that_is_not_whole_is_refused(count):
    with pytest.raises(ValueError, match=f"{count} must be a whole number"):
        GraphRunPolicy(**with_limit(GRAPH_RUN_LIMITS, count, 2.5))


@pytest.mark.parametrize("limit", list(DATABASE_WRITE_LIMITS))
@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True], ids=repr)
def test_database_write_policy_with_a_limit_that_is_not_a_positive_number_is_refused(limit, value):
    with pytest.raises(ValueError, match=limit):
        DatabaseWritePolicy(**with_limit(DATABASE_WRITE_LIMITS, limit, value))


@pytest.mark.parametrize("limit", ["attempt_timeout_seconds", "retry_after_seconds"])
def test_database_write_policy_with_a_wait_that_is_zero_is_refused(limit):
    with pytest.raises(ValueError, match=limit):
        DatabaseWritePolicy(**with_limit(DATABASE_WRITE_LIMITS, limit, 0))


def test_database_write_policy_with_no_limit_on_attempts_and_none_on_time_is_made():
    unlimited = with_limit(
        with_limit(DATABASE_WRITE_LIMITS, "max_attempts", 0), "give_up_after_seconds", None
    )
    assert DatabaseWritePolicy(**unlimited).max_attempts == 0


# ------------------------------------------------------------------ failure types
def test_every_failure_that_ends_a_run_has_a_message_that_is_shown():
    assert set(RUN_ENDING_ACTION_FAILURES) <= set(WORDED_BY_THE_RUNTIME)


# ------------------------------------------------------------------ texts
@pytest.mark.parametrize("name", [field.name for field in dataclasses.fields(FailureTexts)])
def test_text_that_says_nothing_is_refused_because_a_failed_outcome_must_say_what_went_wrong(name):
    words = {field.name: "x" for field in dataclasses.fields(FailureTexts)} | {name: ""}
    with pytest.raises(ValueError, match=name):
        FailureTexts(**words)


# ------------------------------------------------------------------ the data converter
def test_first_step_input_survives_the_converter_with_no_outcome():
    assert converted(StepInput(RUN, None)) == StepInput(RUN, None)


def test_step_input_with_the_outcome_of_an_action_survives_the_converter():
    assert converted(StepInput(RUN, OUTCOME)) == StepInput(RUN, OUTCOME)


def test_step_that_finished_survives_the_converter():
    finished = StepOutcome.finished({"answers": [{"ok": True}], "nothing": None})
    assert converted(finished) == finished


def test_step_that_waits_for_an_action_survives_the_converter():
    waiting = StepOutcome.waiting_for(ActionRequest("scripted-agent:1:2", "publish", {"post": 7}))
    assert converted(waiting) == waiting


def test_outcome_of_a_failed_action_survives_the_converter():
    failed = ActionOutcome("scripted-agent:1:2", succeeded=False, result={}, error="it failed")
    assert converted(failed) == failed


def test_prepare_input_survives_the_converter():
    prepared = PrepareRunInput(
        scope=RunScope(workspace_id=uuid.uuid4(), project_id=uuid.uuid4()),
        request=ActionRequest("scripted-agent:1:2", "research", {"question": "why?"}),
    )
    assert converted(prepared) == prepared


def test_prepared_run_survives_the_converter():
    made = PreparedRun(run_id=uuid.uuid4())
    assert converted(made) == made


@pytest.mark.parametrize(
    "decoded",
    [StepInput, PrepareRunInput, PreparedRun, GraphRunPolicy, DatabaseWritePolicy, FailureTexts],
    ids=lambda c: c.__name__,
)
def test_type_the_workflow_carries_has_annotations_that_are_types_not_text(decoded):
    # The sandbox's data converter evaluates text annotations in the namespace of the class's
    # module, which may not be the module the sandbox has loaded: the run then hangs.
    assert not any(isinstance(field.type, str) for field in dataclasses.fields(decoded))
