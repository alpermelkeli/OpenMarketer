"""The chat agent's tools: what the model may read of its project, and the request actions.

The read tools answer with bounded JSON text built from the records ``ChatReads`` returns,
and never with what a model sent. Problems (a section that does not exist, an identifier
that does not name anything) are text the model can correct from. No read tool writes
anything or asks for an action.

``start_research``, ``start_creative`` and ``start_writing`` are the other kind: *request
actions*. Each asks the turn's caller (``ChatTurnContext.requests``) to create a run for the
project and start it (a research run, a studio run that makes one image, a writing run that
writes one post and proposes it), and answers with the run's id and status; it writes nothing
itself and starts no workflow, and a caller that gave no port is told that starting runs is
not available. A run that was started is recorded for the person watching the turn
(``run_started``, through ``ChatTurnContext.events``), so the chat can follow the run's own
progress. What it sends is read strictly against ``ResearchRequest``, ``AssetRequest`` or
``WritingBrief`` first, so a request that does not fit is refused with the fields at fault
and never their values. A brief's optional fields a model sent as empty text (a weak model
copies the defaults back) are read as not given.

Before a post is asked for, code holds the brief to the approved profile, as the writer's
``prepare`` will: there must be one, and the feature an announcement names must be one of its
live features (``rules.feature_not_announceable``). The writer checks again when it runs; the
check here only spares the operator a run that would fail at once.

Everything a read tool answers with that a person, a model or a web page wrote (a research
finding, a studio run's goal and copy, the verifier's notes) is the project's data, and the
system prompt tells the model to read it as such.

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
from openmarketer_core.db.models import ProfileStatus
from openmarketer_core.orchestrator.chat_agent.definitions import ChatTurnContext, agent
from openmarketer_core.orchestrator.chat_agent.rules import (
    feature_not_announceable,
    term_not_to_mention,
)
from openmarketer_core.orchestrator.reads import (
    MAX_LISTED_RECORDS,
    PROFILE_SECTIONS,
    UnknownProfileSection,
)
from openmarketer_core.orchestrator.requests import (
    ResearchNotStarted,
    StudioNotStarted,
    WritingNotStarted,
)
from openmarketer_core.research.questions import DEFAULT_MAX_SOURCES, ResearchRequest
from openmarketer_core.research.workflow_contract import RESEARCH_RUN_KIND
from openmarketer_core.run_events import RUN_STARTED, RunEvent
from openmarketer_core.studio.requests import AssetRequest
from openmarketer_core.studio.workflow_contract import STUDIO_RUN_KIND
from openmarketer_core.writing.brief import DEFAULT_VARIANTS, WritingBrief, WritingPlatform
from openmarketer_core.writing.workflow_contract import WRITING_RUN_KIND

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
    policy gate held. Each has an id whose full detail you can read."""
    proposals = context.reads.pending_proposals()
    if not proposals:
        return "No proposals wait for a person."
    return _text([asdict(proposal) for proposal in proposals])


@agent.tool
def explain_proposal(context: ChatTurnContext, proposal_id: str) -> str:
    """One proposal in full: what it asks to do, which run asked, its effect and its deadline.
    proposal_id comes from a listed pending proposal or the operator's message."""
    wanted = _uuid_of(proposal_id)
    if wanted is None:
        return "error: proposal_id must be a UUID; take one from a listed pending proposal"
    found = context.reads.proposal(wanted)
    if found is None:
        return f"error: no proposal {wanted} in this project"
    return _text(asdict(found))


@agent.tool
def get_analysis_run(context: ChatTurnContext, run_id: str) -> str:
    """One analysis run: its status, its times, and the profile version it stored or why it
    failed. run_id comes from a listed analysis run or the operator's message."""
    wanted = _uuid_of(run_id)
    if wanted is None:
        return "error: run_id must be a UUID; take one from a listed analysis run"
    found = context.reads.run(wanted)
    if found is None:
        return f"error: no run {wanted} in this project"
    return _text(asdict(found))


@agent.tool
def list_analysis_runs(context: ChatTurnContext, limit: int = 10) -> str:
    """The project's recent analysis runs, newest first, at most limit of them. Each has an
    id whose run you can read in full."""
    runs = context.reads.recent_runs(max(1, min(limit, MAX_LISTED_RECORDS)))
    if not runs:
        return "The project has no analysis runs."
    return _text([asdict(run) for run in runs])


