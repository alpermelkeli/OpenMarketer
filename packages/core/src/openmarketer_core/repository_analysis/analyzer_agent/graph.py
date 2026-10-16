"""The analyzer's graph: its nodes, what follows what, and the run that needs no workflow.

This module is wiring only. The rules live in ``rules.py``, model access goes through
``ChatModel`` (the model router), repository access through the attempt's reading
(``RepoFiles``/``RepoTools``), and both reach a node in its ``AnalyzerContext``, so the state
holds only plain data and can be checkpointed.

    clone -> prepare -> call_model -> run_tools -> call_model -> ... -> END
                            ^______________|  (until a submitted profile is accepted)

``clone`` does not clone: it asks the workflow for the action ``clone_repository`` and keeps
the commit that was cloned. Every later step reads that commit, so an attempt that continues
a run never meets a repository that moved under its conversation.

Importing this module is what puts the nodes and the read tools (``tools.py``) on
``definitions.agent``. A run in a workflow takes them step by step with checkpoints
(``agent_runtime.langgraph_agent.AgentSteps``, built by the worker with ``recursion_limit``);
``analyze`` runs them in memory from ``prepare`` for the command line and the benchmark,
which have cloned the repository themselves.

This module opens no checkpoint store, clones nothing and stores no profile, and does not
decide when a run's checkpoints are forgotten: whoever runs the graph does.
"""

import uuid
from collections.abc import Iterable
from typing import Any

from openmarketer_core.agent_runtime.agent import END
from openmarketer_core.agent_runtime.langgraph_agent import run_from
from openmarketer_core.agent_runtime.langgraph_steps import request_action
from openmarketer_core.llm import ChatModel
from openmarketer_core.repository_analysis.analyzer_agent import (
    rules,
    tools,  # noqa: F401  (declares the read tools)
)
from openmarketer_core.repository_analysis.analyzer_agent.definitions import (
    AnalyzerContext,
    AnalyzerState,
    CloneRepository,
    agent,
    analysis_of,
    initial_state,
)
from openmarketer_core.repository_analysis.analyzer_agent.rules import (
    Analysis,
    AnalysisError,
    Limits,
)
from openmarketer_core.repository_analysis.extraction import ExtractedFact
from openmarketer_core.repository_analysis.intake import RepoFiles

FORCE_SUBMIT = {"type": "function", "function": {"name": rules.SUBMIT}}


@agent.node(start=True)
def clone(state: AnalyzerState, context: AnalyzerContext) -> dict[str, Any]:
    """Ask for the repository of the run to be cloned, and keep the commit that was."""
    outcome = request_action(
        CloneRepository(
            workspace_id=uuid.UUID(state["workspace_id"]),
            project_id=uuid.UUID(state["project_id"]),
            run_id=uuid.UUID(state["run_id"]),
        )
    )
    if not outcome.succeeded:
        # A repository that cannot be read ends the analysis; the words are the action's.
        raise AnalysisError(outcome.error)
    return {"commit_sha": outcome.result["commit_sha"]}


@agent.node
def prepare(state: AnalyzerState, context: AnalyzerContext) -> dict[str, Any]:
    """Start the conversation with the map of the repository at the run's commit."""
    files = context.reading_at(state["commit_sha"]).files
    return {"messages": rules.initial_messages(files, context.facts_of(files))}


@agent.node
def call_model(state: AnalyzerState, context: AnalyzerContext) -> dict[str, Any]:
    limits = context.limits
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


@agent.node
def run_tools(state: AnalyzerState, context: AnalyzerContext) -> dict[str, Any]:
    # The reading this step's tool calls reach: the commit the run pinned when it cloned.
    context.read_from(state["commit_sha"])
    outcome = rules.run_tool_calls(
        state["pending_calls"],
        agent,
        context,
        submissions_so_far=state["submissions"],
        wrapping_up=state["wrapping_up"],
        limits=context.limits,
    )
    return {
        "messages": outcome.messages,
        "tool_calls": outcome.names,
        "pending_calls": [],
        "submissions": state["submissions"] + outcome.submissions,
        "profile": outcome.profile.model_dump(mode="json") if outcome.profile else None,
        "notes": outcome.notes,
    }


agent.edge(clone, prepare)
agent.edge(prepare, call_model)


@agent.route(after=call_model, to=[run_tools, call_model])
def after_model(state: AnalyzerState) -> str:
    return run_tools.__name__ if state["pending_calls"] else call_model.__name__


@agent.route(after=run_tools, to=[call_model, END])
def after_tools(state: AnalyzerState) -> str:
    return END if state["profile"] is not None else call_model.__name__


def recursion_limit(limits: Limits) -> int:
    """How many graph steps a run under ``limits`` may take before it is stopped as a defect."""
    # Two graph steps per model turn, plus the clone, the preparation and the final check
    # that raises on the limits.
    return 2 * limits.max_steps + 10


def analyze(
    files: RepoFiles,
    model: ChatModel,
    *,
    facts: Iterable[ExtractedFact] = (),
    limits: Limits | None = None,
) -> Analysis:
    """Run the analyzer agent on a repository snapshot and return the drafted profile.

    The run is in memory: nothing is stored, and a run that fails is run again from the
    start. The repository is the caller's, already cloned and scanned, so the graph is entered
    after its clone.

    Raises ``AnalysisError`` when no profile is accepted within the limits and ``LLMError``
    when the model cannot be used.
    """
    limits = limits or Limits()
    found = list(facts)
    context = AnalyzerContext(
        model=model, limits=limits, files_at=lambda _: files, facts_of=lambda _: found
    )
    final = run_from(
        agent,
        prepare.__name__,
        initial_state(),
        context=context,
        recursion_limit=recursion_limit(limits),
    )
    return analysis_of(final)
