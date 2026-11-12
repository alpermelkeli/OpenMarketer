"""Tests for the ``OrchestratorTask`` workflow: a task's graph and the runs it waits for, on a
real Temporal server.

The task's worker is built the way the process builds it, with its real wiring, graph, gate and
database and the orchestrator scripted; the runs it starts are served the same way by the
writer's and the studio's real wiring, with their models, image provider, sandbox and object
storage scripted or faked, each on a queue of the test's own (``children`` in ``conftest.py``).
Skipped when Temporal or PostgreSQL is not reachable. Timeouts are real, so the limits are
short. A person's decision is made as the API makes it: stored, then signalled to the run that
waits for it. The chat's follow-ups are children the task starts and abandons: these tests serve
them on a queue of the test's own with the real ``ChatFollowUp`` workflow and its activity
scripted (``ScriptedFollowUps``), which records what each was about, in the order they came.
"""

import asyncio
import base64
import json
import re
import subprocess
import sys
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest
import studio_fakes  # pyright: ignore[reportMissingImports]
import writer_fakes  # pyright: ignore[reportMissingImports]
from google.protobuf.json_format import MessageToDict
from sqlalchemy import text
from task_fakes import (
    BRAND_VOICE,
    COPY,
    FEATURE_DESCRIPTION,
    POST_REQUEST,
    POST_WITH_IMAGE,
    UNDERSTANDING,
    ScriptedOrchestrator,
    a_plan,
    a_post_plan,
    a_post_with_an_image_plan,
    a_review,
    writing_step,
)
from temporalio import activity
from temporalio.client import WorkflowFailureError, WorkflowHandle, WorkflowHistory
from temporalio.service import RPCError
from temporalio.worker import Worker

from openmarketer_core.agent_runtime.gate import PROPOSAL_DECIDED_SIGNAL, ProposalStatus
from openmarketer_core.credentials.catalog import OPENROUTER_API_KEY
from openmarketer_core.db.checkpoint_schema import CHECKPOINT_TABLES_OF_THREADS
from openmarketer_core.db.content_items import content_item
from openmarketer_core.db.models import ContentItemStatus, TaskStatus, TaskStepOutcome
from openmarketer_core.db.session import transaction
from openmarketer_core.db.studio_runs import studio_run
from openmarketer_core.db.writing_runs import writing_run
from openmarketer_core.llm import LLMError
from openmarketer_core.llm_config import ModelRouter
from openmarketer_core.orchestrator.task.plan import TaskOutcome
from openmarketer_core.orchestrator.task.task_agent.definitions import (
    CONFIRM_PLAN,
    RUN_STUDIO,
    RUN_WRITING,
)
from openmarketer_core.orchestrator.task.workflow_contract import (
    TASK_RUN_KIND,
    TASK_WORKFLOW,
    task_workflow_id,
)
from openmarketer_core.social_accounts.catalog import SocialPlatform
from openmarketer_core.social_accounts.publishing_costs import read_publishing_costs
from openmarketer_core.studio.studio_agent import rules as studio_rules
from openmarketer_core.writing.writer_agent.definitions import PUBLISH_POST
from openmarketer_worker import main
from openmarketer_worker.creative_studio import wiring as studio_wiring
from openmarketer_worker.credentials import WorkspaceCredentials
from openmarketer_worker.orchestrator import contract
from openmarketer_worker.orchestrator import steps as follow_up_steps
from openmarketer_worker.orchestrator.contract import (
    CHAT_FOLLOW_UP_WORKFLOW,
    ChatFollowUpInput,
    FollowUpNotTaken,
    follow_up_workflow_id,
)
from openmarketer_worker.orchestrator.workflow import ChatFollowUp
from openmarketer_worker.writer import wiring as writer_wiring
from openmarketer_worker.writer.publishing import PostPublishing

