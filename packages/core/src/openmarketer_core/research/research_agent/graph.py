"""The researcher's graph: its nodes, what follows what, and how one attempt asks the model.

This module is wiring only. The rules live in ``rules.py``, model access goes through
``ChatModel`` (the model router), and the two actions (``search_web``, ``fetch_page``) reach
the network only through the workflow that runs them; the text of a page is read through the
attempt's context. The state holds plain data only and can be checkpointed.

    plan -> search -> ... -> search -> fetch -> ... -> fetch -> read -> ... -> read
      ^                                                                        |
      |                                        synthesize -> check <----------+
      |                                                          |
      +-------------------------- one more round --------------->| (unanswered questions)
                                                                 v
                                                              report -> END

``plan`` turns the request into search queries (again, with the gaps, for one more round),
``search`` and ``fetch`` ask for an action each, ``read`` has the reader make fixed fields of
one stored page, ``synthesize`` turns the readings into findings with their sources, ``check``
verifies them against the pages this run fetched, and ``report`` finishes the run; whoever
runs the graph stores the report when it ends.

Every node is safe to run twice: a step that died before its checkpoint is taken again from
the same state, and the model calls are made for this attempt only.
"""

import json
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from openmarketer_core.agent_runtime.agent import END
from openmarketer_core.agent_runtime.langgraph_steps import request_action
from openmarketer_core.agent_runtime.model_input import DoesNotFit, read_strict_json
from openmarketer_core.research.page_text import PageTextUnavailable
from openmarketer_core.research.questions import ResearchRequest
from openmarketer_core.research.research_agent import rules
from openmarketer_core.research.research_agent.definitions import (
    FetchPage,
    ResearchContext,
    ResearchState,
    SearchWeb,
    agent,
)
from openmarketer_core.research.research_agent.rules import (
    DraftFinding,
    DraftFindings,
    QueryPlan,
    Reading,
    ResearchError,
)


@dataclass
class _Submission:
    """What one submission cost, on the way to what was submitted."""

    cost_usd: float = 0.0
    model: str = ""


def _ask_for[Model: BaseModel](
    context: ResearchContext,
    role: str,
    messages: list[dict[str, Any]],
    *,
    tool: dict[str, Any],
    tool_name: str,
    model_type: type[Model],
    spent: _Submission,
) -> Model:
    """Ask the model for one submission, at most twice; the submission, or ``ResearchError``.

    The first call forces the submission tool. An answer that is not the call, carries no
    readable arguments, or does not fit the fields is told what was wrong and asked once
    more, inside the same node; a second unusable answer ends the run. Every call's cost is
    added to ``spent``, whichever attempt it belonged to.
    """
    for _ in range(rules.SUBMISSION_ATTEMPTS):
        reply = context.model.chat(
            role, messages, tools=[tool], tool_choice=rules.forced(tool_name)
        )
        spent.cost_usd += reply.cost_usd
        spent.model = reply.model or spent.model
        submitted = _read_call(reply, tool_name, model_type)
        if not isinstance(submitted, str):
            return submitted
        messages = [*messages, reply.message, {"role": "user", "content": submitted}]
    raise ResearchError(f"the model did not submit a readable {tool_name} after two attempts")


def _read_call[Model: BaseModel](
    reply: Any, tool_name: str, model_type: type[Model]
) -> Model | str:
    """The submission of the forced call, or the text to tell the model what was wrong."""
    calls = [c for c in reply.tool_calls if c.get("function", {}).get("name") == tool_name]
    if not calls:
        return f"Call {tool_name} with the fields it takes."
    try:
        arguments = json.loads(calls[0]["function"].get("arguments") or "{}")
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
    except (ValueError, KeyError) as e:
        return f"error: could not parse the arguments: {e}"
    try:
        return read_strict_json(model_type, arguments)
    except DoesNotFit as e:
        return f"Rejected. Fix this and submit again: {e}"


def _request_of(state: ResearchState) -> ResearchRequest:
    """The run's request as the prompts take it, rebuilt from the state."""
    return ResearchRequest(
        questions=state["questions"],
        topics=state["topics"],
        competitors=state["competitors"],
        time_window=state["time_window"],
        max_sources=state["max_sources"],
    )