@agent.tool
def list_research_runs(context: ChatTurnContext, limit: int = 10) -> str:
    """The project's recent research runs, newest first, at most limit of them: the questions
    they answer, who asked for them, whether each has a report, and why one failed. Each has
    an id whose report you can read."""
    runs = context.reads.recent_research(max(1, min(limit, MAX_LISTED_RECORDS)))
    if not runs:
        return "The project has no research runs."
    return _text([asdict(run) for run in runs])


@agent.tool
def read_research_report(context: ChatTurnContext, run_id: str = "") -> str:
    """The findings of a research run: every statement with its confidence and the pages it
    rests on (their address and the quotes from them), the questions it answered, and the
    notes of what the code dropped. Without run_id, the latest run that has a report.
    run_id comes from a listed research run."""
    wanted: uuid.UUID | None = None
    if run_id:
        wanted = _uuid_of(run_id)
        if wanted is None:
            return "error: run_id must be a UUID; take one from a listed research run"
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
    stores a report whose findings and sources can be read once it is done. Use it only when
    the operator asks for research, and give the run's id in the answer. It answers with the
    new run's id and status; the report is read later."""
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
    context.events.record(
        RunEvent(RUN_STARTED, {"run_kind": RESEARCH_RUN_KIND, "run_id": str(run.id)})
    )
    return _text({"run_id": str(run.id), "status": run.status, "requested_by": run.requested_by})


@agent.tool
def list_studio_runs(context: ChatTurnContext, limit: int = 10) -> str:
    """The project's recent studio runs, newest first, at most limit of them: the image each
    was asked for (goal, platform, exact copy), its status, what it cost, the asset it ended
    with in the Studio library, and why one failed. Each has an id whose run you can read in
    full with get_studio_run."""
    runs = context.reads.recent_studio_runs(max(1, min(limit, MAX_LISTED_RECORDS)))
    if not runs:
        return "The project has no studio runs."
    return _text([asdict(run) for run in runs])


@agent.tool
def get_studio_run(context: ChatTurnContext, run_id: str) -> str:
    """One studio run in full: what it was asked for, its status and cost, the asset it ended
    with, what the checks and the visual verifier found of that image, and why a run that
    needs review was handed to a person. run_id comes from a listed studio run."""
    wanted = _uuid_of(run_id)
    if wanted is None:
        return "error: run_id must be a UUID; take one from a listed studio run"
    found = context.reads.studio_run(wanted)
    if found is None:
        return f"error: no studio run {wanted} in this project"
    return _text(asdict(found))


@agent.tool
def list_media_assets(context: ChatTurnContext, limit: int = 10) -> str:
    """The project's Studio library, newest first, at most limit images: each with the run
    that made it, its platform, and whether a person accepted it, rejected it or has not
    decided yet (draft)."""
    assets = context.reads.media_assets(max(1, min(limit, MAX_LISTED_RECORDS)))
    if not assets:
        return "The project's Studio library is empty."
    return _text([asdict(asset) for asset in assets])


@agent.tool
def start_creative(
    context: ChatTurnContext,
    goal: str,
    platform: str,
    exact_copy: list[str] | None = None,
    style_notes: str = "",
    reference_asset_ids: list[str] | None = None,
) -> str:
    """Start a studio run of this project: it makes one image for one platform and stores it
    in the project's Studio library for a person to accept. Use it only when the operator asks
    for an image. platform is one of: instagram_post, instagram_portrait, instagram_story,
    x_post, linkedin_post, og_image, app_store_screenshot.
    exact_copy is every line of text the image must show: the operator's words verbatim when
    they gave some, otherwise at most two short lines you wrote (a headline and an optional
    subline); empty for an image without text. style_notes are the operator's wishes on the
    look, or your own art direction where they gave none; goal states the idea of the image.
    reference_asset_ids name images of the Studio library to look at. A copy that names a
    term the brand says not to mention is refused. It answers with the new run's id and
    status; the image is read later."""
    if context.requests is None:
        return "error: starting a studio run is not available here"
    try:
        request = read_strict_json(
            AssetRequest,
            {
                "goal": goal,
                "platform": platform,
                "exact_copy": exact_copy or [],
                "style_notes": style_notes,
                "reference_asset_ids": reference_asset_ids or [],
            },
        )
    except DoesNotFit as e:
        return f"error: the image request does not fit{fields_phrase(e.fields)}"
    banned = term_not_to_mention(request.exact_copy, _do_not_mention(context))
    if banned is not None:
        return (
            f"error: exact_copy names {shown(banned)}, which the Product Profile's brand says "
            "not to mention; write the copy without it"
        )
    try:
        run = context.requests.start_creative(request)
    except StudioNotStarted as e:
        return f"error: {e}"
    context.events.record(
        RunEvent(RUN_STARTED, {"run_kind": STUDIO_RUN_KIND, "run_id": str(run.id)})
    )
    return _text({"run_id": str(run.id), "status": run.status, "platform": run.platform})


