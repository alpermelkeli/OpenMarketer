"""Fixtures of the writer's tests: its fakes, its runs, and its work wired as the worker wires it.

Handlers and activities are tested as plain functions against PostgreSQL; the workflow on the
dev stack's Temporal server, on the test's own task queue. Both are skipped when they are not
reachable. The writer is scripted (``writer_fakes.py``): no test needs a model.
"""

import sys
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from temporalio.testing import ActivityEnvironment

from openmarketer_core.agent_runtime.actions import ActionOutcome, ActionRequest
from openmarketer_core.agent_runtime.steps import GraphRunRef, StepOutcome
from openmarketer_core.credentials.cipher import CredentialCipher
from openmarketer_core.llm import ChatModel
from openmarketer_core.social_accounts.catalog import SocialPlatform
from openmarketer_core.social_accounts.publishing_costs import read_publishing_costs
from openmarketer_core.writing.workflow_contract import WRITING_RUN_KIND, WritingRunInput
from openmarketer_core.writing.writer_agent import rules
from openmarketer_worker import AgentRegistration
from openmarketer_worker.agent_runtime.vocabulary import RUN_ACTION, RUN_GRAPH_STEP, StepInput
from openmarketer_worker.credentials import WorkspaceCredentials
from openmarketer_worker.writer import steps, wiring
from openmarketer_worker.writer.policy import WriterPolicy
from openmarketer_worker.writer.publishing import PostPublishing
from openmarketer_worker.writer.writing import WritingWork

# The fakes are a module beside the tests, which are not a package.
sys.path.insert(0, str(Path(__file__).parent))

from writer_fakes import (  # noqa: E402
    QUICK,
    X_APP,
    FakeAssets,
    FakeX,
    ScriptedChat,
    WriterDatabase,
    one_round_that_passes,
)

PUBLISHING = Path(__file__).resolve().parents[4] / "config" / "publishing.yaml"


@pytest.fixture
def writer_database(sessions) -> WriterDatabase:
    return WriterDatabase(sessions)


@pytest.fixture
def x() -> FakeX:
    """The platform an approved post is published on, keeping the account's posts."""
    return FakeX()


@pytest.fixture
def assets() -> FakeAssets:
    return FakeAssets()


@pytest.fixture
def writer_parts(sessions, checkpoints, cipher: CredentialCipher, x: FakeX, assets: FakeAssets):
    """The writer's parts with a scripted model and a fake X in place, and what a test changes
    of them. The environment sets the installation's app on X, and the test's ``cipher`` opens
    the token of an account a test connects."""

    def parts(
        *,
        model: ScriptedChat | None = None,
        new_model: Callable[[uuid.UUID], ChatModel] | None = None,
        policy: WriterPolicy = QUICK,
        limits: rules.Limits | None = None,
        environ: Mapping[str, str] = X_APP,
    ) -> wiring.WriterParts:
        def scripted(workspace_id: uuid.UUID) -> ChatModel:
            # A model given is shared by every attempt; without one, each attempt that asks a
            # model gets a round that passes, so one registration serves several runs.
            return model or one_round_that_passes()

        return wiring.WriterParts(
            sessions=sessions,
            checkpoints=checkpoints,
            new_model=new_model or scripted,
            publishing=PostPublishing(
                sessions,
                WorkspaceCredentials(sessions, environ, cipher),
                assets,
                {SocialPlatform.X: x},
            ),
            publishing_costs=read_publishing_costs(PUBLISHING),
            policy=policy,
            limits=limits or rules.Limits(),
        )

    return parts


@pytest.fixture
def work(writer_parts) -> WritingWork:
    """The writer's work as the wiring builds it, for its handlers as plain functions."""
    parts = writer_parts()
    return WritingWork(
        parts.sessions,
        parts.checkpoints,
        new_model=parts.new_model,
        limits=parts.limits,
        publishing=parts.publishing,
        publishing_costs=parts.publishing_costs,
    )


@pytest.fixture
def writer_registration(writer_parts) -> Callable[..., AgentRegistration]:
    def registration(**changes) -> AgentRegistration:
        return wiring.registration(writer_parts(**changes))

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

    def start(self, run: WritingRunInput) -> WriterPolicy:
        return self.run(steps.START_RUN, run)

    async def step(self, run: WritingRunInput, resume: ActionOutcome | None = None) -> StepOutcome:
        graph_run = GraphRunRef(kind=WRITING_RUN_KIND, run_id=run.run_id)
        return await ActivityEnvironment().run(
            self.named(RUN_GRAPH_STEP), StepInput(run=graph_run, resume=resume)
        )

    async def act(self, request: ActionRequest) -> ActionOutcome:
        return await ActivityEnvironment().run(self.named(RUN_ACTION), request)


@pytest.fixture
def activities_of() -> Callable[[AgentRegistration], Activities]:
    return Activities