RATE_LIMITED = LLMError("the model provider answered HTTP 429", status=429)
REFUSED = LLMError("the model provider refused the request (HTTP 400)", status=400)
MODELS = str(Path(__file__).resolve().parents[4] / "config" / "models.yaml")
PUBLISHING = Path(__file__).resolve().parents[4] / "config" / "publishing.yaml"
STORED_KEY = "sk-stored-0123456789"


def readable(history: WorkflowHistory) -> str:
    """The history as the Temporal UI shows it: payloads are stored encoded, so decode them."""
    document = json.dumps([MessageToDict(event) for event in history.events])
    payloads = [
        base64.b64decode(data).decode(errors="replace")
        for data in re.findall(r'"data": "([A-Za-z0-9+/=]*)"', document)
    ]
    return "\n".join([document, *payloads])


@dataclass
class ScriptedFollowUps:
    """The follow-up activity of the chat, scripted: what each follow-up was about, in order.

    The follow-up of a step in ``held`` answers only once its event is set, and keeps its
    attempt alive meanwhile.
    """

    asked: list[ChatFollowUpInput] = field(default_factory=list)
    held: dict[str, asyncio.Event] = field(default_factory=dict)

    @activity.defn(name=follow_up_steps.FOLLOW_UP_ON_RUN)
    async def follow_up_on_run(self, follow_up: ChatFollowUpInput) -> None:
        self.asked.append(follow_up)
        release = self.held.get(follow_up.step or "")
        while release is not None and not release.is_set():
            activity.heartbeat()
            await asyncio.sleep(0.2)

    @activity.defn(name=follow_up_steps.RECORD_FOLLOW_UP_NOT_TAKEN)
    async def record_follow_up_not_taken(self, not_taken: FollowUpNotTaken) -> None:
        raise AssertionError(f"a follow-up was not taken: {not_taken.cause}")

    def steps(self) -> list[str | None]:
        return [follow_up.step for follow_up in self.asked]


@pytest.fixture
def follow_ups(monkeypatch) -> tuple[ScriptedFollowUps, str]:
    """The chat's follow-ups, scripted, and a queue of the test's own that the task starts them
    on: the real queue may be served by a worker in development. The workflow sandbox passes
    the contract through, so the task's workflow reads the queue the test set."""
    queue = f"test-follow-ups-{uuid.uuid4()}"
    monkeypatch.setattr(contract, "CHAT_FOLLOW_UP_TASK_QUEUE", queue)
    return ScriptedFollowUps(), queue


@pytest.fixture
def serving(temporal, task_queue, children, task_registration, sessions, checkpoints, follow_ups):
    """The task's worker on the test's queue, the writer's and the studio's on theirs, and the
    chat's follow-ups, scripted, on theirs; it gives the scripted follow-ups."""
    scripted, follow_up_queue = follow_ups

    @asynccontextmanager
    async def serve(*, writer_model: Any = None, **changes):
        def a_writer(workspace_id: uuid.UUID):
            if isinstance(writer_model, Exception):
                raise writer_model
            return writer_fakes.one_round_that_passes()

        writer = writer_wiring.registration(
            writer_wiring.WriterParts(
                sessions=sessions,
                checkpoints=checkpoints,
                new_model=a_writer,
                # The task's projects connect no account: every post is a dry run.
                publishing=PostPublishing(
                    sessions,
                    WorkspaceCredentials(sessions, writer_fakes.X_APP, None),
                    writer_fakes.FakeAssets(),
                    {SocialPlatform.X: writer_fakes.FakeX()},
                ),
                publishing_costs=read_publishing_costs(PUBLISHING),
                policy=writer_fakes.QUICK,
            )
        )
        studio_chat = studio_fakes.one_round_that_passes()
        studio = studio_wiring.registration(
            studio_wiring.StudioParts(
                sessions=sessions,
                checkpoints=checkpoints,
                new_model=lambda workspace_id: studio_chat,  # type: ignore[arg-type,return-value]
                new_media=lambda workspace_id, tier: studio_fakes.FakeMedia(),  # type: ignore[arg-type,return-value]
                image_estimate=lambda workspace_id, tier: studio_fakes.ESTIMATE,
                sandbox=studio_fakes.FakeSandbox(text=COPY),
                assets=studio_fakes.MemoryAssets(),
                policy=studio_fakes.QUICK,
                limits=studio_rules.Limits(),
                environ=studio_fakes.CAPS,
            )
        )
        served = [
            replace(task_registration(**changes), task_queue=task_queue),
            replace(writer, task_queue=children[RUN_WRITING]),
            replace(studio, task_queue=children[RUN_STUDIO]),
        ]
        async with AsyncExitStack() as stack:
            for registration in served:
                await stack.enter_async_context(
                    main.build_worker(temporal, registration, max_concurrent_activities=4)
                )
            await stack.enter_async_context(
                Worker(
                    temporal,
                    task_queue=follow_up_queue,
                    workflows=[ChatFollowUp],
                    activities=[scripted.follow_up_on_run, scripted.record_follow_up_not_taken],
                )
            )
            yield scripted

    return serve


