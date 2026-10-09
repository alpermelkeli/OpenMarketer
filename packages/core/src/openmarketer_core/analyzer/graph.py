"""The analyzer agent as a LangGraph graph.

This module is wiring only: two nodes and the edges between them. The rules
live in ``rules.py``, model access goes through ``ChatModel`` (the model
router), and repository access goes through ``RepoTools``.

    START -> call_model -> run_tools -> call_model -> ... -> END
                 ^______________|  (until a submitted profile is accepted)

Collaborators are bound when the graph is built, so the state holds only
plain data and can be checkpointed. With a ``ResumableRun`` the graph saves a
checkpoint after each step, and a later attempt at the same run continues
from the last one. This module does not open the checkpoint store
(``graph_checkpoints/postgres.py`` does) and does not decide when a run's checkpoints are
forgotten: its caller does, when the run is over.
"""

from __future__ import annotations

import logging
import operator
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import StateSnapshot

from openmarketer_core.analyzer import rules
from openmarketer_core.analyzer.rules import (
    Analysis,
    AnalysisError,
    Limits,
    Resumption,
    StoredProgress,
)
from openmarketer_core.analyzer.tools import RepoTools
from openmarketer_core.extraction import ExtractedFact
from openmarketer_core.graph_checkpoints import RunCheckpoints
from openmarketer_core.intake import RepoFiles
from openmarketer_core.llm import ChatModel
from openmarketer_core.profile import ProductProfile

logger = logging.getLogger(__name__)

FORCE_SUBMIT = {"type": "function", "function": {"name": rules.SUBMIT}}
# The checkpoint of a finished step is in the store before the next step starts.
CHECKPOINT_DURABILITY = "sync"


class AnalyzerState(TypedDict):
    commit_sha: str  # the commit a resumable run analyses; empty without checkpoints
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


AnalyzerGraph = CompiledStateGraph[AnalyzerState, None, AnalyzerState, AnalyzerState]


@dataclass(frozen=True)
class ResumableRun:
    """An analysis run that survives a failed attempt: its checkpoints and the commit it reads."""

    checkpoints: RunCheckpoints
    commit_sha: str


def build_graph(
    model: ChatModel,
    tools: RepoTools,
    limits: Limits,
    *,
    checkpoints: BaseCheckpointSaver[str] | None = None,
) -> AnalyzerGraph:
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
    return builder.compile(checkpointer=checkpoints)


def analyze(
    files: RepoFiles,
    model: ChatModel,
    *,
    facts: Iterable[ExtractedFact] = (),
    limits: Limits | None = None,
    resume: ResumableRun | None = None,
) -> Analysis:
    """Run the analyzer agent on a repository snapshot and return the drafted profile.

    With ``resume``, a checkpoint is stored after every graph step, and an
    attempt begins with what an earlier attempt at the same run left behind
    (``rules.resumption`` decides; the result says which it was):

    - nothing: it starts from the beginning;
    - steps on the same commit: it continues after the last finished one.
      Steps, cost, submissions and the conversation carry over, so the limits
      hold across attempts. The step that was running when the earlier attempt
      stopped runs again: ``run_tools`` only reads files, a repeated
      ``call_model`` pays for one model call;
    - a finished analysis of the same commit: that result is returned and the
      model is not called;
    - anything on another commit: it is forgotten and the analysis starts over.

    Raises ``AnalysisError`` when no profile is accepted within the limits,
    ``LLMError`` when the model cannot be used, and whatever the checkpoint
    store raises when it cannot be used.
    """
    limits = limits or Limits()
    tools = RepoTools(files)
    # Two graph steps per model turn, plus the final check that raises on the limits.
    recursion_limit = 2 * limits.max_steps + 10
    if resume is None:
        graph = build_graph(model, tools, limits)
        initial = _initial_state(files, facts, commit_sha="")
        final = graph.invoke(initial, {"recursion_limit": recursion_limit})
        return _analysis(final, Resumption.FRESH)

    thread_id = resume.checkpoints.thread_id
    graph = build_graph(model, tools, limits, checkpoints=resume.checkpoints.store).with_config(
        {"recursion_limit": recursion_limit, "configurable": {"thread_id": thread_id}}
    )
    how = rules.resumption(_stored_progress(graph), resume.commit_sha)
    if how is Resumption.ALREADY_FINISHED:
        return _analysis(_latest_checkpoint(graph).values, how)
    if how is Resumption.CONTINUED:
        return _analysis(graph.invoke(None, durability=CHECKPOINT_DURABILITY), how)
    if how is Resumption.STARTED_OVER:
        logger.info(
            "checkpoints of thread %s are not of commit %s: starting over",
            thread_id,
            resume.commit_sha,
        )
        resume.checkpoints.forget()
    initial = _initial_state(files, facts, commit_sha=resume.commit_sha)
    return _analysis(graph.invoke(initial, durability=CHECKPOINT_DURABILITY), how)


def _initial_state(
    files: RepoFiles, facts: Iterable[ExtractedFact], *, commit_sha: str
) -> AnalyzerState:
    return {
        "commit_sha": commit_sha,
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


def _latest_checkpoint(graph: AnalyzerGraph) -> StateSnapshot:
    # A resumable graph is bound to its thread, and ``get_state`` still asks which one to read.
    return graph.get_state(graph.config or {})


def _stored_progress(graph: AnalyzerGraph) -> StoredProgress | None:
    """What the latest checkpoint of the graph's thread says, or ``None`` when it has none."""
    latest = _latest_checkpoint(graph)
    if latest.created_at is None:
        return None
    # A checkpoint from before the first step holds no state yet, so no commit either.
    commit_sha = latest.values.get("commit_sha", "")
    # Not ``latest.next``: it is also empty when a step ran and its checkpoint was not stored.
    return StoredProgress(commit_sha=commit_sha, finished=latest.values.get("profile") is not None)


def _analysis(final: dict[str, Any], how: Resumption) -> Analysis:
    return Analysis(
        profile=ProductProfile.model_validate(final["profile"]),
        cost_usd=final["cost_usd"],
        steps=final["step"],
        model=final["model"],
        tool_calls=final["tool_calls"],
        notes=final["notes"],
        resumption=how,
    )