@agent.tool
def list_writing_runs(context: ChatTurnContext, limit: int = 10) -> str:
    """The project's recent writing runs, newest first, at most limit of them: the brief each
    was asked (goal, feature, angle, variants, the image and the research it was given), its
    status, what it cost, who asked for it and why one failed. Each has an id whose run and
    post you can read with get_writing_run."""
    runs = context.reads.recent_writing_runs(max(1, min(limit, MAX_LISTED_RECORDS)))
    if not runs:
        return "The project has no writing runs."
    return _text([asdict(run) for run in runs])


@agent.tool
def get_writing_run(context: ChatTurnContext, run_id: str) -> str:
    """One writing run in full: its brief, status and cost, and the post it proposed: the
    text, whether the critic passed it, whether it waits for a person (with the proposal a
    person decides it through) or how it was settled. run_id comes from a listed writing
    run."""
    wanted = _uuid_of(run_id)
    if wanted is None:
        return "error: run_id must be a UUID; take one from a listed writing run"
    found = context.reads.writing_run(wanted)
    if found is None:
        return f"error: no writing run {wanted} in this project"
    return _text(asdict(found))


@agent.tool
def start_writing(
    context: ChatTurnContext,
    goal: str,
    feature: str | None = None,
    angle: str | None = None,
    variants: int = DEFAULT_VARIANTS,
    asset: str | None = None,
    research_run_id: str | None = None,
) -> str:
    """Start a writing run of this project: it writes one post for X from the approved
    Product Profile and proposes it. The post waits in the project's Approvals until a person
    approves or rejects it; nothing is sent to X (dry run). Use it only when the operator asks
    for a post or a tweet, or to introduce or announce the product.
    goal is one of: introduce (the product as a whole), announce (one live feature, whose id
    goes in feature), angle (a direction, which goes in angle: the operator's words verbatim
    when they gave some). feature is given only to announce and angle only for an angle.
    variants is how many texts the writer offers, 1 to 5. asset is the id of an image of the
    Studio library the post goes with (not a rejected one); research_run_id a succeeded
    research run whose findings the post may use. Give either only when the operator refers to
    one. It answers with the new run's id and status; the post is read later."""
    if context.requests is None:
        return "error: starting a writing run is not available here"
    try:
        brief = read_strict_json(
            WritingBrief,
            {
                "platform": WritingPlatform.X.value,
                "goal": goal,
                "feature": _given(feature),
                "angle": _given(angle),
                "variants": variants,
                "asset": _given(asset),
                "research_run_id": _given(research_run_id),
            },
        )
    except DoesNotFit as e:
        return f"error: the post's brief does not fit{fields_phrase(e.fields)}"
    problem = _brief_problem(context, brief)
    if problem is not None:
        return f"error: {problem}"
    try:
        run = context.requests.start_writing(brief)
    except WritingNotStarted as e:
        return f"error: {e}"
    context.events.record(
        RunEvent(RUN_STARTED, {"run_kind": WRITING_RUN_KIND, "run_id": str(run.id)})
    )
    return _text({"run_id": str(run.id), "status": run.status, "platform": run.platform})


def _given(value: str | None) -> str | None:
    """An optional field of a brief, ``None`` when a model sent it empty."""
    if value is None or not value.strip():
        return None
    return value


def _brief_problem(context: ChatTurnContext, brief: WritingBrief) -> str | None:
    """Why the writer would refuse ``brief`` at once, read from the approved profile."""
    reading = context.reads.profile("features")
    if reading is None or reading.status != ProfileStatus.APPROVED:
        return (
            "the project has no approved Product Profile, and a post is written from one; "
            "tell the operator to approve the profile first"
        )
    if brief.feature is None:
        return None
    return feature_not_announceable(brief.feature, reading.content)


def _do_not_mention(context: ChatTurnContext) -> list[str]:
    """The terms the approved profile's brand says not to mention; none without one."""
    reading = context.reads.profile("brand")
    if reading is None or reading.status != ProfileStatus.APPROVED:
        return []
    brand = reading.content if isinstance(reading.content, dict) else {}
    terms = brand.get("do_not_mention")
    return [term for term in terms if isinstance(term, str)] if isinstance(terms, list) else []


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