@pytest.fixture
def start(temporal, task_queue):
    async def start(run) -> WorkflowHandle:
        return await temporal.start_workflow(
            TASK_WORKFLOW, run, id=task_workflow_id(run.run_id), task_queue=task_queue
        )

    return start


async def ended(handle: WorkflowHandle) -> dict[str, Any]:
    # A workflow task that cannot be decoded is tried again for ever: a deadline makes a task
    # that hangs a failure of the test.
    return await asyncio.wait_for(handle.result(), timeout=120)


async def pending(task_database, run, action_name: str, *, within: float = 60):
    """The proposal of ``action_name`` once the gate holds it, pending."""

    async def stored():
        while True:
            for proposal in task_database.proposals(run, action_name):
                if proposal.status is ProposalStatus.PENDING:
                    return proposal
            await asyncio.sleep(0.1)

    return await asyncio.wait_for(stored(), timeout=within)


async def decide(temporal, task_database, run, action_name: str, *, approved: bool):
    """Decide on the held action as a person does through the API, and wake its run."""
    proposal = await pending(task_database, run, action_name)
    task_database.decide(run, proposal, approved=approved)
    await temporal.get_workflow_handle(proposal.workflow_id).signal(
        PROPOSAL_DECIDED_SIGNAL, proposal.id
    )
    return proposal


async def follow_up_of(temporal, run) -> dict[str, Any]:
    """The input of the follow-up of its report the task started, which is then terminated."""
    handle = temporal.get_workflow_handle(follow_up_workflow_id(TASK_RUN_KIND, run.run_id))
    try:
        started = (await handle.fetch_history()).events[0]
    finally:
        try:
            await handle.terminate()
        except RPCError:
            pass  # it ended already
    (argument,) = started.workflow_execution_started_event_attributes.input.payloads
    return json.loads(argument.data)


def scope_of(run) -> dict[str, uuid.UUID]:
    return {"workspace_id": run.workspace_id, "project_id": run.project_id}


async def stored_succeeded(task_database, run, *, within: float = 90) -> None:
    """Once the task is stored succeeded: its graph has finished and its report is stored."""

    async def succeeded():
        while task_database.task(run).status is not TaskStatus.SUCCEEDED:
            await asyncio.sleep(0.1)

    await asyncio.wait_for(succeeded(), timeout=within)


def three_posts() -> ScriptedOrchestrator:
    """A plan of three posts one after the other, each reviewed as it ends."""
    plan = a_plan(writing_step("first"), writing_step("second"), writing_step("third"))
    reviews = (a_review("dispatch_next"), a_review("dispatch_next"), a_review())
    return ScriptedOrchestrator(plan, *reviews)


