"""Tests for the ``WritePost`` workflow: the writer's graph run on a real Temporal server.

The worker is built the way the process builds it, with the writer's real wiring, graph, gate
and database, and the writer scripted (``writer_fakes.py``). It runs on the dev
stack's Temporal server, on the test's own task queue; skipped when it, or PostgreSQL, is not
reachable. Timeouts are real, so the limits are short. A person's decision on the post is made
the way the API makes it: stored, then signalled to the run.
"""

import asyncio
import base64
import json
import re
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from google.protobuf.json_format import MessageToDict
from sqlalchemy import text
from temporalio.client import WorkflowFailureError, WorkflowHandle, WorkflowHistory
from temporalio.service import RPCError, RPCStatusCode
from writer_fakes import (
    ACCESS_TOKEN,
    BRAND_VOICE,
    FEATURE_DESCRIPTION,
    POST,
    SECRETS,
    ScriptedChat,
    a_draft,
    one_round_that_passes,
)

from openmarketer_core.agent_runtime.gate import PROPOSAL_DECIDED_SIGNAL, ProposalStatus
from openmarketer_core.credentials.catalog import OPENROUTER_API_KEY
from openmarketer_core.db.checkpoint_schema import CHECKPOINT_TABLES_OF_THREADS
from openmarketer_core.db.content_items import settle_content_item
from openmarketer_core.db.models import ContentItemStatus, WritingRunStatus
from openmarketer_core.db.run_events import latest_event_of_run
from openmarketer_core.db.session import transaction
from openmarketer_core.llm import LLMError
from openmarketer_core.llm_config import ModelRouter
from openmarketer_core.run_events import FAILED, FINISHED
from openmarketer_core.social_accounts.publishing import (
    PublishingFailure,
    PublishingRefused,
    PublishingUnavailable,
    RefusalReason,
)
from openmarketer_core.writing.workflow_contract import (
    WRITING_RUN_KIND,
    WRITING_WORKFLOW,
    writing_workflow_id,
)
from openmarketer_worker import main
from openmarketer_worker.credentials import WorkspaceCredentials
from openmarketer_worker.orchestrator.contract import (
    CHAT_FOLLOW_UP_TASK_QUEUE,
    CHAT_FOLLOW_UP_WORKFLOW,
    follow_up_workflow_id,
)

MODELS = str(Path(__file__).resolve().parents[4] / "config" / "models.yaml")
RATE_LIMITED = LLMError("the model provider answered HTTP 429", status=429)
REFUSED = LLMError("the model provider refused the request (HTTP 400)", status=400)
STORED_KEY = "sk-stored-0123456789"


def readable(history: WorkflowHistory) -> str:
    """The history as the Temporal UI shows it: payloads are stored encoded, so decode them."""
    document = json.dumps([MessageToDict(event) for event in history.events])
    payloads = [
        base64.b64decode(data).decode(errors="replace")
        for data in re.findall(r'"data": "([A-Za-z0-9+/=]*)"', document)
    ]
    return "\n".join([document, *payloads])


@pytest.fixture
def serving(temporal, task_queue, writer_registration):
    """A worker built the way the process builds it, serving the writer on the test's queue."""

    @asynccontextmanager
    async def serve(**changes):
        registration = replace(writer_registration(**changes), task_queue=task_queue)
        async with main.build_worker(temporal, registration, max_concurrent_activities=4):
            yield

    return serve


@pytest.fixture
def start(temporal, task_queue):
    async def start(run) -> WorkflowHandle:
        return await temporal.start_workflow(
            WRITING_WORKFLOW, run, id=writing_workflow_id(run.run_id), task_queue=task_queue
        )

    return start


async def ended(handle: WorkflowHandle) -> dict[str, Any]:
    # A workflow task that cannot be decoded is tried again for ever: a deadline makes a run
    # that hangs a failure of the test.
    return await asyncio.wait_for(handle.result(), timeout=90)


