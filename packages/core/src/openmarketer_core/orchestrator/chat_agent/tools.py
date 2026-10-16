"""The chat agent's tools: what the model may read of its project, and the one request action.

The read tools answer with bounded JSON text built from the records ``ChatReads`` returns,
and never with what a model sent. Problems (a section that does not exist, an identifier
that does not name anything) are text the model can correct from. No read tool writes
anything or asks for an action.

``start_research`` is the other kind: a *request action*. It asks the turn's caller
(``ChatTurnContext.requests``) to create a research run for the project and start it, and
answers with the run's id and status; it writes nothing itself and starts no workflow, and a
caller that gave no port is told that starting runs is not available. What it sends is read
strictly against ``ResearchRequest`` first, so a request that does not fit is refused with
the fields at fault and never their values.

The tools are declared on the agent here; importing this module is what puts them on it.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from typing import Any

from openmarketer_core.agent_runtime.model_input import (
    DoesNotFit,
    fields_phrase,
    read_strict_json,
    shown,
)
from openmarketer_core.orchestrator.chat_agent.definitions import ChatTurnContext, agent
from openmarketer_core.orchestrator.reads import (
    MAX_LISTED_RECORDS,
    PROFILE_SECTIONS,
    UnknownProfileSection,
)
from openmarketer_core.orchestrator.requests import ResearchNotStarted
from openmarketer_core.research.questions import DEFAULT_MAX_SOURCES, ResearchRequest

# The most a tool answer is; a profile with many features is the long one.
MAX_ANSWER_CHARS = 6000
TRUNCATED = "\n... truncated"


@agent.tool
def get_project_status(context: ChatTurnContext) -> str:
    """Where the project stands now: its profile versions, its latest analysis run, and the
    proposals that wait for a person. Start here."""
    return _text(asdict(context.reads.project_status()))


@agent.tool
def read_profile(context: ChatTurnContext, section: str | None = None) -> str:
    """The Product Profile, the structured description of the product. Without a section it
    lists the sections; with one, that section's content. It reads the latest approved
    profile, or the latest draft while nothing is approved."""
    if section is not None and section not in PROFILE_SECTIONS:
        return f"error: there is no section {shown(section)}; the sections are: " + ", ".join(
            PROFILE_SECTIONS
        )
    try:
        reading = context.reads.profile(section)
    except UnknownProfileSection as e:
        return f"error: {e}"
    if reading is None:
        return "The project has no Product Profile yet."
    if section is None:
        return _text(
            {
                "version": reading.version,
                "status": reading.status,
                "approved_at": reading.approved_at,
                "sections": list(PROFILE_SECTIONS),
            }
        )
    return _text(
        {
            "version": reading.version,
            "status": reading.status,
            "section": section,
            "content": reading.content,
        }
    )


@agent.tool
def list_pending_proposals(context: ChatTurnContext) -> str:
    """The proposals that wait for a person's decision: the actions agents asked for that the
    policy gate held. Each has an id for explain_proposal."""
    proposals = context.reads.pending_proposals()
    if not proposals:
        return "No proposals wait for a person."
    return _text([asdict(proposal) for proposal in proposals])


@agent.tool
def explain_proposal(context: ChatTurnContext, proposal_id: str) -> str:
    """One proposal in full: what it asks to do, which run asked, its effect and its deadline.
    proposal_id comes from list_pending_proposals or the operator's message."""
    wanted = _uuid_of(proposal_id)
    if wanted is None:
        return "error: proposal_id must be a UUID; take one from list_pending_proposals"
    found = context.reads.proposal(wanted)
    if found is None:
        return f"error: no proposal {wanted} in this project"
    return _text(asdict(found))


@agent.tool
def get_analysis_run(context: ChatTurnContext, run_id: str) -> str:
    """One analysis run: its status, its times, and the profile version it stored or why it
    failed. run_id comes from list_analysis_runs or the operator's message."""
    wanted = _uuid_of(run_id)
    if wanted is None:
        return "error: run_id must be a UUID; take one from list_analysis_runs"
    found = context.reads.run(wanted)
    if found is None:
        return f"error: no run {wanted} in this project"
    return _text(asdict(found))


@agent.tool
def list_analysis_runs(context: ChatTurnContext, limit: int = 10) -> str:
    """The project's recent analysis runs, newest first, at most limit of them. Each has an
    id for get_analysis_run."""
    runs = context.reads.recent_runs(max(1, min(limit, MAX_LISTED_RECORDS)))
    if not runs:
        return "The project has no analysis runs."
    return _text([asdict(run) for run in runs])


@agent.tool
def list_research_runs(context: ChatTurnContext, limit: int = 10) -> str:
    """The project's recent research runs, newest first, at most limit of them: the questions
    they answer, who asked for them, whether each has a report, and why one failed. Each has
    an id for read_research_report."""
    runs = context.reads.recent_research(max(1, min(limit, MAX_LISTED_RECORDS)))
    if not runs:
        return "The project has no research runs."
    return _text([asdict(run) for run in runs])


@agent.tool
def read_research_report(context: ChatTurnContext, run_id: str = "") -> str:
    """The findings of a research run: every statement with its confidence and the pages it
    rests on (their address and the quotes from them), the questions it answered, and the
    notes of what the code dropped. Without run_id, the latest run that has a report.
    run_id comes from list_research_runs."""
    wanted: uuid.UUID | None = None
    if run_id:
        wanted = _uuid_of(run_id)
        if wanted is None:
            return "error: run_id must be a UUID; take one from list_research_runs"
    report = context.reads.research_report(wanted)
    if report is None:
        if wanted is None:
            return "No research run of this project has a report yet."
        return f"error: research run {wanted} has no report"
    return _text(asdict(report))


@agent.tool
def start_research(
    context: ChatTurnContext,
    questions: list[str],
    topics: list[str] | None = None,
    competitors: list[str] | None = None,
    time_window: str = "",
    max_sources: int = DEFAULT_MAX_SOURCES,
) -> str:
    """Start a research run of this project: it answers the questions from public pages and
    stores a report whose findings and sources read_research_report returns once it is done.
    Use it only when the operator asks for research, and give the run's id in the answer. It
    answers with the new run's id and status; the run itself is read later."""
    if context.requests is None:
        return "error: starting a research run is not available here"
    try:
        request = read_strict_json(
            ResearchRequest,
            {
                "questions": questions,
                "topics": topics or [],
                "competitors": competitors or [],
                "time_window": time_window,
                "max_sources": max_sources,
            },
        )
    except DoesNotFit as e:
        return f"error: the research request does not fit{fields_phrase(e.fields)}"
    try:
        run = context.requests.start_research(request)
    except ResearchNotStarted as e:
        return f"error: {e}"
    return _text({"run_id": str(run.id), "status": run.status, "requested_by": run.requested_by})


def _uuid_of(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _text(value: Any) -> str:
    """The reading as the model reads it: JSON, bounded."""
    text = json.dumps(value, default=str, ensure_ascii=False)
    if len(text) > MAX_ANSWER_CHARS:
        return text[:MAX_ANSWER_CHARS] + TRUNCATED
    return text
