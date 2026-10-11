"""The greeter's graph: a node that asks for a greeting, and where the run goes from there.

Only the worker's process loads this module: it imports LangGraph's side of core. Importing it is
also what registers the nodes on the agent, so ``handlers.py`` imports the functions below.
"""

import uuid
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel
from scripted_graphs import asking_for

from dsl.definitions import Greet, Greeting, Unlisted, agent
from openmarketer_core.agent_runtime.agent import END
from openmarketer_core.agent_runtime.langgraph_steps import request_action
from openmarketer_core.agent_runtime.steps import GraphRunRef

# Who each run greets. A real agent reads this from its run's row in the database.
subjects: dict[uuid.UUID, str] = {}

# Subjects that make the graph ask for what it must not, as a model that was led astray would.
ASKS_FOR_ANOTHER_AGENTS_ACTION = "asks for another agent's action"
ASKS_WITH_A_PAYLOAD_THAT_DOES_NOT_FIT = "asks with a payload that does not fit"


def initial_state(run: GraphRunRef) -> dict[str, Any]:
    return {"name": subjects.get(run.run_id, "Ada"), "reply": "", "summary": ""}


def final_result(state: Mapping[str, Any]) -> dict[str, Any]:
    """What a run reports: the summary, and not the state it came from."""
    return {"summary": state["summary"]}


@agent.node(start=True)
def ask(state: Greeting) -> dict[str, Any]:
    outcome = request_action(_greeting_asked_for(state["name"]))
    return {"reply": outcome.result["text"] if outcome.succeeded else ""}


@agent.node
def wrap_up(state: Greeting) -> dict[str, Any]:
    return {"summary": f"{state['reply']}!"}


@agent.node
def give_up(state: Greeting) -> dict[str, Any]:
    return {"summary": "nobody was greeted"}


@agent.route(after=ask, to=[wrap_up, give_up])
def was_greeted(state: Greeting) -> str:
    return wrap_up.__name__ if state["reply"] else give_up.__name__


agent.edge(wrap_up, END)
agent.edge(give_up, END)


def _greeting_asked_for(subject: str) -> BaseModel:
    if subject == ASKS_FOR_ANOTHER_AGENTS_ACTION:
        return Unlisted(name=subject)
    if subject == ASKS_WITH_A_PAYLOAD_THAT_DOES_NOT_FIT:
        return asking_for("greet", {"nom": subject})
    return Greet(name=subject)