async def pending_proposal(writer_database, run, *, within: float = 30):
    async def stored():
        while True:
            found = writer_database.proposals(run)
            if found:
                return found[0]
            await asyncio.sleep(0.1)

    return await asyncio.wait_for(stored(), timeout=within)


async def decided(writer_database, handle, run, *, approved: bool) -> dict[str, Any]:
    """Decide on the run's post as a person does through the API; how the run ended."""
    proposal = await pending_proposal(writer_database, run)
    writer_database.decide(run, proposal, approved=approved)
    await handle.signal(PROPOSAL_DECIDED_SIGNAL, proposal.id)
    return await ended(handle)


# ------------------------------------------------------------------------- success
async def test_approved_post_is_recorded_as_a_dry_run_and_the_run_succeeds(
    serving, start, writer_database
):
    asked = writer_database.requested()
    async with serving():
        handle = await start(asked.run)
        proposal = await pending_proposal(writer_database, asked.run)
        assert writer_database.item(asked.run).status is ContentItemStatus.PROPOSED
        output = await decided(writer_database, handle, asked.run, approved=True)

    stored = writer_database.run(asked.run)
    item = writer_database.item(asked.run)
    assert stored.status is WritingRunStatus.SUCCEEDED
    assert (stored.profile_version_id, stored.content_item_id) == (asked.version_id, item.id)
    assert stored.cost_usd == item.cost_usd == Decimal("0.01")
    assert (item.status, item.text, item.proposal_id) == (
        ContentItemStatus.DRY_RUN,
        POST,
        proposal.id,
    )
    assert output["status"] == "dry_run" and output["content_item_id"] == str(item.id)
    assert proposal.payload["text"] == POST
    [settled] = writer_database.proposals(asked.run)
    assert settled.status is ProposalStatus.APPROVED


async def test_approved_post_with_an_image_and_research_is_recorded_with_both(
    serving, start, writer_database
):
    asked = writer_database.requested(with_image=True, with_research=True)
    assert asked.image is not None
    async with serving():
        handle = await start(asked.run)
        proposal = await pending_proposal(writer_database, asked.run)
        output = await decided(writer_database, handle, asked.run, approved=True)

    item = writer_database.item(asked.run)
    assert writer_database.run(asked.run).status is WritingRunStatus.SUCCEEDED
    assert output["status"] == "dry_run"
    assert (item.status, item.asset_id, item.asset_version_id, item.research_run_id) == (
        ContentItemStatus.DRY_RUN,
        asked.image.asset_id,
        asked.image.version_id,
        asked.research_run_id,
    )
    assert proposal.payload["asset_version_id"] == str(asked.image.version_id)


async def test_rejected_post_is_recorded_rejected_and_the_run_succeeds_without_sending_it(
    serving, start, writer_database
):
    asked = writer_database.requested()
    async with serving():
        handle = await start(asked.run)
        output = await decided(writer_database, handle, asked.run, approved=False)

    assert writer_database.run(asked.run).status is WritingRunStatus.SUCCEEDED
    assert writer_database.item(asked.run).status is ContentItemStatus.REJECTED
    assert output["status"] == "rejected"


# ------------------------------------------------------------- a failure that passes
async def test_model_provider_that_limited_the_rate_is_tried_again_and_the_run_proposes(
    serving, start, writer_database
):
    model = ScriptedChat(writer=[RATE_LIMITED, a_draft()])
    asked = writer_database.requested()
    async with serving(model=model):
        handle = await start(asked.run)
        await decided(writer_database, handle, asked.run, approved=True)

    assert writer_database.run(asked.run).status is WritingRunStatus.SUCCEEDED
    assert model.calls == ["writer", "writer"]


