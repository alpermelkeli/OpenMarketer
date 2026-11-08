"""Fixtures of the task's tests: its fakes, its tasks, and its work wired as the worker wires it.

Handlers and activities are tested as plain functions against PostgreSQL; the workflow on the
dev stack's Temporal server, on the test's own task queue, with the runs it starts served by the
writer's and the studio's real wiring on queues of the test's own too (``children``). Both are
skipped when they are not reachable. The orchestrator, the writer and the studio's
models, image provider, sandbox and object storage are scripted or fakes: no test needs a model.
"""

import sys
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from temporalio.testing import ActivityEnvironment

from openmarketer_core.llm import ChatModel
from openmarketer_core.orchestrator.task.task_agent import rules
from openmarketer_core.orchestrator.task.task_agent.definitions import agent
from openmarketer_worker import AgentRegistration
from openmarketer_worker.orchestrator_task import wiring
from openmarketer_worker.orchestrator_task.policy import TaskPolicy
from openmarketer_worker.orchestrator_task.task import TaskWork

# The fakes are modules beside the tests, which are not packages: the task's own, and the
# writer's and the studio's for the runs it starts.
TESTS = Path(__file__).resolve().parent.parent
for folder in (TESTS / "orchestrator_task", TESTS / "writer", TESTS / "creative_studio"):
    sys.path.insert(0, str(folder))

from task_fakes import (  # noqa: E402
    QUICK,
    ScriptedOrchestrator,
    TaskDatabase,
    a_post_plan,
    a_review,
)


@pytest.fixture
def task_database(sessions) -> TaskDatabase:
    return TaskDatabase(sessions)


def a_post_without_an_image() -> ScriptedOrchestrator:
    return ScriptedOrchestrator(a_post_plan(), a_review())


@pytest.fixture
def task_parts(sessions, checkpoints):
    """The task's parts with a scripted orchestrator in place, and what a test changes of them."""

    def parts(
        *,
        model: ScriptedOrchestrator | None = None,
        new_model: Callable[[uuid.UUID], ChatModel] | None = None,
        policy: TaskPolicy = QUICK,
        limits: rules.TaskLimits | None = None,
    ) -> wiring.TaskParts:
        chat = model or a_post_without_an_image()
        return wiring.TaskParts(
            sessions=sessions,
            checkpoints=checkpoints,
            new_model=new_model or (lambda workspace_id: chat),
            policy=policy,
            limits=limits or rules.TaskLimits(),
        )

    return parts


@pytest.fixture
def work(task_parts) -> TaskWork:
    """The task's work as the wiring builds it, for its handlers as plain functions."""
    parts = task_parts()
    return TaskWork(
        parts.sessions, parts.checkpoints, new_model=parts.new_model, limits=parts.limits
    )


@pytest.fixture
def task_registration(task_parts) -> Callable[..., AgentRegistration]:
    def registration(**changes) -> AgentRegistration:
        return wiring.registration(task_parts(**changes))

    return registration


class Activities:
    """The activities a registration holds, found by name and run as plain functions."""

    def __init__(self, registration: AgentRegistration) -> None:
        self._registration = registration

    def named(self, name: str) -> Callable[..., Any]:
        (found,) = (a for a in self._registration.activities if a.__name__ == name)
        return found

    def run(self, name: str, argument: object) -> Any:
        return ActivityEnvironment().run(self.named(name), argument)

    async def run_async(self, name: str, argument: object) -> Any:
        return await ActivityEnvironment().run(self.named(name), argument)


@pytest.fixture
def activities_of() -> Callable[[AgentRegistration], Activities]:
    return Activities


@pytest.fixture
def children(monkeypatch) -> dict[str, str]:
    """A queue of the test's own for each kind of run a task starts, by action name.

    The targets of the task's run actions name the real queues of the writer's and the studio's
    workers, which a worker in development may be serving: a test's children go to its own.
    The workflow sandbox passes the agent's declarations through, so it sees the same targets.
    """
    queues: dict[str, str] = {}
    for name, target in agent.run_targets.items():
        queue = f"test-{name}-{uuid.uuid4()}"
        queues[name] = queue
        definition = agent._definitions[name]  # noqa: SLF001  (a test's own queue)
        monkeypatch.setitem(
            agent._definitions,  # noqa: SLF001
            name,
            replace(definition, run=replace(target, task_queue=queue)),
        )
    return queues
