"""Tests for the ``CreativeStudio`` workflow: the studio's graph run on a real Temporal server.

The worker is built the way the process builds it, with the studio's real wiring, graph and
database, and fakes for the models, the image provider, the sandbox and object storage
(``studio_fakes.py``). It runs on the dev stack's Temporal server, on the test's own task
queue; skipped when it, or PostgreSQL, is not reachable. Timeouts are real, so the limits are
short.

The follow-up is a child the workflow starts and abandons on the orchestrator's queue. These
tests serve no worker there: they read the child Temporal holds under the follow-up's id, then
terminate it.
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
from studio_fakes import (
    GOAL,
    IMAGE_COST,
    STYLE_NOTES,
    ScriptedChat,
    a_brief,
    a_verdict,
    generating,
    one_round_that_passes,
    ready,
)
from temporalio.client import WorkflowFailureError, WorkflowHandle, WorkflowHistory
from temporalio.service import RPCError, RPCStatusCode

from openmarketer_core.agent_runtime.gate import PROPOSAL_DECIDED_SIGNAL
from openmarketer_core.agent_runtime.threads import thread_id
from openmarketer_core.budget.caps import SpendDecision
from openmarketer_core.credentials.catalog import OPENROUTER_API_KEY
from openmarketer_core.db.budget_ledger import month_balance, reservation_of_action
from openmarketer_core.db.media_assets import asset_versions, media_asset
from openmarketer_core.db.models import LedgerEntryKind, MediaAssetStatus, StudioRunStatus
from openmarketer_core.db.proposals import decide_proposal, list_proposals
from openmarketer_core.db.run_events import events_of_chat
from openmarketer_core.db.session import transaction
from openmarketer_core.db.studio_runs import studio_run
from openmarketer_core.llm import LLMError
from openmarketer_core.llm_config import ModelRouter
from openmarketer_core.studio.media_provider import MediaAccountLimited
from openmarketer_core.studio.openrouter_images import ACCOUNT_LIMITED
from openmarketer_core.studio.sandbox import SandboxUnavailable
from openmarketer_core.studio.workflow_contract import (
    STUDIO_RUN_KIND,
    STUDIO_WORKFLOW,
    studio_workflow_id,
)
from openmarketer_worker import main
from openmarketer_worker.credentials import WorkspaceCredentials
from openmarketer_worker.orchestrator.contract import (
    CHAT_FOLLOW_UP_TASK_QUEUE,
    CHAT_FOLLOW_UP_WORKFLOW,
    follow_up_workflow_id,
)

MODELS = str(Path(__file__).resolve().parents[4] / "config" / "models.yaml")
PERSON = uuid.uuid4()
RATE_LIMITED = LLMError("the model provider answered HTTP 429", status=429)
REFUSED = LLMError("the model provider refused the request (HTTP 400)", status=400)
# The start of every PNG, as base64: an image anywhere in the history would show it.
PNG_IN_BASE64 = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()[:10]


def readable(history: WorkflowHistory) -> str:
    """The history as the Temporal UI shows it: payloads are stored encoded, so decode them."""
    document = json.dumps([MessageToDict(event) for event in history.events])
    payloads = [
        base64.b64decode(data).decode(errors="replace")
        for data in re.findall(r'"data": "([A-Za-z0-9+/=]*)"', document)
    ]
    return "\n".join([document, *payloads])


@pytest.fixture
def serving(temporal, task_queue, studio_registration):
    """A worker built the way the process builds it, serving the studio on the test's queue."""

    @asynccontextmanager
    async def serve(**changes):
        registration = replace(studio_registration(**changes), task_queue=task_queue)
        async with main.build_worker(temporal, registration, max_concurrent_activities=4):
            yield

    return serve


@pytest.fixture
def start(temporal, task_queue):
    async def start(run) -> WorkflowHandle:
        return await temporal.start_workflow(
            STUDIO_WORKFLOW, run, id=studio_workflow_id(run.run_id), task_queue=task_queue
        )

    return start


async def ended(handle: WorkflowHandle) -> dict[str, Any]:
    # A workflow task that cannot be decoded is tried again for ever: a deadline makes a run
    # that hangs a failure of the test.
    return await asyncio.wait_for(handle.result(), timeout=90)