# ------------------------------------------------------------ failures that end it
async def test_request_the_model_provider_refuses_ends_the_run_failed_at_once(
    serving, start, writer_database
):
    model = ScriptedChat(writer=[REFUSED, a_draft()])
    asked = writer_database.requested()
    async with serving(model=model):
        with pytest.raises(WorkflowFailureError):
            await ended(await start(asked.run))

    stored = writer_database.run(asked.run)
    assert (stored.status, stored.error) == (WritingRunStatus.FAILED, str(REFUSED))
    assert (stored.content_item_id, stored.cost_usd) == (None, Decimal(0))
    assert model.calls == ["writer"]


async def test_project_without_an_approved_profile_ends_the_run_failed_saying_so(
    serving, start, writer_database
):
    asked = writer_database.requested(approved=False)
    async with serving():
        with pytest.raises(WorkflowFailureError):
            await ended(await start(asked.run))

    stored = writer_database.run(asked.run)
    assert stored.status is WritingRunStatus.FAILED
    assert stored.error == "the project has no approved product profile to write from"


async def test_approved_post_that_cannot_be_recorded_ends_the_run_failed_with_why(
    serving, start, writer_database, sessions
):
    asked = writer_database.requested()
    async with serving():
        handle = await start(asked.run)
        proposal = await pending_proposal(writer_database, asked.run)
        # The post was settled meanwhile: the handler cannot record it as sent.
        with transaction(sessions) as session:
            settle_content_item(
                session,
                workspace_id=asked.run.workspace_id,
                project_id=asked.run.project_id,
                item_id=uuid.UUID(proposal.payload["content_item_id"]),
                status=ContentItemStatus.EXPIRED,
            )
        writer_database.decide(asked.run, proposal, approved=True)
        await handle.signal(PROPOSAL_DECIDED_SIGNAL, proposal.id)
        with pytest.raises(WorkflowFailureError):
            await ended(handle)

    stored = writer_database.run(asked.run)
    assert stored.status is WritingRunStatus.FAILED
    assert stored.error is not None
    assert stored.error.startswith("the approved post could not be recorded: content item")
    assert stored.cost_usd == Decimal("0.01")


async def test_run_without_the_model_providers_key_ends_failed_with_what_to_set(
    serving, start, writer_database, sessions, cipher
):
    credentials = WorkspaceCredentials(sessions, {}, cipher)
    router = ModelRouter.from_file(MODELS, env={})

    def new_model(workspace_id: uuid.UUID):
        credentials.model_key(workspace_id, router)
        raise AssertionError("no key is set: the line above raises")

    asked = writer_database.requested()
    async with serving(new_model=new_model):
        with pytest.raises(WorkflowFailureError):
            await ended(await start(asked.run))

    stored = writer_database.run(asked.run)
    assert stored.status is WritingRunStatus.FAILED
    assert stored.error is not None and "OPENROUTER_API_KEY" in stored.error


# ------------------------------------------------- published from the connected account
async def test_approved_post_is_published_from_the_account_and_the_run_succeeds(
    serving, start, writer_database, cipher, x
):
    asked = writer_database.requested()
    writer_database.connect(asked.run, cipher)
    async with serving():
        handle = await start(asked.run)
        proposal = await pending_proposal(writer_database, asked.run)
        output = await decided(writer_database, handle, asked.run, approved=True)

    item = writer_database.item(asked.run)
    ((_, post),) = x.posts
    assert writer_database.run(asked.run).status is WritingRunStatus.SUCCEEDED
    assert output["status"] == "published"
    assert (item.status, item.external_id, item.proposal_id) == (
        ContentItemStatus.PUBLISHED,
        post.external_id,
        proposal.id,
    )
    assert proposal.payload["username"] == "acme"
    assert proposal.payload["cost_estimate_usd"] == "0.015"


