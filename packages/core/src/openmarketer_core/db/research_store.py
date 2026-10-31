"""Storing a research report and reading it back.

The report is the run's outcome and the record that outlives the pages it
rests on: the questions, the checked findings (each naming the pages and the
quotes it relies on), what the code dropped on the way, the cost and the model.
``save_research_report`` adds it; the worker marks the run succeeded with its
id in the same transaction, so a run and its report appear together or not at
all. ``research_report`` reads a run's report, its asker included (the run
knows who asked), and refuses a run that has none.

A post may draw on the findings of a research run that succeeded
(``succeeded_research_run``). ``research_findings_for_post`` is what the writer
is shown of them: each finding's statement, confidence and injection flag with
the address and quotes of its sources, read back strictly and bounded by the
caller, since every word of it came from public pages. Which findings a post may
use, and how, is the writer's rule; this module returns them in the report's
order and leaves nothing out but what the bounds cut.

Every function is scoped to a workspace and a project, takes a session and
never commits. A research run a post names is not deleted
(``db/research_runs.py``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from sqlalchemy.orm import Session

from openmarketer_core.db.models import ResearchReportRecord, ResearchRunStatus
from openmarketer_core.db.research_runs import ResearchRun, research_run
from openmarketer_core.research.research_agent.rules import (
    MAX_FINDING_SOURCES,
    MAX_QUOTE_CHARS,
    MAX_READING_QUOTES,
    MAX_STATEMENT_CHARS,
    Report,
)


class ResearchReportNotFound(Exception):
    """The run has no report: it is unfinished, failed, or there is no such run."""

    def __init__(self, project_id: uuid.UUID, run_id: uuid.UUID) -> None:
        super().__init__(f"research run {run_id} of project {project_id} has no report")


class ResearchRunNotUsable(Exception):
    """The research run has not succeeded, so a post has no findings of it to draw on."""

    def __init__(self, run_id: uuid.UUID, status: ResearchRunStatus) -> None:
        self.run_id = run_id
        self.status = status
        super().__init__(f"research run {run_id} is {status.value}; a post uses a succeeded one")


class StoredResearchReportInvalid(Exception):
    """A finding stored with the run's report does not fit the shape it was stored in."""

    def __init__(self, run_id: uuid.UUID) -> None:
        self.run_id = run_id
        super().__init__(f"the stored findings of research run {run_id} do not fit their schema")


@dataclass(frozen=True)
class FindingSource:
    """A page a finding rests on: its address and the quotes copied from it."""

    url: str
    quotes: tuple[str, ...]


@dataclass(frozen=True)
class FindingForPost:
    """One checked finding as the writer is shown it. ``flagged``: every page it rests on
    raised an injection sign."""

    statement: str
    confidence: float
    flagged: bool
    sources: tuple[FindingSource, ...]


@dataclass(frozen=True)
class ResearchFindingsForPost:
    """The findings of a succeeded run a post may draw on, bounded.

    ``left_out`` is how many findings the report holds beyond ``limit``.
    """

    run_id: uuid.UUID
    questions: tuple[str, ...]
    findings: tuple[FindingForPost, ...]
    left_out: int


@dataclass(frozen=True)
class ResearchReport:
    """A stored report: its findings as data, and what it cost."""

    id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID
    requested_by: str
    questions: list[str]
    findings: list[dict[str, Any]]
    notes: list[str]
    cost_usd: float
    model: str
    created_at: datetime


def save_research_report(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    report: Report,
) -> ResearchReport:
    """Store the report of the run; the report, with its id.

    Raises ``ResearchRunNotFound`` when the workspace has no such run of the
    project.
    """
    run = research_run(session, workspace_id=workspace_id, project_id=project_id, run_id=run_id)
    record = ResearchReportRecord(
        project_id=project_id,
        questions=list(report.questions),
        findings=[finding.model_dump(mode="json") for finding in report.findings],
        notes=list(report.notes),
        cost_usd=report.cost_usd,
        model=report.model,
    )
    session.add(record)
    session.flush()
    return _report(record, run_id=run_id, requested_by=run.requested_by)