def the_run(sessions, run):
    with transaction(sessions) as session:
        return studio_run(
            session, workspace_id=run.workspace_id, project_id=run.project_id, run_id=run.run_id
        )


def versions_of(sessions, run, asset_id):
    with transaction(sessions) as session:
        return asset_versions(
            session,
            workspace_id=run.workspace_id,
            project_id=run.project_id,
            asset_id=asset_id,
            limit=20,
        ).items


# ------------------------------------------------------------------------- success
async def test_run_whose_image_passes_its_first_round_succeeds_with_a_draft_of_one_version(
    serving, start, studio_database, sessions
):
    run = studio_database.requested().run
    async with serving():
        output = await ended(await start(run))

    stored = the_run(sessions, run)
    assert stored.status is StudioRunStatus.SUCCEEDED
    assert output["asset_id"] == str(stored.final_asset_id)
    assert output["needs_review"] is False
    with transaction(sessions) as session:
        asset = media_asset(
            session,
            workspace_id=run.workspace_id,
            project_id=run.project_id,
            asset_id=uuid.UUID(output["asset_id"]),
        )
    [version] = versions_of(sessions, run, asset.id)
    assert asset.status is MediaAssetStatus.DRAFT
    assert asset.current_version_id == version.id == uuid.UUID(output["version_id"])
    assert version.passed is True and version.verifier_report is not None


async def test_paid_image_of_a_run_is_committed_in_the_ledger_at_what_it_cost(
    serving, start, studio_database, sessions
):
    run = studio_database.requested().run
    async with serving():
        output = await ended(await start(run))

    [version] = versions_of(sessions, run, uuid.UUID(output["asset_id"]))
    with transaction(sessions) as session:
        held = reservation_of_action(
            session,
            workspace_id=run.workspace_id,
            run_kind=STUDIO_RUN_KIND,
            action_id=version.action_id,
        )
    assert held is not None
    assert held.decision is SpendDecision.RUN_AUTOMATICALLY
    assert (held.settlement, held.settled_usd) == (LedgerEntryKind.COMMIT, IMAGE_COST)
    assert the_run(sessions, run).cost_usd >= IMAGE_COST


async def test_image_dearer_than_one_action_may_spend_alone_waits_for_a_persons_approval(
    serving, start, studio_database, sessions, media, temporal
):
    run = studio_database.requested().run
    caps = {"MEDIA_MONTHLY_CAP_USD": "10", "MEDIA_AUTO_APPROVE_USD": "0.01"}
    async with serving(environ=caps):
        handle = await start(run)
        proposal = await pending_proposal(sessions, run)
        assert media.calls == []
        assert proposal.estimate_usd == Decimal("0.08")
        with transaction(sessions) as session:
            decide_proposal(
                session,
                workspace_id=run.workspace_id,
                project_id=run.project_id,
                proposal_id=proposal.id,
                approved=True,
                decided_by=PERSON,
            )
        await handle.signal(PROPOSAL_DECIDED_SIGNAL, proposal.id)
        await ended(handle)

    assert len(media.calls) == 1
    assert the_run(sessions, run).status is StudioRunStatus.SUCCEEDED


async def pending_proposal(sessions, run, *, within: float = 30):
    async def stored():
        while True:
            with transaction(sessions) as session:
                found = list_proposals(
                    session, workspace_id=run.workspace_id, project_id=run.project_id, limit=5
                ).items
            if found:
                return found[0]
            await asyncio.sleep(0.1)

    return await asyncio.wait_for(stored(), timeout=within)


# ------------------------------------------------------------- a failure that passes
async def test_model_provider_that_limited_the_rate_is_tried_again_and_the_run_succeeds(
    serving, start, studio_database, sessions
):
    model = ScriptedChat(
        creative_director=[RATE_LIMITED, a_brief()],
        studio=[generating(), ready()],
        asset_verifier=[a_verdict()],
    )
    run = studio_database.requested().run
    async with serving(model=model):
        await ended(await start(run))

    assert the_run(sessions, run).status is StudioRunStatus.SUCCEEDED
    assert model.calls.count("creative_director") == 2