def two_posts() -> ScriptedOrchestrator:
    plan = a_plan(writing_step("first"), writing_step("second"))
    return ScriptedOrchestrator(plan, a_review("dispatch_next"), a_review())


async def approve_posts(temporal, task_database, run, count: int) -> None:
    for _ in range(count):
        await decide(temporal, task_database, run, PUBLISH_POST, approved=True)


# ------------------------------------------------------------------------- success
async def test_post_without_an_image_runs_the_writer_and_ends_with_its_approved_post(
    serving, start, task_database, sessions, temporal
):
    asked = task_database.requested(POST_REQUEST)
    model = ScriptedOrchestrator(a_post_plan(), a_review())
    async with serving(model=model):
        handle = await start(asked.run)
        await decide(temporal, task_database, asked.run, PUBLISH_POST, approved=True)
        output = await ended(handle)
    history = readable(await handle.fetch_history())

    stored = task_database.task(asked.run)
    assert output["outcome"] == "completed"
    assert (stored.status, stored.outcome) == (TaskStatus.SUCCEEDED, TaskOutcome.COMPLETED)
    [step] = stored.steps
    assert (step.step_key, step.outcome) == ("post", TaskStepOutcome.SUCCEEDED)
    with transaction(sessions) as session:
        run = writing_run(session, **scope_of(asked.run), run_id=step.child_run_id)
        assert run.content_item_id is not None
        item = content_item(session, **scope_of(asked.run), item_id=run.content_item_id)
    assert (run.requested_by, run.chat_thread_id) == ("run", None)
    assert item.status is ContentItemStatus.DRY_RUN
    assert stored.report is not None and stored.report.content_item_ids == [item.id]
    # Nobody confirmed anything: a post with no image cannot spend.
    assert task_database.proposals(asked.run, CONFIRM_PLAN) == []
    assert model.calls == ["orchestrator", "orchestrator"]
    assert (await follow_up_of(temporal, asked.run))["run_kind"] == TASK_RUN_KIND
    # The history holds identifiers and small values; nothing of the profile or the request.
    for unshown in (FEATURE_DESCRIPTION, BRAND_VOICE, UNDERSTANDING, POST_REQUEST):
        assert unshown not in history


async def test_post_with_an_image_is_confirmed_then_the_studios_asset_goes_with_the_post(
    serving, start, task_database, sessions, temporal
):
    asked = task_database.requested(POST_WITH_IMAGE)
    model = ScriptedOrchestrator(a_post_with_an_image_plan(), a_review("dispatch_next"), a_review())
    async with serving(model=model):
        handle = await start(asked.run)
        confirmation = await pending(task_database, asked.run, CONFIRM_PLAN)
        # The plan is stored with the task while it waits, as the person is shown it.
        assert [s.id for s in task_database.task(asked.run).plan.steps] == ["image", "post"]  # type: ignore[union-attr]
        assert confirmation.payload["plan"]["steps"][0]["request"]["exact_copy"] == [COPY]
        await decide(temporal, task_database, asked.run, CONFIRM_PLAN, approved=True)
        await decide(temporal, task_database, asked.run, PUBLISH_POST, approved=True)
        await ended(handle)

    stored = task_database.task(asked.run)
    image, post = stored.steps
    assert (image.step_key, post.step_key) == ("image", "post")
    assert image.outcome in (TaskStepOutcome.SUCCEEDED, TaskStepOutcome.NEEDS_REVIEW)
    assert post.outcome is TaskStepOutcome.SUCCEEDED
    with transaction(sessions) as session:
        made = studio_run(session, **scope_of(asked.run), run_id=image.child_run_id)
        run = writing_run(session, **scope_of(asked.run), run_id=post.child_run_id)
        assert run.content_item_id is not None
        item = content_item(session, **scope_of(asked.run), item_id=run.content_item_id)
    assert made.final_asset_id is not None and item.asset_id == made.final_asset_id
    assert item.status is ContentItemStatus.DRY_RUN
    assert stored.outcome is TaskOutcome.COMPLETED
    assert stored.report is not None and stored.report.asset_ids == [made.final_asset_id]
    await follow_up_of(temporal, asked.run)


