"""The greeter's graph: a node that asks for a greeting, and where the run goes from there.

Only the worker's process loads this module: it imports LangGraph's side of core. Importing it is
also what registers the nodes on the agent, so ``wiring.py`` imports the functions below. The
node reads whom to greet from the run's context and keeps no state of its own.

What the run asks for is decided by its context's subject, which a test sets: an ordinary
greeting (``Greet``), one of the two run actions (``WaitForAGreeting``, ``StartAGreeting``),
or, for the tests of what must not run, another agent's action or a payload that does not fit.
"""

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel
from scripted_graphs import asking_for

from dsl.definitions import (
    CHILD_FAILS,
    CHILD_GREETS,
    CHILD_GREETS_SLOWLY,
    CHILD_WAITS,
    Greet,
    GreeterContext,
    Greeting,
    StartAGreeting,
    Unlisted,
    WaitForAGreeting,
    agent,
)
from openmarketer_core.agent_runtime.actions import ActionOutcome
from openmarketer_core.agent_runtime.agent import END
from openmarketer_core.agent_runtime.langgraph_steps import request_action
from openmarketer_core.agent_runtime.steps import GraphRunRef

# Subjects that make the graph ask for what it must not, as a model that was led astray would.
ASKS_FOR_ANOTHER_AGENTS_ACTION = "asks for another agent's action"
ASKS_WITH_A_PAYLOAD_THAT_DOES_NOT_FIT = "asks with a payload that does not fit"
# Subjects that make the graph ask for a run of the child workflow. The first three wait for
# it, the last two start it and go on; the slow one is the one a test cancels its parent in.
WAITS_FOR_A_GREETING = "waits for a greeting"
WAITS_FOR_A_FAILING_GREETING = "waits for a failing greeting"
WAITS_FOR_A_GREETING_THAT_NEVER_COMES = "waits for a greeting that never comes"
STARTS_A_GREETING = "starts a greeting"
STARTS_A_SLOW_GREETING_AND_GREETS = "starts a slow greeting and then greets"

_WAITED_FOR = {
    WAITS_FOR_A_GREETING: CHILD_GREETS,
    WAITS_FOR_A_FAILING_GREETING: CHILD_FAILS,
    WAITS_FOR_A_GREETING_THAT_NEVER_COMES: CHILD_WAITS,
}
_STARTED = {
    STARTS_A_GREETING: CHILD_GREETS,
    STARTS_A_SLOW_GREETING_AND_GREETS: CHILD_GREETS_SLOWLY,
}


def initial_state(run: GraphRunRef) -> dict[str, Any]:
    return {"reply": "", "summary": ""}


def final_result(state: Mapping[str, Any]) -> dict[str, Any]:
    """What a run reports: the summary, and not the state it came from."""
    return {"summary": state["summary"]}


@agent.node(start=True)
def ask(state: Greeting, context: GreeterContext) -> dict[str, Any]:
    outcome = request_action(_greeting_asked_for(context.subject))
    return {"reply": _reply_of(outcome)}


@agent.node
def greet_again(state: Greeting, context: GreeterContext) -> dict[str, Any]:
    """A second, ordinary greeting: what the graph does after a run it started.

    A test holds its handler, which is how it cancels a parent that has a run of its own
    already going.
    """
    outcome = request_action(Greet(name="Ada"))
    return {"reply": _reply_of(outcome)}


@agent.node
def wrap_up(state: Greeting) -> dict[str, Any]:
    return {"summary": f"{state['reply']}!"}


@agent.node
def give_up(state: Greeting) -> dict[str, Any]:
    return {"summary": "nobody was greeted"}


@agent.route(after=ask, to=[greet_again, wrap_up, give_up])
def was_greeted(state: Greeting, context: GreeterContext) -> str:
    if context.subject == STARTS_A_SLOW_GREETING_AND_GREETS:
        return greet_again.__name__
    return wrap_up.__name__ if state["reply"] else give_up.__name__


agent.edge(greet_again, wrap_up)
agent.edge(wrap_up, END)
agent.edge(give_up, END)


def _reply_of(outcome: ActionOutcome) -> str:
    """What the run's reply is taken from: a handler's text, a child's greeting, or a run id."""
    if not outcome.succeeded:
        return ""
    for key in ("text", "greeting", "run_id"):
        value = outcome.result.get(key)
        if isinstance(value, str):
            return value
    return ""


def _greeting_asked_for(subject: str) -> BaseModel:
    if subject == ASKS_FOR_ANOTHER_AGENTS_ACTION:
        return Unlisted(name=subject)
    if subject == ASKS_WITH_A_PAYLOAD_THAT_DOES_NOT_FIT:
        return asking_for("greet", {"nom": subject})
    if subject in _WAITED_FOR:
        return WaitForAGreeting(name="Ada", outcome=_WAITED_FOR[subject])
    if subject in _STARTED:
        return StartAGreeting(name="Ada", outcome=_STARTED[subject])
    return Greet(name=subject)
