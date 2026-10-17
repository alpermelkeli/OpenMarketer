"""The researcher's declarations: the agent, its state, what its nodes are given, and its actions.

This is the module the worker's workflow loads, so it imports no graph framework and nothing
that touches the database. The nodes are in ``graph.py``, which only the process that runs
the graph imports; importing it is what puts them on ``agent``.

- ``ResearchState`` is what the graph keeps between steps and what a checkpoint stores:
  plain data only, and never the text of a page. A node that needs a page's text reads it
  from the run's stored pages through its context, and the graph carries only the page's id.
- ``ResearchContext`` is what a node needs that is not state: the model of the attempt, the
  decision model that screens search results, the limits, and the stored text of a page the
  run fetched.
- ``SearchWeb`` and ``FetchPage`` are the two actions the graph asks for. Both reach the
  network, so both are the workflow's to run, under limits of their own. A query and a URL
  are small values a model wrote from the run's request; a handler checks a URL before
  anything is fetched (``page_fetch.py``).

Nothing here publishes or spends: searching and reading public pages cost the installation
nothing, and nothing of the product is changed.
"""

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from openmarketer_core.agent_runtime.actions import ActionEffect
from openmarketer_core.agent_runtime.agent import Agent
from openmarketer_core.decisions import DecisionModel
from openmarketer_core.llm import ChatModel
from openmarketer_core.research.questions import ResearchRequest
from openmarketer_core.research.research_agent import rules
from openmarketer_core.research.research_agent.rules import CheckedFinding, Limits, Report
from openmarketer_core.research.workflow_contract import RESEARCH_RUN_KIND, ResearchRunInput

SEARCH_WEB = "search_web"
FETCH_PAGE = "fetch_page"

# How the two actions are tried. Constants of the actions and not settings: an operator sets
# how long the model's work may take (``RESEARCH_TIMEOUT_MINUTES``) and how often it is tried,
# and neither says anything of a search or a fetch.
# - A search of the installation's own SearXNG instance answers in seconds; three attempts
#   with five seconds between them ride out a restarted instance. Repeating a search costs
#   nothing and changes nothing.
# - A fetch may render a page with the headless browser, which can take half a minute; two
#   attempts with ten seconds between them ride out a rendering that died. A repeated fetch
#   of the same URL of the same run stores one page: the handler finds the one that is there.
# - A minute without a heartbeat is a worker that is gone.
SEARCH_TIMEOUT_SECONDS = 30
SEARCH_ATTEMPTS = 3
SEARCH_RETRY_AFTER_SECONDS = 5
FETCH_TIMEOUT_SECONDS = 120
FETCH_ATTEMPTS = 2
FETCH_RETRY_AFTER_SECONDS = 10
FETCH_HEARTBEAT_SECONDS = 30

# A URL the model wrote: bounded, and read as a string only. The handler decides what may be
# fetched; nothing here takes it as a path or an import.
MAX_URL_CHARS = 2048


class ResearchState(TypedDict):
    # The run, as text because a checkpoint stores plain data.
    workspace_id: str
    project_id: str
    run_id: str
    # The request, as it was stored with the run.
    questions: list[str]
    topics: list[str]
    competitors: list[str]
    time_window: str
    max_sources: int
    # What the graph has done: the rounds of queries, what was found, what page ids exist.
    round: int
    queries: list[str]  # every query of every round, in order
    next_query: int  # the query the next search asks for
    categories: list[str]  # the kinds of source of the latest round
    gaps: list[str]  # the questions the latest check left unanswered
    # {"url", "title", "snippet", "query"}: as searches returned them, with the query that found
    # each, and without those the screening dropped. A thread stored before queries were kept
    # has results without "query".
    results: list[dict[str, Any]]
    next_result: int  # the result the next fetch takes
    pages: list[dict[str, Any]]  # {"page_id", "url", "title", "flags"}, fetched pages
    next_page: int  # the page the next reading takes
    readings: list[dict[str, Any]]  # {"page_id", "url", "flags", "reading"}
    drafts: list[dict[str, Any]]  # findings as submitted, before the check
    findings: list[dict[str, Any]]  # checked findings, as the report stores them
    notes: list[str]  # what the code dropped or worked around
    cost_usd: float
    model: str