# ------------------------------------------------------------ failures that end it
async def test_request_the_model_provider_refuses_ends_the_run_failed_at_once(
    serving, start, studio_database, sessions, media
):
    model = ScriptedChat(creative_director=[REFUSED, a_brief()])
    run = studio_database.requested().run
    async with serving(model=model):
        with pytest.raises(WorkflowFailureError):
            await ended(await start(run))

    stored = the_run(sessions, run)
    assert (stored.status, stored.error) == (StudioRunStatus.FAILED, str(REFUSED))
    assert model.calls == ["creative_director"]
    assert media.calls == []


async def test_run_without_the_model_providers_key_ends_failed_with_what_to_set(
    serving, start, studio_database, sessions, cipher
):
    credentials = WorkspaceCredentials(sessions, {}, cipher)
    router = ModelRouter.from_file(MODELS, env={})

    def new_model(workspace_id: uuid.UUID):
        credentials.model_key(workspace_id, router)
        raise AssertionError("no key is set: the line above raises")

    run = studio_database.requested().run
    async with serving(new_model=new_model):
        with pytest.raises(WorkflowFailureError):
            await ended(await start(run))

    stored = the_run(sessions, run)
    assert stored.status is StudioRunStatus.FAILED
    assert stored.error is not None and "OPENROUTER_API_KEY" in stored.error


async def follow_up_of(temporal, run) -> dict[str, Any]:
    """The input of the follow-up the run started, which is then terminated."""
    handle = temporal.get_workflow_handle(follow_up_workflow_id(STUDIO_RUN_KIND, run.run_id))
    try:
        started = (await handle.fetch_history()).events[0]
    finally:
        try:
            await handle.terminate()
        except RPCError:
            pass  # it ended already: a worker of the dev stack may serve the queue
    (argument,) = started.workflow_execution_started_event_attributes.input.payloads
    return json.loads(argument.data)


async def test_key_that_cannot_pay_for_images_ends_the_run_failed_at_once_and_spends_nothing(
    serving, start, studio_database, sessions, media, temporal
):
    media.failures = [MediaAccountLimited(ACCOUNT_LIMITED)]
    model = one_round_that_passes()
    asked = studio_database.requested(from_a_chat=True)
    run = asked.run
    async with serving(model=model):
        handle = await start(run)
        with pytest.raises(WorkflowFailureError):
            await ended(handle)
    history = readable(await handle.fetch_history())

    stored = the_run(sessions, run)
    assert (stored.status, stored.error) == (StudioRunStatus.FAILED, ACCOUNT_LIMITED)
    assert len(media.calls) == 1
    assert model.calls == ["creative_director", "studio"]
    [action_id] = set(
        re.findall(re.escape(thread_id(STUDIO_RUN_KIND, run.run_id)) + r":[\w-]+", history)
    )
    with transaction(sessions) as session:
        held = reservation_of_action(
            session, workspace_id=run.workspace_id, run_kind=STUDIO_RUN_KIND, action_id=action_id
        )
        balance = month_balance(session, workspace_id=run.workspace_id)
    assert held is not None and held.settlement is LedgerEntryKind.RELEASE
    assert (balance.spent_usd, balance.reserved_usd) == (0, 0)
    assert (await follow_up_of(temporal, run))["chat_id"] == str(asked.chat_id)


async def test_sandbox_that_cannot_start_ends_the_run_failed_once_its_attempts_are_used_up(
    serving, start, studio_database, sessions, sandbox
):
    no_sandbox = "the sandbox could not be started: the docker command was not found"
    sandbox.failures = [SandboxUnavailable(no_sandbox) for _ in range(3)]
    model = one_round_that_passes()
    run = studio_database.requested().run
    async with serving(model=model):
        with pytest.raises(WorkflowFailureError):
            await ended(await start(run))

    stored = the_run(sessions, run)
    assert (stored.status, stored.error) == (StudioRunStatus.FAILED, no_sandbox)
    assert sandbox.failures == [] and sandbox.read == []
    assert model.left() == {"asset_verifier": 1}


