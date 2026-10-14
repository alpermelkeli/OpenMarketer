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

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Annotated

from anyio import from_thread
from fastapi import Depends, Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_api.workflows import Workflows
from openmarketer_core.credentials.cipher import CredentialCipher
from openmarketer_core.db.session import transaction
from openmarketer_core.workflow_start import WorkflowStart


@dataclass(frozen=True)
class Services:
    """Everything that outlives a request."""

    sessions: sessionmaker[Session]
    workflows: Workflows
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


def workflows(services: Annotated[Services, Depends(_services)]) -> Workflows:
    return services.workflows


def start_workflow(
    workflows: Annotated[Workflows, Depends(workflows)],
) -> Callable[[WorkflowStart], None]:
    """Starting a workflow, as a function a route's worker thread can call."""

    def start(workflow: WorkflowStart) -> None:
        from_thread.run(workflows.start, workflow)

    return start


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
StartWorkflow = Annotated[Callable[[WorkflowStart], None], Depends(start_workflow)]
