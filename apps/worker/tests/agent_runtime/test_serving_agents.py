"""One worker process serving two agents, the way ``main.py`` builds and serves them.

The greeter and the announcer register the runtime's activities under the same names. Each is
served by a ``Worker`` of its own, on a queue of its own, and both run their workflows in the
same process at the same time. Two agents on one queue are refused before anything serves.
They run on the dev stack's Temporal server and PostgreSQL, and are skipped without them.
"""

import asyncio
import uuid
from dataclasses import replace

import pytest
from announcer.rig import Desk
from announcer.wiring import Board
from announcer.wiring import wire as wire_the_announcer
from dsl.definitions import KIND as GREETER_KIND
from dsl.wiring import GreeterWorld
from dsl.wiring import wire as wire_the_greeter

from openmarketer_core.agent_runtime.gate import RunScope
from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_core.db.models import Project
from openmarketer_core.db.session import transaction
from openmarketer_worker import main


@pytest.fixture
def scope(database, sessions) -> RunScope:
    """A project for the announcer's proposals."""
    workspace_id = database.new_workspace()
    with transaction(sessions) as session:
        project = Project(
            workspace_id=workspace_id, name="Example", source_repo_url="https://example.com/r.git"
        )
        session.add(project)
        session.flush()
        return RunScope(workspace_id=workspace_id, project_id=project.id)


@pytest.fixture
def registrations(task_queue, checkpoints, sessions):
    """The greeter and the announcer, each on a queue of its own for this test."""
    greeter = replace(wire_the_greeter(GreeterWorld(), checkpoints), task_queue=f"{task_queue}-g")
    announcer = replace(
        wire_the_announcer(Board(), checkpoints, sessions), task_queue=f"{task_queue}-a"
    )
    return greeter, announcer


async def test_two_agents_run_their_workflows_in_one_process(
    temporal, registrations, sessions, scope
):
    greeter, announcer = registrations
    workers = main.build_workers(temporal, [greeter, announcer], max_concurrent_activities=4)
    stop = asyncio.Event()
    serving = asyncio.create_task(main.serve_until(stop, workers, sessions, []))
    try:
        greeting = await temporal.start_workflow(
            "Greeter",
            GraphRunRef(kind=GREETER_KIND, run_id=uuid.uuid4()),
            id=f"greeter-{uuid.uuid4()}",
            task_queue=greeter.task_queue,
        )
        desk = Desk(temporal, announcer.task_queue, sessions, scope)
        _, announcing = await desk.start()
        await desk.decide(announcing, await desk.pending_proposal(), approved=True)

        assert await asyncio.wait_for(greeting.result(), timeout=30) == {"summary": "hello, Ada!"}
        assert await asyncio.wait_for(announcing.result(), timeout=30) == {
            "outcome": "published as post-1"
        }
    finally:
        stop.set()
        await serving


def test_two_agents_on_one_queue_are_refused_before_anything_serves(temporal, registrations):
    greeter, announcer = registrations
    on_one_queue = replace(announcer, task_queue=greeter.task_queue)

    with pytest.raises(main.StartupError, match="share the task queue"):
        main.build_workers(temporal, [greeter, on_one_queue], max_concurrent_activities=4)
