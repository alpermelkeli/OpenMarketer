"""The analyzer agent as a LangGraph graph.

This is the only analyzer module that imports LangGraph, and it is wiring
only: two nodes and the edges between them. The rules live in ``rules.py``,
model access goes through ``ChatModel`` (the model router), and repository
access goes through ``RepoTools``.

    START -> call_model -> run_tools -> call_model -> ... -> END
                 ^______________|  (until a submitted profile is accepted)

Collaborators are bound when the graph is built, so the state holds only
plain data and can be checkpointed.
"""

from __future__ import annotations

import operator
from collections.abc import Iterable
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from openmarketer_core.analyzer import rules
from openmarketer_core.analyzer.rules import Analysis, AnalysisError, Limits
from openmarketer_core.analyzer.tools import RepoTools
from openmarketer_core.extraction import ExtractedFact
from openmarketer_core.intake import RepoFiles
from openmarketer_core.llm import ChatModel
from openmarketer_core.profile import ProductProfile

FORCE_SUBMIT = {"type": "function", "function": {"name": rules.SUBMIT}}


class AnalyzerState(TypedDict):
    messages: Annotated[list[dict[str, Any]], operator.add]
    tool_calls: Annotated[list[str], operator.add]
    pending_calls: list[dict[str, Any]]  # tool calls of the latest model turn
    step: int
    cost_usd: float
    submissions: int
    wrapping_up: bool
    wrap_up_sent: bool
    model: str
    profile: dict[str, Any] | None  # the accepted profile, as JSON data
    notes: list[str]


def build_graph(model: ChatModel, tools: RepoTools, limits: Limits):
    """Compile the analyzer graph for one repository snapshot."""
    schemas = rules.tool_schemas()

    def call_model(state: AnalyzerState) -> dict[str, Any]:
        cost = state["cost_usd"]
        if cost >= limits.max_cost_usd:
            raise AnalysisError(f"cost limit reached (${cost:.3f}) before a profile was accepted")
        if state["step"] >= limits.max_steps:
            raise AnalysisError(f"no profile after {limits.max_steps} steps")
        step = state["step"] + 1
        wrapping_up = rules.is_wrapping_up(step, cost, limits)

        new_messages: list[dict[str, Any]] = []
        if wrapping_up and not state["wrap_up_sent"]:
            new_messages.append(
                {
                    "role": "user",
                    "content": "You are out of exploration budget. Submit the profile now.",
                }
            )
        reply = model.chat(
            rules.ROLE,
            state["messages"] + new_messages,
            tools=schemas,
            tool_choice=FORCE_SUBMIT if wrapping_up else None,
        )
        new_messages.append(reply.message)
        if not reply.tool_calls:
            new_messages.append(
                {
                    "role": "user",
                    "content": f"Continue with the tools, or call {rules.SUBMIT} if you are done.",
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

    def run_tools(state: AnalyzerState) -> dict[str, Any]:
        outcome = rules.run_tool_calls(
            state["pending_calls"],
            tools,
            submissions_so_far=state["submissions"],
            wrapping_up=state["wrapping_up"],
            limits=limits,
        )
        return {
            "messages": outcome.messages,
            "tool_calls": outcome.names,
            "pending_calls": [],
            "submissions": state["submissions"] + outcome.submissions,
            "profile": outcome.profile.model_dump(mode="json") if outcome.profile else None,
            "notes": outcome.notes,
        }

    def after_model(state: AnalyzerState) -> str:
        return "run_tools" if state["pending_calls"] else "call_model"

    def after_tools(state: AnalyzerState) -> str:
        return END if state["profile"] is not None else "call_model"

    builder = StateGraph(AnalyzerState)
    builder.add_node("call_model", call_model)
    builder.add_node("run_tools", run_tools)
    builder.add_edge(START, "call_model")
    builder.add_conditional_edges("call_model", after_model, ["run_tools", "call_model"])
    builder.add_conditional_edges("run_tools", after_tools, ["call_model", END])
    return builder.compile()


def analyze(
    files: RepoFiles,
    model: ChatModel,
    *,
    facts: Iterable[ExtractedFact] = (),
    limits: Limits | None = None,
) -> Analysis:
    """Run the analyzer agent on a repository snapshot and return the drafted profile."""
    limits = limits or Limits()
    graph = build_graph(model, RepoTools(files), limits)
    initial: AnalyzerState = {
        "messages": rules.initial_messages(files, facts),
        "tool_calls": [],
        "pending_calls": [],
        "step": 0,
        "cost_usd": 0.0,
        "submissions": 0,
        "wrapping_up": False,
        "wrap_up_sent": False,
        "model": "",
        "profile": None,
        "notes": [],
    }
    # Two graph steps per model turn, plus the final check that raises on the limits.
    final = graph.invoke(initial, config={"recursion_limit": 2 * limits.max_steps + 10})
    return Analysis(
        profile=ProductProfile.model_validate(final["profile"]),
        cost_usd=final["cost_usd"],
        steps=final["step"],
        model=final["model"],
        tool_calls=final["tool_calls"],
        notes=final["notes"],
    )