async def test_rejected_plan_ends_the_task_with_its_report_and_starts_no_run(
    serving, start, task_database, temporal
):
    asked = task_database.requested(POST_WITH_IMAGE)
    async with serving(model=ScriptedOrchestrator(a_post_with_an_image_plan())):
        handle = await start(asked.run)
        await decide(temporal, task_database, asked.run, CONFIRM_PLAN, approved=False)
        output = await ended(handle)

    stored = task_database.task(asked.run)
    assert output["outcome"] == "plan_rejected"
    assert (stored.status, stored.outcome) == (TaskStatus.SUCCEEDED, TaskOutcome.PLAN_REJECTED)
    assert stored.steps == ()
    assert stored.report is not None and stored.report.rejected == ["A person rejected the plan."]
    await follow_up_of(temporal, asked.run)


# ------------------------------------------------------------- a failure that passes
async def test_model_provider_that_limited_the_rate_is_tried_again_and_the_task_plans(
    serving, start, task_database, temporal
):
    asked = task_database.requested(POST_WITH_IMAGE)
    model = ScriptedOrchestrator(RATE_LIMITED, a_post_with_an_image_plan())
    async with serving(model=model):
        handle = await start(asked.run)
        await decide(temporal, task_database, asked.run, CONFIRM_PLAN, approved=False)
        await ended(handle)

    assert task_database.task(asked.run).outcome is TaskOutcome.PLAN_REJECTED
    assert model.calls == ["orchestrator", "orchestrator"]
    await follow_up_of(temporal, asked.run)


# ------------------------------------------------------------ failures that end it
async def test_child_run_that_fails_is_a_failed_step_and_the_task_reports_it_incomplete(
    serving, start, task_database, temporal
):
    asked = task_database.requested(POST_REQUEST)
    async with serving(model=ScriptedOrchestrator(a_post_plan(), a_review()), writer_model=REFUSED):
        output = await ended(await start(asked.run))

    stored = task_database.task(asked.run)
    assert output["outcome"] == "incomplete"
    [step] = stored.steps
    assert (step.step_key, step.outcome) == ("post", TaskStepOutcome.FAILED)
    assert stored.report is not None and "failed" in stored.report.summary
    await follow_up_of(temporal, asked.run)


async def test_request_the_model_provider_refuses_ends_the_task_failed_and_tells_its_chat(
    serving, start, task_database, temporal
):
    asked = task_database.requested(POST_REQUEST)
    model = ScriptedOrchestrator(REFUSED)
    async with serving(model=model):
        with pytest.raises(WorkflowFailureError):
            await ended(await start(asked.run))

    stored = task_database.task(asked.run)
    assert (stored.status, stored.error) == (TaskStatus.FAILED, str(REFUSED))
    assert model.calls == ["orchestrator"]
    assert (await follow_up_of(temporal, asked.run))["chat_id"] == str(asked.chat_id)