async def test_platform_that_limited_the_rate_is_waited_for_and_the_post_published_once(
    serving, start, writer_database, cipher, x
):
    asked = writer_database.requested()
    writer_database.connect(asked.run, cipher)
    x.sends = [PublishingUnavailable("X limits the rate", retry_after_seconds=1)]
    async with serving():
        handle = await start(asked.run)
        output = await decided(writer_database, handle, asked.run, approved=True)

    assert output["status"] == "published"
    assert writer_database.item(asked.run).status is ContentItemStatus.PUBLISHED
    assert len(x.posts) == 1 and len(x.publish_calls) == 2 and len(x.lookup_calls) == 1


async def test_post_the_platform_refused_ends_the_run_failed_and_the_post_failed_with_why(
    serving, start, writer_database, cipher, x
):
    asked = writer_database.requested()
    writer_database.connect(asked.run, cipher)
    x.sends = [PublishingRefused("X", RefusalReason.TOO_LONG)]
    async with serving():
        handle = await start(asked.run)
        with pytest.raises(WorkflowFailureError):
            await decided(writer_database, handle, asked.run, approved=True)

    stored = writer_database.run(asked.run)
    item = writer_database.item(asked.run)
    assert stored.status is WritingRunStatus.FAILED
    assert stored.error == (
        "the approved post could not be recorded: X refused the post: the platform says the "
        "text is too long"
    )
    assert (item.status, item.publishing_failure) == (
        ContentItemStatus.FAILED,
        PublishingFailure.TOO_LONG,
    )
    assert len(x.publish_calls) == 1  # a refusal is not tried again


async def test_history_and_checkpoints_hold_no_app_key_or_token_of_a_published_post(
    serving, start, writer_database, sessions, cipher, x
):
    asked = writer_database.requested()
    writer_database.connect(asked.run, cipher)
    async with serving():
        handle = await start(asked.run)
        await pending_proposal(writer_database, asked.run)
        kept = checkpoints_as_text(sessions)
        await decided(writer_database, handle, asked.run, approved=True)
    history = readable(await handle.fetch_history())

    assert x.publish_calls[0].tokens.access_token == ACCESS_TOKEN
    for shown in (history, kept):
        for secret in SECRETS:
            assert secret not in shown and secret.encode().hex() not in shown


# --------------------------------------------------------------------- the history
async def test_history_and_checkpoints_hold_no_key_and_nothing_of_the_profile(
    serving, start, writer_database, sessions, cipher, database
):
    asked = writer_database.requested()
    database.store_credential(asked.run.workspace_id, OPENROUTER_API_KEY, STORED_KEY, cipher)
    credentials = WorkspaceCredentials(sessions, {}, cipher)
    router = ModelRouter.from_file(MODELS, env={})
    model = one_round_that_passes()
    used: list[str] = []

    def new_model(workspace_id: uuid.UUID):
        used.append(credentials.model_key(workspace_id, router))
        return model

    async with serving(new_model=new_model):
        handle = await start(asked.run)
        await pending_proposal(writer_database, asked.run)
        # While the run waits for a person, its checkpoints hold its state.
        kept = checkpoints_as_text(sessions)
        await decided(writer_database, handle, asked.run, approved=True)
    history = readable(await handle.fetch_history())

    assert used == [STORED_KEY]
    assert str(asked.run.run_id) in kept
    for shown in (history, kept):
        assert STORED_KEY not in shown and STORED_KEY.encode().hex() not in shown
    assert FEATURE_DESCRIPTION not in history and BRAND_VOICE not in history


# ------------------------------------------------------------------ telling the chat
async def follow_up_of(temporal, run) -> tuple[Any, dict[str, Any]]:
    """The follow-up the run started, described, and its input; it is then terminated."""
    handle = temporal.get_workflow_handle(follow_up_workflow_id(WRITING_RUN_KIND, run.run_id))
    try:
        described = await handle.describe()
        started = (await handle.fetch_history()).events[0]
    finally:
        try:
            await handle.terminate()
        except RPCError:
            pass  # it ended already: a worker of the dev stack may serve the queue
    (argument,) = started.workflow_execution_started_event_attributes.input.payloads
    return described, json.loads(argument.data)


