"""What a request is given: a database session and the services wired at start-up.

``create_app`` builds an application without services; the process entry point
attaches them, and tests override the dependencies below instead. Identity has
its own module (``identity.py``).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_api.analysis_runs import AnalysisRuns
from openmarketer_core.db.session import transaction


@dataclass(frozen=True)
class Services:
    """Everything that outlives a request."""

    sessions: sessionmaker[Session]
    analysis_runs: AnalysisRuns


def _services(request: Request) -> Services:
    return request.app.state.services


def db_session(services: Annotated[Services, Depends(_services)]) -> Iterator[Session]:
    """One transaction per request: committed when the handler returns, rolled back if it raises."""
    with transaction(services.sessions) as session:
        yield session


def analysis_runs(services: Annotated[Services, Depends(_services)]) -> AnalysisRuns:
    return services.analysis_runs


# The transaction ends before the response is sent, so a failed commit is an
# error response and never a success the database did not keep.
DbSession = Annotated[Session, Depends(db_session, scope="function")]
AnalysisRunsDep = Annotated[AnalysisRuns, Depends(analysis_runs)]
