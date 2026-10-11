"""Tests for what ``agent_workflow`` refuses in a class, and the checks of Temporal it relies on.

Workflow classes must be defined at the top of a module, so each class that is refused is here
at the top of this one, undecorated, and the test decorates it by hand.
"""

import pytest
from temporalio import workflow

from dsl.definitions import agent
from openmarketer_worker.agent_runtime.vocabulary import GraphRunPolicy
from openmarketer_worker.agent_runtime.workflow_dsl import InvalidAgentWorkflow, agent_workflow

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


class BeforeIsNotAsync:
    def before(self, run): ...


class BeforeIsNotAFunction:
    before = 3


class BeforeTakesNothing:
    async def before(self): ...


class AfterTakesTooMuch:
    async def after(self, output, more): ...


class FailedTakesNoReason:
    async def failed(self, run): ...


@pytest.mark.parametrize(
    ("refused", "why"),
    [
        (DefinesRun, "defines run"),
        (InheritsRun, "defines run"),
        (BeforeIsNotAsync, "hook before"),
        (BeforeIsNotAFunction, "hook before"),
        (BeforeTakesNothing, "hook before"),
        (AfterTakesTooMuch, "hook after"),
        (FailedTakesNoReason, "hook failed"),
    ],
    ids=lambda case: getattr(case, "__name__", case),
)
def test_class_that_cannot_be_the_workflow_is_refused_by_name_and_said_why(refused, why):
    with pytest.raises(InvalidAgentWorkflow, match=why) as error:
        agent_workflow(agent, name="Refused", graph=GRAPH)(refused)
    assert f"workflow class {refused.__name__}" in str(error.value)


def test_class_defined_inside_a_function_is_refused_because_temporal_cannot_reach_it():
    class Local:
        pass

    with pytest.raises(InvalidAgentWorkflow, match="at the top of a module") as error:
        agent_workflow(agent, name="Refused", graph=GRAPH)(Local)
    assert "Local" in str(error.value)


def test_workflow_without_a_name_is_refused_because_that_is_how_it_is_started():
    with pytest.raises(InvalidAgentWorkflow, match="needs a name"):
        agent_workflow(agent, name="", graph=GRAPH)


# ----------------------------------------------- what the decorator relies on in Temporal
class Pinned:
    """A plain class, made a workflow by hand as ``agent_workflow`` does it."""


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
    run.__qualname__ = f"{Pinned.__qualname__}.run"
    run.__module__ = Pinned.__module__
    Pinned.run = workflow.run(run)  # pyright: ignore[reportAttributeAccessIssue]
    try:
        workflow.defn(name="Pinned")(Pinned)
    except ValueError as changed:
        pytest.fail(
            "Temporal no longer takes a run function that a decorator built and named as a "
            f"method of the class: agent_workflow must change ({changed})"
        )
