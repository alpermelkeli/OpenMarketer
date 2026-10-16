"""Storing a research report and reading it back.

The report is the run's outcome and the record that outlives the pages it
rests on: the questions, the checked findings (each naming the pages and the
quotes it relies on), what the code dropped on the way, the cost and the model.
``save_research_report`` adds it; the worker marks the run succeeded with its
id in the same transaction, so a run and its report appear together or not at
all. ``research_report`` reads a run's report, its asker included (the run
knows who asked), and refuses a run that has none.

Both functions are scoped to a workspace and a project, take a session and
never commit.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from openmarketer_core.db.models import ResearchReportRecord
from openmarketer_core.db.research_runs import research_run
from openmarketer_core.research.research_agent.rules import Report


class ResearchReportNotFound(Exception):
    """The run has no report: it is unfinished, failed, or there is no such run."""

    def __init__(self, project_id: uuid.UUID, run_id: uuid.UUID) -> None:
        super().__init__(f"research run {run_id} of project {project_id} has no report")


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
