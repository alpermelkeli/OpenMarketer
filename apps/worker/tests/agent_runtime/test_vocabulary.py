"""Tests for the plain data the workflow and the activities share: limits and texts.

A limit that would make Temporal retry for ever is refused when it is made, and the types that
cross between the workflow and the activities are shown to survive the data converter that
carries them, with annotations that are types and not text (the sandbox's converter needs that).
"""

import dataclasses
import uuid

import pytest
from temporalio.converter import DataConverter

from openmarketer_core.agent_runtime.actions import ActionOutcome, ActionRequest
from openmarketer_core.agent_runtime.steps import GraphRunRef, StepOutcome
from openmarketer_worker.agent_runtime.vocabulary import (
    ActionPolicy,
    DatabaseWritePolicy,
    FailureTexts,
    GraphRunPolicy,
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
@pytest.mark.parametrize(
    "limits",
    [
        {"attempt_timeout_seconds": 0},
        {"max_attempts": 0},  # Temporal reads 0 attempts as no limit, which an action must not have
        {"retry_after_seconds": 0},
        {"heartbeat_timeout_seconds": 0},
        {"heartbeat_timeout_seconds": -1},
    ],
)
def test_action_policy_with_a_limit_that_is_not_positive_is_refused(limits):
    given = {"attempt_timeout_seconds": 30, "max_attempts": 2, "retry_after_seconds": 1} | limits
    with pytest.raises(ValueError, match=next(iter(limits))):
        ActionPolicy(**given)


@pytest.mark.parametrize(
    "limit",
    [
        "attempt_timeout_seconds",
        "heartbeat_timeout_seconds",
        "max_attempts",
        "retry_after_seconds",
        "max_actions",
    ],
)
def test_graph_run_policy_with_a_limit_that_is_not_positive_is_refused(limit):
    given = {
        "attempt_timeout_seconds": 30,
        "heartbeat_timeout_seconds": 60,
        "max_attempts": 5,
        "retry_after_seconds": 60,
        "max_actions": 200,
    } | {limit: 0}
    with pytest.raises(ValueError, match=limit):
        GraphRunPolicy(**given)


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


def test_policies_survive_the_converter_with_and_without_a_heartbeat_timeout():
    assert converted(ActionPolicy(30, 2, 0.5)) == ActionPolicy(30, 2, 0.5)
    watched = ActionPolicy(30, 2, 0.5, heartbeat_timeout_seconds=9)
    assert converted(watched) == watched
    assert converted(GraphRunPolicy(30, 60, 5, 60, max_actions=3)) == GraphRunPolicy(
        30, 60, 5, 60, max_actions=3
    )


@pytest.mark.parametrize(
    "decoded",
    [StepInput, ActionPolicy, GraphRunPolicy, DatabaseWritePolicy, FailureTexts],
    ids=lambda c: c.__name__,
)
def test_type_the_workflow_carries_has_annotations_that_are_types_not_text(decoded):
    # The sandbox's data converter evaluates text annotations in the namespace of the class's
    # module, which may not be the module the sandbox has loaded: the run then hangs.
    assert not any(isinstance(field.type, str) for field in dataclasses.fields(decoded))
