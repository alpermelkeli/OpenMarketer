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

from openmarketer_api.event_stream import OpenStreams, StreamTiming
from openmarketer_api.workflows import Workflows
from openmarketer_core.credentials.cipher import CredentialCipher
from openmarketer_core.db.session import transaction
from openmarketer_core.llm_catalog import ModelCatalog
from openmarketer_core.llm_config import ModelRouter
from openmarketer_core.research.page_store import PageStore
from openmarketer_core.studio.asset_store import AssetStore
from openmarketer_core.tracing import NOT_TRACING, Tracing
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
    # The model configuration with the environment's overrides; it holds the environment too.
    model_router: ModelRouter = field(repr=False)
    model_catalog: ModelCatalog = field(repr=False)
    # The object storage the pages of deleted research runs are removed from.
    page_store: PageStore
    # The object storage the studio's images are read from and removed from.
    asset_store: AssetStore
    # The process's tracing: off unless the environment gave Langfuse's keys (``main.py``).
    tracing: Tracing = NOT_TRACING


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


def tell_run(
    workflows: Annotated[Workflows, Depends(workflows)],
) -> Callable[[str, uuid.UUID], None]:
    """Telling a run of a decision, as a function a route's worker thread can call."""

    def tell(workflow_id: str, proposal_id: uuid.UUID) -> None:
        from_thread.run(workflows.tell_decided, workflow_id, proposal_id)

    return tell


def environment(services: Annotated[Services, Depends(_services)]) -> Mapping[str, str]:
    return services.environment


def credential_cipher(
    services: Annotated[Services, Depends(_services)],
) -> CredentialCipher | None:
    return services.credential_cipher


def model_router(services: Annotated[Services, Depends(_services)]) -> ModelRouter:
    return services.model_router


def model_catalog(services: Annotated[Services, Depends(_services)]) -> ModelCatalog:
    return services.model_catalog


def page_store(services: Annotated[Services, Depends(_services)]) -> PageStore:
    return services.page_store


def asset_store(services: Annotated[Services, Depends(_services)]) -> AssetStore:
    return services.asset_store


def process_tracing(services: Annotated[Services, Depends(_services)]) -> Tracing:
    return services.tracing


def event_stream_timing() -> StreamTiming:
    """How a stream of events polls, keeps alive and ends; a test makes it quick."""
    return StreamTiming()


# One count for the whole process: every stream of every request takes its place here.
_OPEN_STREAMS = OpenStreams()


def open_event_streams() -> OpenStreams:
    """The streams this process has open; a test gives its own, with a small limit."""
    return _OPEN_STREAMS


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
Models = Annotated[ModelRouter, Depends(model_router)]
Catalog = Annotated[ModelCatalog, Depends(model_catalog)]
Pages = Annotated[PageStore, Depends(page_store)]
Assets = Annotated[AssetStore, Depends(asset_store)]
ProcessTracing = Annotated[Tracing, Depends(process_tracing)]
StartWorkflow = Annotated[Callable[[WorkflowStart], None], Depends(start_workflow)]
EventStreamTiming = Annotated[StreamTiming, Depends(event_stream_timing)]
EventStreamsOpen = Annotated[OpenStreams, Depends(open_event_streams)]
TellRun = Annotated[Callable[[str, uuid.UUID], None], Depends(tell_run)]