def _cost_left(state: ResearchState, context: ResearchContext) -> None:
    if state["cost_usd"] >= context.limits.max_cost_usd:
        raise ResearchError(
            f"cost limit reached (${state['cost_usd']:.3f}) before a report was ready"
        )


def _page_cap(state: ResearchState, context: ResearchContext) -> int:
    return min(state["max_sources"], context.limits.max_pages)


@agent.node(start=True)
def plan(state: ResearchState, context: ResearchContext) -> dict[str, Any]:
    """Ask the researcher model for this round's search queries."""
    _cost_left(state, context)
    round_no = state["round"] + 1
    if round_no > context.limits.max_rounds:
        raise ResearchError(f"a round {round_no} was planned, over the limit")
    spent = _Submission()
    planned = _ask_for(
        context,
        rules.RESEARCHER_ROLE,
        rules.scope_messages(
            _request_of(state),
            round_no=round_no,
            gaps=state["gaps"],
            queries_so_far=state["queries"],
        ),
        tool=rules.queries_tool(),
        tool_name=rules.SUBMIT_QUERIES,
        model_type=QueryPlan,
        spent=spent,
    )
    known = {query.casefold() for query in state["queries"]}
    fresh = [
        query
        for query in planned.queries[: context.limits.max_queries_per_round]
        if query.casefold() not in known
    ]
    if not fresh:
        raise ResearchError("the model planned no query that has not been searched already")
    return {
        "round": round_no,
        "queries": [*state["queries"], *fresh],
        "categories": rules.plan_categories(planned.categories) or ["general"],
        "cost_usd": state["cost_usd"] + spent.cost_usd,
        "model": spent.model or state["model"],
    }


@agent.node
def search(state: ResearchState, context: ResearchContext) -> dict[str, Any]:
    """Ask for one query to be searched; its results join the candidates to fetch."""
    query = state["queries"][state["next_query"]]
    outcome = request_action(
        SearchWeb(
            workspace_id=uuid.UUID(state["workspace_id"]),
            project_id=uuid.UUID(state["project_id"]),
            run_id=uuid.UUID(state["run_id"]),
            query=query,
            categories=state["categories"],
        )
    )
    next_query = state["next_query"] + 1
    if not outcome.succeeded:
        return {
            "next_query": next_query,
            "notes": [*state["notes"], f"a search did not answer: {outcome.error}"],
        }
    results = [
        result
        for result in outcome.result.get("results", [])
        if isinstance(result, dict) and isinstance(result.get("url"), str)
    ]
    return {"results": [*state["results"], *results], "next_query": next_query}


@agent.node
def fetch(state: ResearchState, context: ResearchContext) -> dict[str, Any]:
    """Ask for one candidate to be fetched and stored; keep its identifier and flags."""
    result = state["results"][state["next_result"]]
    outcome = request_action(
        FetchPage(
            workspace_id=uuid.UUID(state["workspace_id"]),
            project_id=uuid.UUID(state["project_id"]),
            run_id=uuid.UUID(state["run_id"]),
            url=result["url"],
        )
    )
    next_result = state["next_result"] + 1
    if not outcome.succeeded:
        return {
            "next_result": next_result,
            "notes": [*state["notes"], f"a page was not fetched: {outcome.error}"],
        }
    page = {
        "page_id": str(outcome.result["page_id"]),
        "url": str(outcome.result["url"]),
        "title": str(result.get("title") or ""),
        "flags": list(outcome.result.get("flags", [])),
    }
    return {"pages": [*state["pages"], page], "next_result": next_result}