def last_event(writer_database, run, kind: str):
    with transaction(writer_database.sessions) as session:
        return latest_event_of_run(
            session,
            workspace_id=run.workspace_id,
            project_id=run.project_id,
            run_kind=WRITING_RUN_KIND,
            run_id=run.run_id,
            kind=kind,
        )


async def test_run_a_chat_asked_for_starts_the_chats_follow_up_once_its_post_is_settled(
    serving, start, writer_database, temporal
):
    asked = writer_database.requested(from_a_chat=True)
    async with serving():
        handle = await start(asked.run)
        await decided(writer_database, handle, asked.run, approved=False)

    described, given = await follow_up_of(temporal, asked.run)
    assert (described.workflow_type, described.task_queue) == (
        CHAT_FOLLOW_UP_WORKFLOW,
        CHAT_FOLLOW_UP_TASK_QUEUE,
    )
    assert given == {
        "workspace_id": str(asked.run.workspace_id),
        "project_id": str(asked.run.project_id),
        "chat_id": str(asked.chat_id),
        "run_kind": WRITING_RUN_KIND,
        "run_id": str(asked.run.run_id),
        "step": None,
    }
    finished = last_event(writer_database, asked.run, FINISHED)
    assert finished is not None
    assert dict(finished.detail) == {
        "content_item_id": str(writer_database.item(asked.run).id),
        "post_status": "rejected",
    }


async def test_failed_run_a_chat_asked_for_tells_the_chat_why(
    serving, start, writer_database, temporal
):
    asked = writer_database.requested(approved=False, from_a_chat=True)
    async with serving():
        with pytest.raises(WorkflowFailureError):
            await ended(await start(asked.run))

    _, given = await follow_up_of(temporal, asked.run)
    assert given["chat_id"] == str(asked.chat_id)
    failed = last_event(writer_database, asked.run, FAILED)
    assert failed is not None
    assert dict(failed.detail) == {"reason": writer_database.run(asked.run).error}


async def test_run_no_chat_asked_for_starts_no_follow_up(serving, start, writer_database, temporal):
    asked = writer_database.requested()
    async with serving():
        handle = await start(asked.run)
        await decided(writer_database, handle, asked.run, approved=False)

    handle = temporal.get_workflow_handle(follow_up_workflow_id(WRITING_RUN_KIND, asked.run.run_id))
    with pytest.raises(RPCError) as missing:
        await handle.describe()
    assert missing.value.status is RPCStatusCode.NOT_FOUND


def checkpoints_as_text(sessions) -> str:
    """Every row of the checkpoint tables, as the database prints it."""
    with transaction(sessions) as session:
        return "\n".join(
            row
            for table in CHECKPOINT_TABLES_OF_THREADS
            for row in session.execute(text(f"SELECT t::text FROM {table} t")).scalars()
        )


# ------------------------------------------------------- what the sandbox loads
def test_module_of_the_workflow_loads_no_database_graph_framework_or_activity():
    code = (
        "import sys, json; import openmarketer_worker.writer.workflow; "
        "print(json.dumps(sorted(sys.modules)))"
    )
    # ``-I``: nothing of this process's environment or working folder reaches the import.
    done = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )
    forbidden = (
        "langgraph",
        "langchain",
        "sqlalchemy",
        "psycopg",
        "openmarketer_core.db",
        "openmarketer_core.graph_checkpoints",
        "openmarketer_core.writing.writer_agent.graph",
        "temporalio.activity",
        "openmarketer_worker.writer.wiring",
        "openmarketer_worker.writer.writing",
        "openmarketer_worker.writer.activities",
        "openmarketer_worker.writer.settings",
        "openmarketer_worker.credentials",
        "openmarketer_worker.settings",
        "cryptography",
    )
    assert [name for name in json.loads(done.stdout) if name.startswith(forbidden)] == []
