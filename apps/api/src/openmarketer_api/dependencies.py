"""What a request is given: a database session and the services wired at start-up.

``create_app`` builds an application without services; the process entry point
attaches them, and tests override the dependencies below instead. Identity has
its own module (``identity.py``).

Routes are plain functions that FastAPI runs in worker threads, so database
work never blocks the event loop. The one asynchronous thing a route needs,
starting a workflow, is handed to it as a plain function that runs on the
event loop and waits for it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Annotated

from anyio import from_thread
from fastapi import Depends, Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_api.analysis_workflows import AnalysisWorkflows
from openmarketer_core.analysis_workflow import AnalyzeRepositoryInput
from openmarketer_core.db.session import transaction


@dataclass(frozen=True)
class Services:
    """Everything that outlives a request."""

    sessions: sessionmaker[Session]
    analysis_workflows: AnalysisWorkflows


def _services(request: Request) -> Services:
    return request.app.state.services


def sessions(services: Annotated[Services, Depends(_services)]) -> sessionmaker[Session]:
    return services.sessions


def db_session(services: Annotated[Services, Depends(_services)]) -> Iterator[Session]:
    """One transaction per request: committed when the handler returns, rolled back if it raises."""
    with transaction(services.sessions) as session:
        yield session


def analysis_workflows(services: Annotated[Services, Depends(_services)]) -> AnalysisWorkflows:
    return services.analysis_workflows


def start_workflow(
    workflows: Annotated[AnalysisWorkflows, Depends(analysis_workflows)],
) -> Callable[[AnalyzeRepositoryInput], None]:
    """Starting a workflow, as a function a route's worker thread can call."""

    def start(run: AnalyzeRepositoryInput) -> None:
        from_thread.run(workflows.start, run)

    return start


async def no_request_body(request: Request) -> None:
    """Refuse a body on a route that takes none, instead of ignoring what the client sent.

    A client that sends ``{"approved_by": ...}`` to an approval must learn that
    it had no effect.
    """
    if await request.body():
        raise RequestValidationError(
            [{"type": "extra_forbidden", "loc": ("body",), "msg": "this request takes no body"}]
        )


# The transaction ends before the response is sent, so a failed commit is an
# error response and never a success the database did not keep.
DbSession = Annotated[Session, Depends(db_session, scope="function")]
Sessions = Annotated[sessionmaker[Session], Depends(sessions)]
StartWorkflow = Annotated[Callable[[AnalyzeRepositoryInput], None], Depends(start_workflow)]
