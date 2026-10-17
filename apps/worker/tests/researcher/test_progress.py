"""Tests for what a research run shows the chat that asked for it: its progress and its end.

They run the researcher's activities as the worker registers them, as plain functions in
Temporal's activity environment, against PostgreSQL (see the root ``conftest.py``); they are
skipped when it is not reachable. The model is scripted and nothing reaches the network: the
search, the fetcher and the page store are never asked in the steps these tests take.
"""

import json
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from temporalio.testing import ActivityEnvironment

from openmarketer_core.agent_runtime.steps import GraphRunRef, StepOutcome
from openmarketer_core.db.run_events import StoredRunEvent, events_of_chat
from openmarketer_core.db.session import DatabaseError, transaction
from openmarketer_core.llm import ChatReply
from openmarketer_core.research.research_agent.definitions import SEARCH_WEB
from openmarketer_core.research.research_agent.rules import SUBMIT_QUERIES
from openmarketer_core.research.workflow_contract import RESEARCH_RUN_KIND, ResearchRunInput
from openmarketer_core.run_events import FAILED, FINISHED, PLANNED
from openmarketer_worker import AgentRegistration
from openmarketer_worker.agent_runtime.vocabulary import RUN_GRAPH_STEP, StepInput
from openmarketer_worker.researcher import research, steps, wiring
from openmarketer_worker.researcher.policy import ResearchPolicy


class PlanningModel:
    """A researcher model that plans one query, and records the roles it was asked for."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def chat(self, role, messages, *, tools=None, tool_choice=None) -> ChatReply:
        self.asked.append(role)
        arguments = {"queries": ["acme competitors"], "categories": ["general"]}
        call = {
            "id": "call-1",
            "type": "function",
            "function": {"name": SUBMIT_QUERIES, "arguments": json.dumps(arguments)},
        }
        message = {"role": "assistant", "content": None, "tool_calls": [call]}
        return ChatReply(message=message, model="fake/model", cost_usd=0.01)


class NotAsked:
    """Stands in for a service the steps under test never reach."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"{name} was not expected to be called")


@pytest.fixture
def researcher(sessions, checkpoints) -> AgentRegistration:
    model = PlanningModel()
    return wiring.registration(
        wiring.ResearcherParts(
            sessions=sessions,
            checkpoints=checkpoints,
            new_model=lambda workspace_id: model,
            new_decisions=lambda workspace_id: NotAsked(),  # pyright: ignore[reportArgumentType]
            search=NotAsked(),  # pyright: ignore[reportArgumentType]
            fetcher=NotAsked(),  # pyright: ignore[reportArgumentType]
            pages=NotAsked(),  # pyright: ignore[reportArgumentType]
            policy=ResearchPolicy(),
            retention=timedelta(days=1),
        )
    )


def activity_of(registration: AgentRegistration, name: str) -> Callable[..., Any]:
    (found,) = (a for a in registration.activities if a.__name__ == name)
    return found


async def first_step(registration: AgentRegistration, run: ResearchRunInput) -> StepOutcome:
    step = StepInput(run=GraphRunRef(kind=RESEARCH_RUN_KIND, run_id=run.run_id), resume=None)
    return await ActivityEnvironment().run(activity_of(registration, RUN_GRAPH_STEP), step)


def record_end(registration: AgentRegistration, end: steps.RunEnd) -> steps.RunEndRecorded:
    return ActivityEnvironment().run(activity_of(registration, steps.RECORD_END), end)


def events_of(sessions, run: ResearchRunInput, chat_id: uuid.UUID | None) -> list[StoredRunEvent]:
    assert chat_id is not None
    with transaction(sessions) as session:
        return events_of_chat(
            session,
            workspace_id=run.workspace_id,
            project_id=run.project_id,
            chat_id=chat_id,
            after=None,
            limit=50,
        )


# ------------------------------------------------------------------- progress
async def test_node_that_ran_shows_its_progress_in_the_chat_that_asked(
    researcher, database, sessions
):
    asked = database.requested_research()

    outcome = await first_step(researcher, asked.run)

    assert outcome.action is not None and outcome.action.name == SEARCH_WEB
    [planned] = events_of(sessions, asked.run, asked.chat_id)
    assert (planned.run_kind, planned.run_id, planned.kind) == (
        RESEARCH_RUN_KIND,
        asked.run.run_id,
        PLANNED,
    )
    assert planned.detail["queries"] == ["acme competitors"]


async def test_progress_that_cannot_be_stored_does_not_fail_the_step(
    researcher, database, sessions, monkeypatch
):
    def database_down(*args, **kwargs):
        raise DatabaseError("the database could not be reached")

    monkeypatch.setattr(research, "append_events", database_down)
    asked = database.requested_research()

    outcome = await first_step(researcher, asked.run)

    assert outcome.action is not None and outcome.action.name == SEARCH_WEB
    assert events_of(sessions, asked.run, asked.chat_id) == []


# ------------------------------------------------------------------------ end
def test_run_that_finished_shows_how_many_findings_it_has(researcher, database, sessions):
    asked = database.requested_research()

    told = record_end(researcher, steps.RunEnd(run=asked.run, findings=3))

    assert told == steps.RunEndRecorded(chat_id=asked.chat_id)
    [finished] = events_of(sessions, asked.run, asked.chat_id)
    assert (finished.kind, dict(finished.detail)) == (FINISHED, {"findings": 3})


def test_run_that_failed_shows_why(researcher, database, sessions):
    asked = database.requested_research()

    record_end(researcher, steps.RunEnd(run=asked.run, reason="the research did not finish"))

    [failed] = events_of(sessions, asked.run, asked.chat_id)
    assert (failed.kind, dict(failed.detail)) == (FAILED, {"reason": "the research did not finish"})


def test_run_no_chat_asked_for_names_no_chat_to_follow_up_in(researcher, database):
    asked = database.requested_research(from_a_chat=False)

    told = record_end(researcher, steps.RunEnd(run=asked.run, findings=0))

    assert told == steps.RunEndRecorded(chat_id=None)


def test_run_of_another_workspace_records_nothing_and_names_no_chat(researcher, database, sessions):
    asked = database.requested_research()
    elsewhere = ResearchRunInput(
        workspace_id=database.new_workspace(),
        project_id=asked.run.project_id,
        run_id=asked.run.run_id,
    )

    told = record_end(researcher, steps.RunEnd(run=elsewhere, findings=3))

    assert told == steps.RunEndRecorded(chat_id=None)
    assert events_of(sessions, asked.run, asked.chat_id) == []