def research_report(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID
) -> ResearchReport:
    """The run's report.

    Raises ``ResearchRunNotFound`` when the workspace has no such run of the
    project, and ``ResearchReportNotFound`` when the run has no report.
    """
    run = research_run(session, workspace_id=workspace_id, project_id=project_id, run_id=run_id)
    if run.report_id is None:
        raise ResearchReportNotFound(project_id, run_id)
    record = session.get(ResearchReportRecord, run.report_id)
    if record is None:
        raise ResearchReportNotFound(project_id, run_id)
    return _report(record, run_id=run_id, requested_by=run.requested_by)


def _report(
    record: ResearchReportRecord, *, run_id: uuid.UUID, requested_by: str
) -> ResearchReport:
    return ResearchReport(
        id=record.id,
        project_id=record.project_id,
        run_id=run_id,
        requested_by=requested_by,
        questions=list(record.questions),
        findings=list(record.findings),
        notes=list(record.notes),
        cost_usd=record.cost_usd,
        model=record.model,
        created_at=record.created_at,
    )


def succeeded_research_run(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID
) -> ResearchRun:
    """The run, if it succeeded, so a post can draw on its findings.

    Raises ``ResearchRunNotFound`` when the workspace has no such run of the project, and
    ``ResearchRunNotUsable`` when it has not succeeded.
    """
    run = research_run(session, workspace_id=workspace_id, project_id=project_id, run_id=run_id)
    if run.status is not ResearchRunStatus.SUCCEEDED:
        raise ResearchRunNotUsable(run_id, run.status)
    return run


def research_findings_for_post(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    limit: int,
    quotes_per_source: int,
) -> ResearchFindingsForPost:
    """The first ``limit`` findings of the run's report, each source with at most
    ``quotes_per_source`` of its quotes.

    Raises ``ResearchRunNotFound``, ``ResearchRunNotUsable`` (as ``succeeded_research_run``),
    ``ResearchReportNotFound``, and ``StoredResearchReportInvalid`` when a stored finding no
    longer fits its shape.
    """
    if limit < 1 or quotes_per_source < 0:
        raise ValueError("a post is shown at least one finding and no negative number of quotes")
    run = succeeded_research_run(
        session, workspace_id=workspace_id, project_id=project_id, run_id=run_id
    )
    record = session.get(ResearchReportRecord, run.report_id) if run.report_id else None
    if record is None or record.project_id != project_id:
        raise ResearchReportNotFound(project_id, run_id)
    try:
        stored = [_StoredFinding.model_validate(finding) for finding in record.findings]
    except ValidationError as e:
        raise StoredResearchReportInvalid(run_id) from e
    return ResearchFindingsForPost(
        run_id=run_id,
        questions=tuple(record.questions),
        findings=tuple(finding.for_post(quotes_per_source) for finding in stored[:limit]),
        left_out=max(0, len(stored) - limit),
    )


# The longest address a page is fetched from (``research/page_fetch.py``), which this adapter
# does not import.
_MAX_URL_CHARS = 2048
_Quote = Annotated[str, StringConstraints(min_length=1, max_length=MAX_QUOTE_CHARS)]


class _StoredSource(BaseModel):
    """A source as ``CheckedFinding.sources`` stores it."""

    model_config = ConfigDict(extra="forbid")

    page_id: uuid.UUID
    url: Annotated[str, StringConstraints(min_length=1, max_length=_MAX_URL_CHARS)]
    quotes: list[_Quote] = Field(max_length=MAX_READING_QUOTES)


class _StoredFinding(BaseModel):
    """A finding as the report stores it (``CheckedFinding``), its sources read strictly."""

    model_config = ConfigDict(extra="forbid")

    question_index: int
    statement: Annotated[str, StringConstraints(min_length=1, max_length=MAX_STATEMENT_CHARS)]
    confidence: float = Field(ge=0, le=1)
    flagged: bool
    sources: list[_StoredSource] = Field(max_length=MAX_FINDING_SOURCES)

    def for_post(self, quotes_per_source: int) -> FindingForPost:
        return FindingForPost(
            statement=self.statement,
            confidence=self.confidence,
            flagged=self.flagged,
            sources=tuple(
                FindingSource(url=source.url, quotes=tuple(source.quotes[:quotes_per_source]))
                for source in self.sources
            ),
        )
