"""Tests for the follow-up activity: the turn it takes, with which model, and how it fails.

The activity is the one the worker registers, run as a plain function in Temporal's activity
environment against PostgreSQL (see the root ``conftest.py``); skipped when it is not
reachable. The model is scripted: nothing reaches the network.
"""

import dataclasses
import json
import uuid

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from openmarketer_core.credentials.resolution import ModelKeyNotSet
from openmarketer_core.db.chat import chat_history
from openmarketer_core.db.chat_turns import MAX_FOLLOW_UP_TURNS, start_turn
from openmarketer_core.db.models import ChatRole, ChatTurnTrigger
from openmarketer_core.db.research_runs import mark_run_failed
from openmarketer_core.db.run_events import events_of_chat
from openmarketer_core.db.session import transaction
from openmarketer_core.llm import ChatReply, LLMError
from openmarketer_core.orchestrator.chat_agent.rules import SUBMIT
from openmarketer_core.orchestrator.follow_up import NOT_TAKEN
from openmarketer_core.tracing import NOT_TRACING
from openmarketer_worker.orchestrator import steps, wiring
from openmarketer_worker.orchestrator.contract import ChatFollowUpInput, FollowUpNotTaken


class ScriptedModel:
    """Answers with ``answer`` at once, or raises ``error``; counts the calls."""

    def __init__(self, answer: str = "The research found two competitors.", error=None):
        self.answer = answer
        self.error = error
        self.calls = 0

    def chat(self, role, messages, *, tools=None, tool_choice=None) -> ChatReply:
        self.calls += 1
        if self.error is not None:
            raise self.error
        arguments = json.dumps({"answer": self.answer})
        call = {
            "id": "c1",
            "type": "function",
            "function": {"name": SUBMIT, "arguments": arguments},
        }
        message = {"role": "assistant", "content": None, "tool_calls": [call]}
        return ChatReply(message=message, model="fake/model", cost_usd=0.01)


class FollowingUp:
    """The follow-up activity wired with a model per workspace, and the workspaces it was for."""

    def __init__(self, sessions, model=None, *, refused: Exception | None = None):
        self.model = model or ScriptedModel()
        self.models_for: list[uuid.UUID] = []

        def new_model(workspace_id: uuid.UUID):
            self.models_for.append(workspace_id)
            if refused is not None:
                raise refused
            return self.model

        parts = wiring.OrchestratorParts(
            sessions=sessions, new_model=new_model, tracing=NOT_TRACING
        )
        self.activity, self.not_taken = wiring.registration(parts).activities

    async def __call__(self, follow_up: ChatFollowUpInput, *, attempt: int = 1) -> None:
        environment = ActivityEnvironment()
        environment.info = dataclasses.replace(environment.info, attempt=attempt)
        await environment.run(self.activity, follow_up)

    async def tell_not_taken(self, not_taken: FollowUpNotTaken) -> None:
        await ActivityEnvironment().run(self.not_taken, not_taken)


def follow_up_of(asked) -> ChatFollowUpInput:
    return ChatFollowUpInput(
        workspace_id=asked.run.workspace_id,
        project_id=asked.run.project_id,
        chat_id=asked.chat_id,
        run_id=asked.run.run_id,
    )


@pytest.fixture
def ended(database, sessions):
    """A research run a chat asked for, which has failed."""
    asked = database.requested_research()
    with transaction(sessions) as session:
        mark_run_failed(
            session,
            workspace_id=asked.run.workspace_id,
            project_id=asked.run.project_id,
            run_id=asked.run.run_id,
            error="the research did not finish in time",
        )
    return asked


def history(sessions, asked):
    with transaction(sessions) as session:
        return chat_history(
            session,
            workspace_id=asked.run.workspace_id,
            project_id=asked.run.project_id,
            chat_id=asked.chat_id,
        )


async def refusal(
    following_up: FollowingUp, follow_up: ChatFollowUpInput, **kw
) -> ApplicationError:
    with pytest.raises(ApplicationError) as refused:
        await following_up(follow_up, **kw)
    return refused.value


# -------------------------------------------------------------------- success
async def test_follow_up_answers_in_the_chat_with_the_model_of_its_workspace(sessions, ended):
    following_up = FollowingUp(sessions)

    await following_up(follow_up_of(ended))

    assert following_up.models_for == [ended.run.workspace_id]
    [answer] = history(sessions, ended)
    assert (answer.role, answer.content) == (ChatRole.ASSISTANT, following_up.model.answer)


