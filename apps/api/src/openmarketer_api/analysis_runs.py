"""Port: starting the analysis of a project's repository and asking how it went.

An analysis takes minutes and calls a model, so it never runs inside a request.
The API only needs these two operations; where the work runs (a thread today,
a Temporal workflow in the design) is the implementation's business.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from sqlalchemy.orm import Session


class AnalysisStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class AnalysisRun:
    id: uuid.UUID
    workspace_id: uuid.UUID
    project_id: uuid.UUID
    status: AnalysisStatus
    created_at: datetime
    finished_at: datetime | None = None
    error: str | None = None  # why a failed run failed, safe to show to the user
    profile_version: int | None = None  # the draft a succeeded run stored


class AnalysisRunNotFound(Exception):
    """The project has no analysis run with that identifier."""


class AnalysisAlreadyRunning(Exception):
    """The project has an analysis that is not finished yet."""


class AnalysisRuns(Protocol):
    def start(
        self, session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID
    ) -> AnalysisRun:
        """Begin analysing the project's repository and return at once.

        Raises ``ProjectNotFound``, ``IntakeError`` when the project's repository
        is not a remote URL, and ``AnalysisAlreadyRunning``.
        """
        ...

    def get(
        self, *, workspace_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID
    ) -> AnalysisRun:
        """The run as it is now. Raises ``AnalysisRunNotFound``."""
        ...