# --------------------------------------------------------------------- the history
async def test_history_and_checkpoints_hold_no_key_of_the_tasks_workspace(
    serving, start, task_database, sessions, cipher, database, temporal
):
    asked = task_database.requested(POST_WITH_IMAGE)
    database.store_credential(asked.run.workspace_id, OPENROUTER_API_KEY, STORED_KEY, cipher)
    credentials = WorkspaceCredentials(sessions, {}, cipher)
    router = ModelRouter.from_file(MODELS, env={})
    model = ScriptedOrchestrator(a_post_with_an_image_plan())
    used: list[str] = []

    def new_model(workspace_id: uuid.UUID):
        used.append(credentials.model_key(workspace_id, router))
        return model

    async with serving(new_model=new_model):
        handle = await start(asked.run)
        await pending(task_database, asked.run, CONFIRM_PLAN)
        # While the task waits for a person, its checkpoints hold its state.
        kept = checkpoints_as_text(sessions)
        await decide(temporal, task_database, asked.run, CONFIRM_PLAN, approved=False)
        await ended(handle)
    history = readable(await handle.fetch_history())

    assert used == [STORED_KEY]
    assert str(asked.run.run_id) in kept
    for shown in (history, kept):
        assert STORED_KEY not in shown and STORED_KEY.encode().hex() not in shown
    await follow_up_of(temporal, asked.run)


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
        "import sys, json; import openmarketer_worker.orchestrator_task.workflow; "
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
        "openmarketer_core.orchestrator.task.task_agent.graph",
        "temporalio.activity",
        "openmarketer_worker.orchestrator_task.wiring",
        "openmarketer_worker.orchestrator_task.task",
        "openmarketer_worker.orchestrator_task.activities",
        "openmarketer_worker.orchestrator_task.settings",
        "openmarketer_worker.credentials",
        "openmarketer_worker.settings",
        "cryptography",
    )
    assert [name for name in json.loads(done.stdout) if name.startswith(forbidden)] == []


# ------------------------------------------------------------- the chat, step by step
async def test_task_of_a_chat_follows_up_on_each_step_it_goes_on_from_then_on_its_report(
    serving, start, task_database, temporal
):
    asked = task_database.requested(POST_REQUEST)
    async with serving(model=three_posts()) as follow_ups:
        follow_ups.held["first"] = asyncio.Event()
        handle = await start(asked.run)
        await approve_posts(temporal, task_database, asked.run, 3)
        await stored_succeeded(task_database, asked.run)
        # The task is over, and its chat still answers about the first step: nothing after it
        # was started, so the chat is told in order whichever follow-up would be taken first.
        assert follow_ups.steps() == ["first"]
        follow_ups.held["first"].set()
        output = await ended(handle)

    assert output["outcome"] == "completed"
    assert follow_ups.steps() == ["first", "second", None]
    assert {(f.run_kind, f.run_id, f.chat_id) for f in follow_ups.asked} == {
        (TASK_RUN_KIND, asked.run.run_id, asked.chat_id)
    }


async def test_task_no_chat_asked_for_starts_no_follow_up(serving, start, task_database, temporal):
    asked = task_database.requested(POST_REQUEST, from_a_chat=False)
    async with serving(model=two_posts()) as follow_ups:
        handle = await start(asked.run)
        await approve_posts(temporal, task_database, asked.run, 2)
        output = await ended(handle)

    assert output["outcome"] == "completed"
    assert follow_ups.asked == []


async def test_retried_workflow_does_not_follow_up_twice_on_a_step(
    serving, start, task_database, temporal, follow_ups
):
    asked = task_database.requested(POST_REQUEST)
    assert asked.chat_id is not None
    _, follow_up_queue = follow_ups
    # What an earlier execution of the task started for its first step, before it was retried.
    earlier = await temporal.start_workflow(
        CHAT_FOLLOW_UP_WORKFLOW,
        ChatFollowUpInput(
            asked.run.workspace_id,
            asked.run.project_id,
            asked.chat_id,
            TASK_RUN_KIND,
            asked.run.run_id,
            step="first",
        ),
        id=follow_up_workflow_id(TASK_RUN_KIND, asked.run.run_id, "first"),
        task_queue=f"{follow_up_queue}-unserved",
    )
    await earlier.terminate()
    async with serving(model=two_posts()) as scripted:
        handle = await start(asked.run)
        await approve_posts(temporal, task_database, asked.run, 2)
        await ended(handle)

    assert scripted.steps() == [None]
    found = temporal.get_workflow_handle(earlier.id)
    assert (await found.describe()).run_id == earlier.first_execution_run_id
