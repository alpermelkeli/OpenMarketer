"""The chat agent's declarations: the agent, its state, its context, and how a turn ends.

One chat turn is the orchestrator's answer to one message from the operator about one
project: the model reads the project through its tools and submits the answer with
``submit_answer`` (``rules.py``); ``graph.py`` wires the nodes. Unlike the analyzer, a turn
has no workflow and no checkpoints: it runs in the process that serves the message
(``graph.answer_message``), so the state below holds nothing that outlives it.

- ``ChatTurnState`` is what the graph keeps between its steps: plain data only.
- ``ChatTurnContext`` is what a node is given besides the state: the model, the reading port
  of the project the turn is about, and the turn's limits. It is built for each turn.
- ``agent`` is the agent; importing ``graph.py`` is what puts its nodes and tools on it.

Nothing here runs: no node, no model call, no read. The prompt, the limits and the checks
are in ``rules.py``; the tools are in ``tools.py``.
"""

import operator
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

from openmarketer_core.agent_runtime.agent import Agent
from openmarketer_core.llm import ChatModel
from openmarketer_core.orchestrator.chat_agent.rules import (
    SYSTEM_PROMPT,
    ChatLimits,
    ChatTurn,
    ChatTurnError,
    conversation_messages,
)
from openmarketer_core.orchestrator.reads import ChatReads
from openmarketer_core.orchestrator.requests import ChatRequests

CHAT_TURN_KIND = "orchestrator-chat"


class ChatTurnState(TypedDict):
    messages: Annotated[list[dict[str, Any]], operator.add]
    tool_calls: Annotated[list[str], operator.add]
    pending_calls: list[dict[str, Any]]  # tool calls of the latest model turn
    step: int  # model calls made so far
    cost_usd: float
    wrapping_up: bool  # the latest model call was told reading is over
    wrap_up_sent: bool
    model: str
    answer: str | None  # the accepted answer, once one is submitted


@dataclass
class ChatTurnContext:
    """What the nodes of one turn are given besides the state.

    ``reads`` is the project the turn is about (``orchestrator/reads.py``), scoped to it, so
    the graph cannot read beyond the project. ``requests`` is what a request action may ask
    of the process that runs the turn (``orchestrator/requests.py``): a caller that holds a
    database and a workflow server gives one; without it (the CLI, a test that only reads)
    the tool that would ask answers that starting runs is not available. A context serves one
    turn, in one thread.
    """

    model: ChatModel
    reads: ChatReads
    limits: ChatLimits
    requests: ChatRequests | None = None


agent = Agent(CHAT_TURN_KIND, state=ChatTurnState, context=ChatTurnContext)


def initial_state(history: Sequence[Mapping[str, Any]]) -> ChatTurnState:
    """What a turn starts from: the conversation so far, and nothing read or spent yet.

    ``history`` is the thread's messages in order; ``rules.conversation_messages`` keeps the
    operator's and the assistant's texts and leaves anything else out.
    """
    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            *conversation_messages(history),
        ],
        "tool_calls": [],
        "pending_calls": [],
        "step": 0,
        "cost_usd": 0.0,
        "wrapping_up": False,
        "wrap_up_sent": False,
        "model": "",
        "answer": None,
    }


def chat_turn_of(final_state: Mapping[str, Any]) -> ChatTurn:
    """The result of a turn whose graph finished: the operator's answer and what it cost."""
    answer = final_state["answer"]
    if not isinstance(answer, str) or not answer:
        raise ChatTurnError("the turn ended without an answer")
    return ChatTurn(
        answer=answer,
        cost_usd=final_state["cost_usd"],
        steps=final_state["step"],
        model=final_state["model"],
        tool_calls=final_state["tool_calls"],
    )