@dataclass
class ResearchContext:
    """What the nodes of one attempt are given besides the state.

    ``page_text_of`` gives the extracted text of a page the run fetched, from object storage.
    ``decisions`` answers the screening of search results (the role ``screener``). A context
    serves one attempt, in one thread. The models keep what they served, so they are made for
    each attempt with the run's workspace in mind.
    """

    model: ChatModel
    decisions: DecisionModel
    limits: Limits
    page_text_of: Callable[[str], str]


agent = Agent(RESEARCH_RUN_KIND, state=ResearchState, context=ResearchContext)


@agent.action(
    SEARCH_WEB,
    timeout=SEARCH_TIMEOUT_SECONDS,
    attempts=SEARCH_ATTEMPTS,
    retry_after=SEARCH_RETRY_AFTER_SECONDS,
    # Searching reads the installation's own SearXNG instance: nothing is published and
    # nothing is spent, so the policy gate lets it run without a person's approval.
    effect=ActionEffect.INTERNAL,
)
class SearchWeb(BaseModel):
    """Search the web for one query and return the results.

    The query is a small value the model wrote from the run's request; the answer is a list
    of ``{"url", "title", "snippet"}``, each bounded, and nothing else.
    """

    model_config = ConfigDict(extra="forbid")

    workspace_id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID
    query: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=2, max_length=rules.MAX_QUERY_CHARS),
    ]
    # Bounded by what a plan may name, not by what it may search now: a search asked for by
    # a thread stored while "it" was allowed is read back with the categories it was asked with.
    categories: list[str] = Field(default_factory=list, max_length=rules.MAX_PLAN_CATEGORIES)


@agent.action(
    FETCH_PAGE,
    timeout=FETCH_TIMEOUT_SECONDS,
    attempts=FETCH_ATTEMPTS,
    retry_after=FETCH_RETRY_AFTER_SECONDS,
    heartbeat=FETCH_HEARTBEAT_SECONDS,
    effect=ActionEffect.INTERNAL,
)
class FetchPage(BaseModel):
    """Fetch one public page of the run and store it.

    The answer is ``{"page_id", "url", "flags"}``: the identifier the graph keeps, the page's
    address after any redirects, and the injection signs the code found in it. The page's
    text stays in object storage; the graph never carries it.
    """

    model_config = ConfigDict(extra="forbid")

    workspace_id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID
    url: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=8, max_length=MAX_URL_CHARS),
    ]


def initial_state(run: ResearchRunInput, request: ResearchRequest) -> ResearchState:
    """What a research run starts from: the run, its request, and nothing done yet."""
    return {
        "workspace_id": str(run.workspace_id),
        "project_id": str(run.project_id),
        "run_id": str(run.run_id),
        "questions": list(request.questions),
        "topics": list(request.topics),
        "competitors": list(request.competitors),
        "time_window": request.time_window,
        "max_sources": request.max_sources,
        "round": 0,
        "queries": [],
        "next_query": 0,
        "categories": [],
        "gaps": [],
        "results": [],
        "next_result": 0,
        "pages": [],
        "next_page": 0,
        "readings": [],
        "drafts": [],
        "findings": [],
        "notes": [],
        "cost_usd": 0.0,
        "model": "",
    }


def report_of(final_state: Mapping[str, Any]) -> Report:
    """The result of a run whose graph finished: the checked findings and what it cost."""
    return Report(
        questions=list(final_state["questions"]),
        findings=[CheckedFinding.model_validate(finding) for finding in final_state["findings"]],
        notes=list(final_state["notes"]),
        cost_usd=final_state["cost_usd"],
        model=final_state["model"],
    )
