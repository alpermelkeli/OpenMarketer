"""What a request is given: a database session and the services wired at start-up.

``create_app`` builds an application without services; the process entry point
attaches them, and tests override the dependencies below instead. Identity has
its own module (``identity.py``).

Routes are plain functions that FastAPI runs in worker threads, so database
work never blocks the event loop. The asynchronous things a route needs,
starting a workflow and telling a run of a decision, are handed to it as plain
functions that run on the event loop and wait for it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Annotated

from anyio import from_thread
from fastapi import Depends, Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_api.analysis_workflows import AnalysisWorkflows
from openmarketer_api.run_signals import RunSignals
from openmarketer_core.credentials.cipher import CredentialCipher
from openmarketer_core.db.session import transaction
from openmarketer_core.repository_analysis.workflow_contract import AnalyzeRepositoryInput


@dataclass(frozen=True)
class Services:
    """Everything that outlives a request."""

    sessions: sessionmaker[Session]
    analysis_workflows: AnalysisWorkflows
    run_signals: RunSignals
    # The process environment, for core to see which credentials it sets. Kept out of
    # the repr, which would print every secret in it.
    environment: Mapping[str, str] = field(repr=False)
    # None when the installation has no ENCRYPTION_KEY.
    credential_cipher: CredentialCipher | None


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


def run_signals(services: Annotated[Services, Depends(_services)]) -> RunSignals:
    return services.run_signals


def tell_run(
    signals: Annotated[RunSignals, Depends(run_signals)],
) -> Callable[[str, uuid.UUID], None]:
    """Telling a run of a decision, as a function a route's worker thread can call."""

    def tell(workflow_id: str, proposal_id: uuid.UUID) -> None:
        from_thread.run(signals.tell_decided, workflow_id, proposal_id)

    return tell


def environment(services: Annotated[Services, Depends(_services)]) -> Mapping[str, str]:
    return services.environment


def credential_cipher(
    services: Annotated[Services, Depends(_services)],
) -> CredentialCipher | None:
    return services.credential_cipher


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
Environment = Annotated[Mapping[str, str], Depends(environment)]
StoringCipher = Annotated[CredentialCipher | None, Depends(credential_cipher)]
StartWorkflow = Annotated[Callable[[AnalyzeRepositoryInput], None], Depends(start_workflow)]
TellRun = Annotated[Callable[[str, uuid.UUID], None], Depends(tell_run)]