# ------------------------------------------------------------------ telling the chat
async def test_run_a_chat_asked_for_starts_the_chats_follow_up_with_its_kind(
    serving, start, studio_database, sessions, temporal
):
    asked = studio_database.requested(from_a_chat=True)
    run = asked.run
    async with serving():
        output = await ended(await start(run))

    handle = temporal.get_workflow_handle(follow_up_workflow_id(STUDIO_RUN_KIND, run.run_id))
    try:
        described = await handle.describe()
        started = (await handle.fetch_history()).events[0]
    finally:
        try:
            await handle.terminate()
        except RPCError:
            pass  # it ended already: a worker of the dev stack may serve the queue
    (argument,) = started.workflow_execution_started_event_attributes.input.payloads
    assert (described.workflow_type, described.task_queue) == (
        CHAT_FOLLOW_UP_WORKFLOW,
        CHAT_FOLLOW_UP_TASK_QUEUE,
    )
    assert json.loads(argument.data) == {
        "workspace_id": str(run.workspace_id),
        "project_id": str(run.project_id),
        "chat_id": str(asked.chat_id),
        "run_kind": STUDIO_RUN_KIND,
        "run_id": str(run.run_id),
    }
    with transaction(sessions) as session:
        events = events_of_chat(
            session,
            workspace_id=run.workspace_id,
            project_id=run.project_id,
            chat_id=asked.chat_id,
            after=None,
            limit=50,
        )
    last = events[-1]
    assert (last.kind, dict(last.detail)) == (
        "finished",
        {"asset_id": output["asset_id"], "version_id": output["version_id"], "needs_review": False},
    )


async def test_run_no_chat_asked_for_starts_no_follow_up(serving, start, studio_database, temporal):
    run = studio_database.requested().run
    async with serving():
        await ended(await start(run))

    handle = temporal.get_workflow_handle(follow_up_workflow_id(STUDIO_RUN_KIND, run.run_id))
    with pytest.raises(RPCError) as missing:
        await handle.describe()
    assert missing.value.status is RPCStatusCode.NOT_FOUND


# --------------------------------------------------------------------- the history
async def test_history_holds_no_key_no_image_and_nothing_of_the_request(
    serving, start, studio_database, sessions, cipher, database, media, temporal
):
    run = studio_database.requested().run
    database.store_credential(run.workspace_id, OPENROUTER_API_KEY, "sk-stored-0123456789", cipher)
    credentials = WorkspaceCredentials(sessions, {}, cipher)
    router = ModelRouter.from_file(MODELS, env={})
    model = one_round_that_passes()
    used: list[str] = []

    def new_model(workspace_id: uuid.UUID):
        used.append(credentials.model_key(workspace_id, router))
        return model

    def new_media(workspace_id: uuid.UUID):
        used.append(credentials.model_key(workspace_id, router))
        return media

    async with serving(new_model=new_model, new_media=new_media):
        handle = await start(run)
        await ended(handle)
    history = readable(await handle.fetch_history())

    assert set(used) == {"sk-stored-0123456789"}
    assert "sk-stored-0123456789" not in history
    assert PNG_IN_BASE64 not in history
    assert GOAL not in history and STYLE_NOTES not in history


# ------------------------------------------------------- what the sandbox loads
def test_module_of_the_workflow_loads_no_database_graph_framework_image_library_or_activity():
    code = (
        "import sys, json; import openmarketer_worker.creative_studio.workflow; "
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
        "PIL",
        "boto3",
        "openmarketer_core.db",
        "openmarketer_core.graph_checkpoints",
        "openmarketer_core.studio.docker_sandbox",
        "openmarketer_core.studio.openrouter_images",
        "openmarketer_core.studio.asset_store",
        "temporalio.activity",
        "openmarketer_worker.creative_studio.wiring",
        "openmarketer_worker.creative_studio.studio",
        "openmarketer_worker.creative_studio.activities",
        "openmarketer_worker.creative_studio.settings",
        "openmarketer_worker.credentials",
        "openmarketer_worker.settings",
        "cryptography",
    )
    assert [name for name in json.loads(done.stdout) if name.startswith(forbidden)] == []
