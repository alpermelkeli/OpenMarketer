"""Tests for the settings an analysis runs with: its workspace's choices, read at each attempt.

The activities are the ones the worker registers, run as plain functions against PostgreSQL.
The choices are stored as the dashboard stores them and read for real (``WorkspaceSettings``),
and the chat model is the real client, on a transport that answers in place of the provider
and keeps what it was sent. The configuration is the repository's own ``config/models.yaml``.
"""

import json
import uuid
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

import httpx
import pytest
from temporalio.exceptions import ApplicationError

from openmarketer_core.db.session import transaction
from openmarketer_core.db.workspace_settings import store_setting
from openmarketer_core.llm import RouterChatModel
from openmarketer_core.llm_config import ModelRouter
from openmarketer_core.workspace_settings.analysis_limits import AnalysisLimitsChoice
from openmarketer_core.workspace_settings.analysis_settings import set_analysis_limits
from openmarketer_core.workspace_settings.model_choices import RoleChoice
from openmarketer_core.workspace_settings.model_settings import (
    set_rejected_parameters,
    set_role_choice,
    set_tier_model,
)
from openmarketer_worker.credentials import WorkspaceCredentials
from openmarketer_worker.repository_analyzer import steps
from openmarketer_worker.repository_analyzer.wiring import model_of_workspace
from openmarketer_worker.workspace_settings import WorkspaceSettings

MODELS = Path(__file__).resolve().parents[4] / "config" / "models.yaml"
KEY = {"OPENROUTER_API_KEY": "sk-environment-0123456789"}
OWNER = uuid.uuid4()


async def refused(work: Awaitable[object]) -> ApplicationError:
    with pytest.raises(ApplicationError) as failure:
        await work
    return failure.value


@pytest.fixture
def requests_sent() -> list[dict[str, Any]]:
    """The body of each request that reached the provider."""
    return []


@pytest.fixture
def worker_with(analyzer_with, sessions, requests_sent, turns):
    """The analyzer of a worker with that environment, wired as the worker wires it."""

    def worker(environ: dict[str, str], *, explores: bool = False, cost_of_a_call: float = 0):
        environ = {**KEY, **environ}
        router = ModelRouter.from_file(MODELS, env=environ)
        settings = WorkspaceSettings(sessions)
        credentials = WorkspaceCredentials(sessions, environ, None)

        def provider(request: httpx.Request) -> httpx.Response:
            requests_sent.append(json.loads(request.content))
            calls = turns.reads_the_readme if explores else turns.submits
            message = {"role": "assistant", "content": None, "tool_calls": calls}
            return httpx.Response(
                200,
                json={
                    "model": "fake/model",
                    "choices": [{"message": message}],
                    "usage": {"cost": cost_of_a_call},
                },
            )

        def new_model(workspace_id):
            model = model_of_workspace(
                settings.router_for(workspace_id, router), workspace_id, credentials
            )
            assert isinstance(model, RouterChatModel)
            model.transport = httpx.MockTransport(provider)
            return model

        return analyzer_with(new_model=new_model, limits_of=settings.analysis_limits)

    return worker


@pytest.fixture
def choose(sessions):
    """Store a choice for a workspace as the dashboard does, under the repository's models."""
    router = ModelRouter.from_file(MODELS, env={})

    def choose(store, **arguments):
        with transaction(sessions) as session:
            store(session, router, set_by=OWNER, **arguments)

    return choose


async def test_tier_chosen_in_the_dashboard_is_the_model_the_analysis_asks_for(
    worker_with, choose, started, requests_sent
):
    choose(set_tier_model, workspace_id=started.workspace_id, tier="fast", model="openai/chosen")
    await worker_with({}).to_the_end(started)
    assert [sent["model"] for sent in requests_sent] == ["openai/chosen"]


async def test_role_chosen_in_the_dashboard_sets_the_model_and_its_temperature(
    worker_with, choose, started, requests_sent
):
    choice = RoleChoice(model="openai/chosen", temperature=0.6)
    choose(set_role_choice, workspace_id=started.workspace_id, role="repo_analyzer", choice=choice)
    await worker_with({}).to_the_end(started)
    assert (requests_sent[0]["model"], requests_sent[0]["temperature"]) == ("openai/chosen", 0.6)


async def test_model_marked_as_rejecting_temperature_is_sent_none(
    worker_with, choose, sessions, started, requests_sent
):
    choose(set_tier_model, workspace_id=started.workspace_id, tier="fast", model="openai/chosen")
    with transaction(sessions) as session:
        set_rejected_parameters(
            session,
            workspace_id=started.workspace_id,
            model="openai/chosen",
            parameters=["temperature"],
            set_by=OWNER,
        )
    await worker_with({}).to_the_end(started)
    assert "temperature" not in requests_sent[0]


async def test_model_of_the_environment_wins_over_the_dashboards(
    worker_with, choose, started, requests_sent
):
    choose(set_tier_model, workspace_id=started.workspace_id, tier="fast", model="openai/chosen")
    await worker_with({"LLM_MODEL__REPO_ANALYZER": "openai/from-env"}).to_the_end(started)
    assert requests_sent[0]["model"] == "openai/from-env"


async def test_choice_of_another_workspace_is_not_used(
    worker_with, choose, database, started, requests_sent
):
    other = database.new_workspace()
    choose(set_tier_model, workspace_id=other, tier="fast", model="openai/chosen")
    await worker_with({}).to_the_end(started)
    assert requests_sent[0]["model"] != "openai/chosen"


async def test_model_chosen_while_the_run_waited_is_the_one_the_next_step_uses(
    worker_with, choose, started, requests_sent
):
    worker = worker_with({})
    cloned = await worker.cloned(started)
    choose(set_tier_model, workspace_id=started.workspace_id, tier="fast", model="openai/later")
    await worker.step(started, cloned)
    assert requests_sent[0]["model"] == "openai/later"


async def test_stored_choice_that_breaks_a_rule_ends_the_analysis_saying_what_to_reset(
    worker_with, sessions, started, requests_sent
):
    with transaction(sessions) as session:
        store_setting(
            session,
            workspace_id=started.workspace_id,
            name="model.role.repo_analyzer",
            value={"temperature": 9},
            set_by=OWNER,
        )
    failure = await refused(worker_with({}).step(started))
    assert (failure.type, failure.non_retryable) == (steps.ANALYSIS_FAILED, True)
    assert failure.message == (
        "the stored choice for the role repo_analyzer is not valid any more: reset it in the"
        " dashboard's Settings, Models"
    )
    assert requests_sent == []


async def test_cost_limit_chosen_in_the_dashboard_ends_the_analysis(
    worker_with, sessions, started, requests_sent
):
    with transaction(sessions) as session:
        set_analysis_limits(
            session,
            workspace_id=started.workspace_id,
            choice=AnalysisLimitsChoice(max_cost_usd=0.01),
            set_by=OWNER,
        )
    worker = worker_with({}, explores=True, cost_of_a_call=0.05)
    failure = await refused(worker.step(started, await worker.cloned(started)))
    assert failure.type == steps.ANALYSIS_FAILED
    assert failure.message == "cost limit reached ($0.050) before a profile was accepted"
    assert len(requests_sent) == 1
