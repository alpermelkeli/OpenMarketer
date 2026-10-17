"""Tests for the ``ChatFollowUp`` workflow with a scripted activity, and for what it loads.

The activity is scripted under the name the worker registers the real one with. They run
against the dev stack's Temporal server on a task queue of the test's own (see
``conftest.py`` of the worker's tests), and are skipped when it is not reachable. Timeouts
and the waits between attempts are real: the first wait is two seconds.
"""

import ast
import asyncio
import json
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from temporalio import activity
from temporalio.client import Client, WorkflowHandle
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from openmarketer_core.db.chat import TURN_ABANDONED_AFTER
from openmarketer_core.orchestrator.chat_agent.rules import MODEL_UNAVAILABLE_REASON
from openmarketer_worker import orchestrator
from openmarketer_worker.orchestrator import policy, steps
from openmarketer_worker.orchestrator.contract import (
    CHAT_FOLLOW_UP_WORKFLOW,
    ChatFollowUpInput,
    FollowUpNotTaken,
    follow_up_workflow_id,
)
from openmarketer_worker.orchestrator.workflow import ChatFollowUp


@dataclass
class ScriptedFollowUp:
    """The follow-up activity, raising each of ``outcomes`` in turn and then answering."""

    outcomes: list[Exception] = field(default_factory=list)
    asked: list[ChatFollowUpInput] = field(default_factory=list)
    told: list[FollowUpNotTaken] = field(default_factory=list)

    @activity.defn(name=steps.FOLLOW_UP_ON_RESEARCH)
    async def follow_up_on_research(self, follow_up: ChatFollowUpInput) -> None:
        self.asked.append(follow_up)
        if self.outcomes:
            raise self.outcomes.pop(0)

    @activity.defn(name=steps.RECORD_FOLLOW_UP_NOT_TAKEN)
    async def record_follow_up_not_taken(self, not_taken: FollowUpNotTaken) -> None:
        self.told.append(not_taken)


@asynccontextmanager
async def serving(client: Client, task_queue: str, follow_up: ScriptedFollowUp):
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[ChatFollowUp],
        activities=[follow_up.follow_up_on_research, follow_up.record_follow_up_not_taken],
    ):
        yield


async def follow_up_in(client: Client, task_queue: str, asked: ChatFollowUpInput) -> WorkflowHandle:
    handle = await client.start_workflow(
        CHAT_FOLLOW_UP_WORKFLOW,
        asked,
        id=follow_up_workflow_id(asked.run_id),
        task_queue=task_queue,
    )
    await asyncio.wait_for(handle.result(), timeout=60)
    return handle


def a_follow_up() -> ChatFollowUpInput:
    return ChatFollowUpInput(
        workspace_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        chat_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
    )


async def test_follow_up_runs_its_activity_once_and_completes(temporal, task_queue):
    scripted, asked = ScriptedFollowUp(), a_follow_up()
    async with serving(temporal, task_queue, scripted):
        await follow_up_in(temporal, task_queue, asked)
    assert scripted.asked == [asked]


async def test_history_holds_the_identifiers_of_the_follow_up_and_nothing_else(
    temporal, task_queue
):
    asked = a_follow_up()
    async with serving(temporal, task_queue, ScriptedFollowUp()):
        handle = await follow_up_in(temporal, task_queue, asked)
    started = (await handle.fetch_history()).events[0]
    arguments = started.workflow_execution_started_event_attributes.input.payloads
    assert [json.loads(argument.data) for argument in arguments] == [
        {
            "workspace_id": str(asked.workspace_id),
            "project_id": str(asked.project_id),
            "chat_id": str(asked.chat_id),
            "run_id": str(asked.run_id),
        }
    ]


async def test_busy_chat_is_followed_up_on_once_it_is_free(temporal, task_queue):
    busy = ApplicationError(steps.CHAT_IS_BUSY, type=steps.CHAT_BUSY)
    scripted = ScriptedFollowUp(outcomes=[busy])
    async with serving(temporal, task_queue, scripted):
        await follow_up_in(temporal, task_queue, a_follow_up())
    assert len(scripted.asked) == 2


async def test_follow_up_that_cannot_answer_is_not_tried_again_and_completes(temporal, task_queue):
    refused = ApplicationError(
        MODEL_UNAVAILABLE_REASON, type=steps.FOLLOW_UP_FAILED, non_retryable=True
    )
    scripted = ScriptedFollowUp(outcomes=[refused])
    async with serving(temporal, task_queue, scripted):
        handle = await follow_up_in(temporal, task_queue, a_follow_up())
    assert len(scripted.asked) == 1
    assert await handle.result() is None


async def test_error_nobody_worded_is_not_tried_again(temporal, task_queue):
    unexpected = ApplicationError("stopped", type="Unexpected")
    scripted = ScriptedFollowUp(outcomes=[unexpected])
    async with serving(temporal, task_queue, scripted):
        await follow_up_in(temporal, task_queue, a_follow_up())
    assert len(scripted.asked) == 1


async def test_follow_up_that_answered_tells_the_chat_nothing_more(temporal, task_queue):
    busy = ApplicationError(steps.CHAT_IS_BUSY, type=steps.CHAT_BUSY)
    scripted = ScriptedFollowUp(outcomes=[busy])
    async with serving(temporal, task_queue, scripted):
        await follow_up_in(temporal, task_queue, a_follow_up())
    assert scripted.told == []


async def test_follow_up_that_ended_before_a_turn_tells_the_chat_why(temporal, task_queue):
    refused = ApplicationError("set the key", type=steps.FOLLOW_UP_FAILED, non_retryable=True)
    scripted, asked = ScriptedFollowUp(outcomes=[refused]), a_follow_up()
    async with serving(temporal, task_queue, scripted):
        await follow_up_in(temporal, task_queue, asked)
    assert scripted.told == [
        FollowUpNotTaken(asked, steps.NOT_TAKEN_BECAUSE[steps.FOLLOW_UP_FAILED])
    ]


async def test_follow_up_with_nothing_to_follow_up_on_tells_nobody(temporal, task_queue):
    nothing = ApplicationError(
        steps.NOTHING_TO_FOLLOW_UP_ON, type=steps.NOTHING_TO_FOLLOW_UP, non_retryable=True
    )
    scripted = ScriptedFollowUp(outcomes=[nothing])
    async with serving(temporal, task_queue, scripted):
        await follow_up_in(temporal, task_queue, a_follow_up())
    assert scripted.told == []


def test_an_attempt_ends_before_its_turn_would_be_taken_for_abandoned():
    attempt = timedelta(seconds=policy.FOLLOW_UP.attempt_timeout_seconds)
    assert attempt < TURN_ABANDONED_AFTER


def test_modules_of_the_workflows_load_no_database_graph_framework_or_activity_code():
    modules = (
        "openmarketer_worker.orchestrator.workflow",
        "openmarketer_worker.researcher.workflow",
    )
    code = f"import sys, json; import {', '.join(modules)}; print(json.dumps(sorted(sys.modules)))"
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
        "temporalio.activity",
        "openmarketer_worker.orchestrator.activities",
        "openmarketer_worker.orchestrator.wiring",
        "openmarketer_worker.researcher.activities",
        "openmarketer_worker.researcher.research",
        "openmarketer_worker.researcher.wiring",
        "openmarketer_worker.credentials",
        "openmarketer_worker.settings",
        "cryptography",
    )
    assert [name for name in json.loads(done.stdout) if name.startswith(forbidden)] == []


def test_package_imports_nothing():
    source = Path(orchestrator.__file__ or "").read_text()
    imports = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import | ast.ImportFrom)
    ]
    assert imports == []
