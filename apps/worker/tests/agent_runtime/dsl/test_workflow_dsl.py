"""Tests for what ``agent_workflow`` refuses in a class, and the checks of Temporal it relies on.

Workflow classes must be defined at the top of a module, so each class that is refused is here
at the top of this one, undecorated, and the test decorates it by hand.
"""

from typing import Any, get_type_hints

import pytest
from temporalio import workflow

from dsl.definitions import GreetRequest, agent, run_of_the_request, the_greeters_project
from dsl.workflow import Greeter, GreeterOfARequest
from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_worker.agent_runtime.vocabulary import GraphRunPolicy
from openmarketer_worker.agent_runtime.workflow_dsl import (
    InvalidAgentWorkflow,
    agent_of,
    agent_workflow,
)

GRAPH = GraphRunPolicy(
    attempt_timeout_seconds=30,
    heartbeat_timeout_seconds=15,
    max_attempts=2,
    retry_after_seconds=1,
)


class DefinesRun:
    async def run(self, run_ref): ...


class HasARun:
    async def run(self, run_ref): ...


class InheritsRun(HasARun): ...


class DefinesTheSignal:
    def proposal_decided(self, proposal_id): ...


class BeforeIsNotAsync:
    def before(self, run): ...


class BeforeIsNotAFunction:
    before = 3


class BeforeTakesNothing:
    async def before(self): ...


class AfterTakesOnlyTheOutput:
    """What a hook ``after`` took before it was given the run."""

    async def after(self, output): ...


class AfterTakesTooMuch:
    async def after(self, run, output, more): ...


class FailedTakesNoReason:
    async def failed(self, run): ...


@pytest.mark.parametrize(
    ("refused", "why"),
    [
        (DefinesRun, "defines run"),
        (InheritsRun, "defines run"),
        (DefinesTheSignal, "defines proposal_decided"),
        (BeforeIsNotAsync, "hook before"),
        (BeforeIsNotAFunction, "hook before"),
        (BeforeTakesNothing, "hook before"),
        (AfterTakesOnlyTheOutput, "hook after"),
        (AfterTakesTooMuch, "hook after"),
        (FailedTakesNoReason, "hook failed"),
    ],
    ids=lambda case: getattr(case, "__name__", case),
)
def test_class_that_cannot_be_the_workflow_is_refused_by_name_and_said_why(refused, why):
    with pytest.raises(InvalidAgentWorkflow, match=why) as error:
        agent_workflow(agent, name="Refused", graph=GRAPH, scope_of=the_greeters_project)(refused)
    assert f"workflow class {refused.__name__}" in str(error.value)


def test_class_defined_inside_a_function_is_refused_because_temporal_cannot_reach_it():
    class Local:
        pass

    with pytest.raises(InvalidAgentWorkflow, match="at the top of a module") as error:
        agent_workflow(agent, name="Refused", graph=GRAPH, scope_of=the_greeters_project)(Local)
    assert "Local" in str(error.value)


def test_workflow_without_a_name_is_refused_because_that_is_how_it_is_started():
    with pytest.raises(InvalidAgentWorkflow, match="needs a name"):
        agent_workflow(agent, name="", graph=GRAPH)


def test_workflow_of_an_agent_whose_action_starts_a_run_without_a_scope_is_refused():
    # The greeter has an action that starts another run, and such a run belongs to the
    # parent's project: a workflow of the agent must say which one that is.
    with pytest.raises(
        InvalidAgentWorkflow, match="'Refused'.*'wait_for_a_greeting' starts another run.*scope_of"
    ):
        agent_workflow(agent, name="Refused", graph=GRAPH)


def test_input_without_a_way_to_get_the_run_out_of_it_is_refused():
    with pytest.raises(InvalidAgentWorkflow, match="'Refused'.*GreetRequest.*give run_of"):
        agent_workflow(
            agent, name="Refused", graph=GRAPH, input=GreetRequest, scope_of=the_greeters_project
        )


def test_way_to_get_the_run_out_of_an_input_the_workflow_does_not_take_is_refused():
    with pytest.raises(InvalidAgentWorkflow, match="'Refused'.*needs no run_of"):
        agent_workflow(
            agent,
            name="Refused",
            graph=GRAPH,
            run_of=run_of_the_request,
            scope_of=the_greeters_project,
        )


def test_input_given_as_the_name_of_its_class_is_refused_because_temporal_decodes_from_the_class():
    with pytest.raises(InvalidAgentWorkflow, match="'Refused'.*never from its name"):
        agent_workflow(
            agent,
            name="Refused",
            graph=GRAPH,
            input="GreetRequest",  # pyright: ignore[reportArgumentType]
            run_of=run_of_the_request,
            scope_of=the_greeters_project,
        )


def test_failure_types_given_as_one_text_are_refused_because_a_text_holds_every_part_of_itself():
    with pytest.raises(InvalidAgentWorkflow, match="'Refused'.*collection of failure types"):
        agent_workflow(
            agent,
            name="Refused",
            graph=GRAPH,
            scope_of=the_greeters_project,
            worded_types="NoteRefused",
        )


# ------------------------------------------------------- what the decorator makes of a class
def the_run_method_of(workflow_class: type) -> Any:
    """The ``run`` the decorator wrote, which the class has no declaration of for a type checker."""
    return vars(workflow_class)["run"]


def test_run_of_a_workflow_with_an_input_of_its_own_is_annotated_with_that_class_itself():
    # A real type, and not its name: Temporal decodes the input from this annotation.
    run = the_run_method_of(GreeterOfARequest)
    assert run.__annotations__["run_input"] is GreetRequest
    assert get_type_hints(run)["run_input"] is GreetRequest


def test_run_of_a_workflow_that_takes_the_run_is_annotated_with_the_run():
    assert get_type_hints(the_run_method_of(Greeter))["run_input"] is GraphRunRef


def test_class_the_decorator_made_a_workflow_of_is_marked_with_its_agent():
    assert agent_of(Greeter) is agent


def test_subclass_of_a_workflow_of_an_agent_is_not_marked_as_one():
    class Subclass(Greeter): ...

    assert agent_of(Subclass) is None


def test_class_no_decorator_made_a_workflow_of_has_no_agent():
    assert agent_of(DefinesRun) is None


# ----------------------------------------------- what the decorator relies on in Temporal
def test_temporal_refuses_a_run_made_in_a_function_and_takes_one_named_as_a_method_of_its_class():
    # ``agent_workflow`` builds ``run`` inside a decorator, which Temporal refuses by the
    # qualified name of the function, and gives it the name of a method of the class and the
    # class's module, which is all Temporal checks of where a run is defined. If this test
    # fails, Temporal has changed one of the two checks, and the decorator with them.
    async def run(self, run_ref): ...

    try:
        workflow.run(run)
    except ValueError:
        pass
    else:
        pytest.fail(
            "Temporal takes a run function made inside a function now: "
            "agent_workflow no longer needs to rename the function it builds"
        )
    # A class of its own each time: Temporal marks the class it makes a workflow of, and refuses
    # a second mark, so a class at the top of the module would fail the test the second time
    # it runs in a process.
    pinned = type("Pinned", (), {})
    run.__qualname__ = f"{pinned.__qualname__}.run"
    run.__module__ = pinned.__module__
    pinned.run = workflow.run(run)  # pyright: ignore[reportAttributeAccessIssue]
    try:
        workflow.defn(name="Pinned")(pinned)
    except ValueError as changed:
        pytest.fail(
            "Temporal no longer takes a run function that a decorator built and named as a "
            f"method of the class: agent_workflow must change ({changed})"
        )
