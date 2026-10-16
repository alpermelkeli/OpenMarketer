"""What the chat agent may read of a project: one port and its records, and no database.

The orchestrator's chat answers the operator's message about one project from what the
product has stored. This module says what the chat may ask for (``ChatReads``) and what it
is given (the frozen records below); ``reads_postgres.py`` answers from PostgreSQL.

Every method is about one workspace and one project, which the adapter holds: the graph
never names either, so it cannot read beyond the project its turn is about. A reading that
does not exist is ``None`` and is not an error; an identifier or a section that does not
exist is refused, because a person could not have typed it.

This module imports no session and reaches no database: only the vocabulary of what was
stored. The records are plain data, so a test can fake the port.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from openmarketer_core.agent_runtime.actions import ActionEffect
from openmarketer_core.agent_runtime.gate import ProposalStatus
from openmarketer_core.agent_runtime.model_input import shown
from openmarketer_core.db.models import AnalysisRunStatus, ProfileStatus, ProjectStatus

# The sections of a Product Profile (the fields of ``profile.ProductProfile``), in order.
PROFILE_SECTIONS: tuple[str, ...] = (
    "product",
    "features",
    "brand",
    "audience",
    "business_model",
    "measurement",
)

# The most records a reading returns, however many are asked for.
MAX_LISTED_RECORDS = 20


class UnknownProfileSection(ValueError):
    """A section of the profile was asked for that the profile does not have."""

    def __init__(self, section: str) -> None:
        super().__init__(
            f"there is no section {shown(section)} in the Product Profile; the sections "
            f"are: {', '.join(PROFILE_SECTIONS)}"
        )
        self.section = section


@dataclass(frozen=True)
class PendingProposals:
    """The proposals of the project that wait for a person: how many, and the nearest deadline."""

    count: int
    next_expiry: datetime | None


@dataclass(frozen=True)
class ProfileReading:
    """One version of the project's Product Profile, without its whole content.

    ``content`` is the asked-for section as JSON data when a section was asked for, and
    ``None`` otherwise.
    """

    version: int
    status: ProfileStatus
    commit_sha: str | None
    edited_from_version: int | None
    created_at: datetime
    approved_at: datetime | None
    content: object | None


@dataclass(frozen=True)
class ProposalReading:
    """One action the policy gate held for a person: what was asked and where it stands."""

    id: uuid.UUID
    action_name: str
    effect: ActionEffect
    payload: dict[str, Any]
    status: ProposalStatus
    run_kind: str
    run_id: uuid.UUID
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None


@dataclass(frozen=True)
class RunReading:
    """One analysis run of the project and how far it got."""

    id: uuid.UUID
    status: AnalysisRunStatus
    error: str | None
    profile_version: int | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True)
class ProjectStatusReading:
    """Where the project stands: what it is, its review state, and its latest work."""

    project_id: uuid.UUID
    name: str
    repository_url: str
    status: ProjectStatus
    autonomy_level: int
    product_url: str | None
    latest_approved_version: int | None
    latest_draft_version: int | None
    unfinished_run_id: uuid.UUID | None
    latest_run: RunReading | None
    pending: PendingProposals


class ChatReads(Protocol):
    """What the chat agent may read of its project, answered from where the product stores it."""

    def project_status(self) -> ProjectStatusReading:
        """The project with where its review stands and its latest run."""
        ...

    def profile(self, section: str | None = None) -> ProfileReading | None:
        """The latest approved profile, or the latest draft while nothing is approved.

        With ``section``, that section's content only. Raises ``UnknownProfileSection`` for a
        section the profile does not have.
        """
        ...

    def pending_proposals(self) -> Sequence[ProposalReading]:
        """The proposals that wait for a person, newest first, at most ``MAX_LISTED_RECORDS``."""
        ...

    def proposal(self, proposal_id: uuid.UUID) -> ProposalReading | None:
        """One proposal of the project, whatever it became, or ``None`` if there is none."""
        ...

    def run(self, run_id: uuid.UUID) -> RunReading | None:
        """One analysis run of the project, or ``None`` if there is none."""
        ...

    def recent_runs(self, limit: int) -> Sequence[RunReading]:
        """The project's analysis runs, newest first, at most ``limit`` records.

        ``MAX_LISTED_RECORDS`` bounds it further.
        """
        ...