async def test_follow_up_taken_again_does_nothing_the_second_time(sessions, ended):
    following_up = FollowingUp(sessions)

    await following_up(follow_up_of(ended))
    await following_up(follow_up_of(ended), attempt=2)

    assert following_up.model.calls == 1
    assert len(history(sessions, ended)) == 1


# ---------------------------------------------------------- failures worth a retry
async def test_busy_chat_is_a_failure_that_is_tried_again(sessions, ended):
    with transaction(sessions) as session:
        start_turn(
            session,
            workspace_id=ended.run.workspace_id,
            project_id=ended.run.project_id,
            chat_id=ended.chat_id,
            trigger=ChatTurnTrigger.MESSAGE,
        )
    following_up = FollowingUp(sessions)

    refused = await refusal(following_up, follow_up_of(ended))

    assert (refused.type, refused.non_retryable) == (steps.CHAT_BUSY, False)
    assert following_up.model.calls == 0


async def test_provider_that_may_answer_later_is_tried_again(sessions, ended):
    following_up = FollowingUp(sessions, ScriptedModel(error=LLMError("HTTP 503", status=503)))

    refused = await refusal(following_up, follow_up_of(ended))

    assert (refused.type, refused.non_retryable) == (steps.MODEL_UNAVAILABLE, False)
    assert "503" not in refused.message


# ------------------------------------------------------------- failures that end it
async def test_follow_up_that_spent_its_last_turn_ends_without_calling_the_model_again(
    sessions, ended
):
    failing = FollowingUp(sessions, ScriptedModel(error=LLMError("HTTP 503", status=503)))
    for attempt in range(1, MAX_FOLLOW_UP_TURNS + 1):
        await refusal(failing, follow_up_of(ended), attempt=attempt)

    refused = await refusal(failing, follow_up_of(ended), attempt=MAX_FOLLOW_UP_TURNS + 1)

    assert (refused.type, refused.non_retryable) == (steps.FOLLOW_UP_FAILED, True)
    assert failing.model.calls == MAX_FOLLOW_UP_TURNS


async def test_run_that_has_not_ended_has_nothing_to_follow_up_on(sessions, database):
    following_up = FollowingUp(sessions)

    refused = await refusal(following_up, follow_up_of(database.requested_research()))

    assert (refused.type, refused.non_retryable) == (steps.NOTHING_TO_FOLLOW_UP, True)
    assert following_up.model.calls == 0


async def test_request_the_provider_refuses_ends_the_follow_up(sessions, ended):
    following_up = FollowingUp(sessions, ScriptedModel(error=LLMError("HTTP 401", status=401)))

    refused = await refusal(following_up, follow_up_of(ended))

    assert (refused.type, refused.non_retryable) == (steps.FOLLOW_UP_FAILED, True)


async def test_workspace_without_a_model_key_ends_the_follow_up_in_the_words_of_its_error(
    sessions, ended
):
    missing = ModelKeyNotSet("OPENROUTER_API_KEY")
    following_up = FollowingUp(sessions, refused=missing)

    refused = await refusal(following_up, follow_up_of(ended))

    assert (refused.type, refused.non_retryable, refused.message) == (
        steps.FOLLOW_UP_FAILED,
        True,
        str(missing),
    )
    assert list(history(sessions, ended)) == []


async def test_chat_of_another_workspace_has_nothing_to_follow_up_on(sessions, ended, database):
    elsewhere = dataclasses.replace(follow_up_of(ended), workspace_id=database.new_workspace())
    following_up = FollowingUp(sessions)

    refused = await refusal(following_up, elsewhere)

    assert (refused.type, refused.non_retryable) == (steps.NOTHING_TO_FOLLOW_UP, True)
    assert following_up.model.calls == 0


# -------------------------------------------------------- telling the chat it was not taken
async def test_follow_up_not_taken_is_told_in_the_chat_in_a_fixed_sentence(sessions, ended):
    cause = steps.NOT_TAKEN_BECAUSE[steps.CHAT_BUSY]

    await FollowingUp(sessions).tell_not_taken(FollowUpNotTaken(follow_up_of(ended), cause))

    with transaction(sessions) as session:
        [_, failed] = events_of_chat(
            session,
            workspace_id=ended.run.workspace_id,
            project_id=ended.run.project_id,
            chat_id=ended.chat_id,
            after=None,
            limit=10,
        )
    assert failed.detail == {"reason": NOT_TAKEN.format(cause=cause)}


async def test_follow_up_not_taken_with_no_chat_to_tell_ends_quietly(sessions, ended, database):
    elsewhere = dataclasses.replace(follow_up_of(ended), workspace_id=database.new_workspace())

    await FollowingUp(sessions).tell_not_taken(FollowUpNotTaken(elsewhere, "cause"))