@agent.node
def read(state: ResearchState, context: ResearchContext) -> dict[str, Any]:
    """Have the reader make fixed fields of one stored page."""
    _cost_left(state, context)
    page = state["pages"][state["next_page"]]
    next_page = state["next_page"] + 1
    try:
        text = context.page_text_of(page["page_id"])
    except PageTextUnavailable:
        # The page's text is gone or cannot be read; the finding that would rest on it cannot
        # be checked, so the page is left out rather than read half-way.
        return {
            "next_page": next_page,
            "notes": [*state["notes"], "a stored page could not be read; it is left out"],
        }
    spent = _Submission()
    reading = _ask_for(
        context,
        rules.READER_ROLE,
        rules.reader_messages(state["questions"], page_url=page["url"], page_text=text),
        tool=rules.reading_tool(),
        tool_name=rules.SUBMIT_READING,
        model_type=Reading,
        spent=spent,
    )
    entry = {
        "page_id": page["page_id"],
        "url": page["url"],
        "flags": page["flags"],
        "reading": reading.model_dump(mode="json"),
    }
    return {
        "readings": [*state["readings"], entry],
        "next_page": next_page,
        "cost_usd": state["cost_usd"] + spent.cost_usd,
        "model": spent.model or state["model"],
    }


@agent.node
def synthesize(state: ResearchState, context: ResearchContext) -> dict[str, Any]:
    """Have the researcher write findings from the readings, as it submitted them."""
    if not state["readings"]:
        return {
            "drafts": [],
            "notes": [
                *state["notes"],
                "no page could be read; the report has no findings",
            ],
        }
    _cost_left(state, context)
    spent = _Submission()
    submitted = _ask_for(
        context,
        rules.RESEARCHER_ROLE,
        rules.synthesis_messages(state["questions"], state["readings"]),
        tool=rules.findings_tool(),
        tool_name=rules.SUBMIT_FINDINGS,
        model_type=DraftFindings,
        spent=spent,
    )
    return {
        "drafts": [finding.model_dump(mode="json") for finding in submitted.findings],
        "cost_usd": state["cost_usd"] + spent.cost_usd,
        "model": spent.model or state["model"],
    }


@agent.node
def check(state: ResearchState, context: ResearchContext) -> dict[str, Any]:
    """Check the submitted findings against the pages this run fetched."""
    drafts = [DraftFinding.model_validate(draft) for draft in state["drafts"]]
    pages = {page["page_id"]: page for page in state["pages"]}
    texts: dict[str, str | None] = {}
    for page_id in pages:
        try:
            texts[page_id] = context.page_text_of(page_id)
        except PageTextUnavailable:
            texts[page_id] = None
    result = rules.check_findings(drafts, questions=state["questions"], pages=pages, texts=texts)
    return {
        "findings": [finding.model_dump(mode="json") for finding in result.findings],
        "notes": [*state["notes"], *result.notes],
        "gaps": result.unanswered,
    }


@agent.node
def report(state: ResearchState, context: ResearchContext) -> dict[str, Any]:
    """Nothing to do: the state is the report, and whoever runs the graph stores it."""
    return {}


agent.edge(plan, search)
agent.edge(synthesize, check)
agent.edge(report, END)


@agent.route(after=search, to=[search, fetch])
def after_search(state: ResearchState) -> str:
    return search.__name__ if state["next_query"] < len(state["queries"]) else fetch.__name__


@agent.route(after=fetch, to=[fetch, read])
def after_fetch(state: ResearchState, context: ResearchContext) -> str:
    pages_left = len(state["pages"]) < _page_cap(state, context)
    more_results = state["next_result"] < len(state["results"])
    return fetch.__name__ if pages_left and more_results else read.__name__


@agent.route(after=read, to=[read, synthesize])
def after_read(state: ResearchState) -> str:
    return read.__name__ if state["next_page"] < len(state["pages"]) else synthesize.__name__


@agent.route(after=check, to=[plan, report])
def after_check(state: ResearchState, context: ResearchContext) -> str:
    more_to_answer = bool(state["gaps"]) and state["round"] < context.limits.max_rounds
    can_afford = state["cost_usd"] < context.limits.max_cost_usd
    return plan.__name__ if more_to_answer and can_afford else report.__name__


def recursion_limit(limits: rules.Limits) -> int:
    """How many graph steps a run under ``limits`` may take before it is stopped as a defect."""
    per_round = 1 + limits.max_queries_per_round + 2 * limits.max_pages
    return 2 * per_round * limits.max_rounds + 10
