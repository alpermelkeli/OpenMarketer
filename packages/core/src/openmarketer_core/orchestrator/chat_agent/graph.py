"""The chat agent's graph: its nodes, what follows what, and the in-memory turn.

    call_model -> run_tools -> call_model -> ... -> END
         ^___________|  (until an answer is submitted with submit_answer)

Importing this module is what puts the read tools and the nodes on ``definitions.agent``.
``answer_message`` runs one turn in memory, in the process that serves the message: a chat
turn has no workflow and no checkpoints, so a turn that is lost (a process that died) is
started again by sending the message again. Before the graph runs, a long history is
summarised (``rules.compact_history``). The reads a node makes go through ``ChatReads``,
built by whoever serves the message; the graph itself reaches no database.

This module is wiring only: the prompt, the limits and the checks live in ``rules.py``,
the tools in ``tools.py``, and the state and the context in ``definitions.py``.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from openmarketer_core.agent_runtime.agent import END
from openmarketer_core.agent_runtime.langgraph_agent import run_from
from openmarketer_core.llm import ChatModel
from openmarketer_core.orchestrator.chat_agent import (
    rules,
    tools,  # noqa: F401  (declares the read tools)
)
from openmarketer_core.orchestrator.chat_agent.definitions import (
    ChatTurnContext,
    ChatTurnState,
    agent,
    chat_turn_of,
    initial_state,
)
from openmarketer_core.orchestrator.chat_agent.rules import ChatLimits, ChatTurn, ChatTurnError
from openmarketer_core.orchestrator.reads import ChatReads

FORCE_SUBMIT = {"type": "function", "function": {"name": rules.SUBMIT}}


@agent.node(start=True)
def call_model(state: ChatTurnState, context: ChatTurnContext) -> dict[str, Any]:
    limits = context.limits
    cost = state["cost_usd"]
    if cost >= limits.max_cost_usd:
        raise ChatTurnError(f"cost limit reached (${cost:.3f}) before an answer was given")
    if state["step"] >= limits.max_model_calls:
        raise ChatTurnError(f"no answer after {limits.max_model_calls} model calls")
    step = state["step"] + 1
    wrapping_up = rules.is_wrapping_up(step, cost, len(state["tool_calls"]), limits)

    new_messages: list[dict[str, Any]] = []
    if wrapping_up and not state["wrap_up_sent"]:
        new_messages.append(
            {
                "role": "user",
                "content": "You are out of reading budget. Answer the operator now.",
            }
        )
    reply = context.model.chat(
        rules.ROLE,
        state["messages"] + new_messages,
        tools=rules.tool_schemas(agent),
        tool_choice=FORCE_SUBMIT if wrapping_up else None,
    )
    new_messages.append(reply.message)
    if not reply.tool_calls:
        new_messages.append(
            {
                "role": "user",
                "content": (
                    f"Continue with the tools, or call {rules.SUBMIT} when the answer is complete."
                ),
            }
        )
    return {
        "messages": new_messages,
        "pending_calls": reply.tool_calls,
        "step": step,
        "cost_usd": cost + reply.cost_usd,
        "wrapping_up": wrapping_up,
        "wrap_up_sent": state["wrap_up_sent"] or wrapping_up,
        "model": reply.model or state["model"],
    }


@agent.node
def run_tools(state: ChatTurnState, context: ChatTurnContext) -> dict[str, Any]:
    last_chance = state["wrapping_up"] or state["step"] >= context.limits.max_model_calls
    outcome = rules.run_tool_calls(state["pending_calls"], agent, context, last_chance=last_chance)
    return {
        "messages": outcome.messages,
        "tool_calls": outcome.names,
        "pending_calls": [],
        "answer": outcome.answer,
    }


@agent.route(after=call_model, to=[run_tools, call_model])
def after_model(state: ChatTurnState) -> str:
    return run_tools.__name__ if state["pending_calls"] else call_model.__name__


@agent.route(after=run_tools, to=[call_model, END])
def after_tools(state: ChatTurnState) -> str:
    return END if state["answer"] is not None else call_model.__name__


def recursion_limit(limits: ChatLimits) -> int:
    """How many graph steps a turn under ``limits`` may take before it is stopped as a defect."""
    # Two graph steps per model call, plus the final check that raises on the limits.
    return 2 * limits.max_model_calls + 6


def answer_message(
    reads: ChatReads,
    model: ChatModel,
    history: Sequence[Mapping[str, Any]],
    *,
    limits: ChatLimits | None = None,
) -> ChatTurn:
    """Run one turn in memory and return the operator's answer.

    A long history is summarised first (``rules.compact_history``): one more model call
    replaces everything but its newest messages, and its cost counts against the turn. The
    turn is then handed the conversation up to the operator's latest message and reads the
    project through ``reads``. Nothing is stored: a turn that fails is lost, and sending the
    message again starts it again. Raises ``ChatTurnError`` when no answer is given within
    the limits and ``LLMError`` when the model cannot be used.
    """
    limits = limits or ChatLimits()
    prepared = rules.compact_history(history, model)
    state = initial_state(prepared.messages)
    state["cost_usd"] = prepared.cost_usd
    context = ChatTurnContext(model=model, reads=reads, limits=limits)
    final = run_from(
        agent,
        call_model.__name__,
        state,
        context=context,
        recursion_limit=recursion_limit(limits),
    )
    return chat_turn_of(final)
