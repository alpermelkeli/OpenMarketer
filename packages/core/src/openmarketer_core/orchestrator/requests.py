"""What a chat turn may ask of the process that runs it, besides reading: start a run.

The chat agent's read tools answer from the project's records and change nothing; a *request
action* is the other kind the design gives it (the report's "Read tools and request actions
are separate"): it asks whoever runs the turn to create a record and start a run. This module
is the port such a tool reaches (``ChatRequests``) and the ways it fails
(``ResearchNotStarted``, ``StudioNotStarted``). Two kinds of run can be started: a research
run and a run of the creative studio, which makes one image. The adapter is the API's
(``openmarketer_api.chat_requests``), which holds the database and the workflow server; a
turn whose caller gave no port answers that starting runs is not available.

The answer is the run as the chat reads runs (``ResearchRunReading``, ``StudioRunReading``),
so a turn can say the new run's id and status at once; what the run finds is read later with
the read tools. This module imports no database, workflow engine or framework, and neither
does the chat agent: the port answers in plain records.

A request is read strictly before it reaches the port (``chat_agent.tools``), against the same
``ResearchRequest`` or ``AssetRequest`` a person's request is: the port never sees what did
not fit.
"""

from __future__ import annotations

from typing import Protocol

from openmarketer_core.orchestrator.reads import ResearchRunReading, StudioRunReading
from openmarketer_core.research.questions import ResearchRequest
from openmarketer_core.studio.requests import AssetRequest


class ResearchNotStarted(Exception):
    """The turn's caller could not start a research run: nothing is running for it.

    A caller that stored a run whose workflow could not be started passes the sentence that
    says so; it holds no value. The tool answers with it in words, so the operator learns why
    nothing will come of the request.
    """

    def __init__(self, reason: str = "the research could not be started") -> None:
        super().__init__(reason)


class StudioNotStarted(Exception):
    """The turn's caller could not start a studio run: nothing is running for it.

    Raised with a sentence of the caller's that holds no value, for the tool to answer with:
    a run whose workflow could not be started, or a reference asset the project does not have.
    """

    def __init__(self, reason: str = "the image could not be started") -> None:
        super().__init__(reason)


class ChatRequests(Protocol):
    """What a chat turn may ask the process that runs it to do, besides reading."""

    def start_research(self, request: ResearchRequest) -> ResearchRunReading:
        """Create a research run for the turn's project and start its workflow.

        Answers with the run as it is now, newly queued. Raises ``ResearchNotStarted`` when a
        run was stored but no workflow could be started for it (it is recorded as failed by
        then); ``ProjectNotFound`` when the turn's project is gone while it is answered.
        """
        ...

    def start_creative(self, request: AssetRequest) -> StudioRunReading:
        """Create a studio run for the turn's project and start its workflow.

        Answers with the run as it is now, newly queued. Raises ``StudioNotStarted`` when a
        reference asset is not the project's (nothing is stored then) or a run was stored but
        no workflow could be started for it (it is recorded as failed by then);
        ``ProjectNotFound`` when the turn's project is gone while it is answered.
        """
        ...
