"""Tests for following up in a chat on a research run it started, once the run has ended.

They need PostgreSQL (see the root ``conftest.py``). The use case commits its own short
transactions, so rows are committed here too. The model is scripted (no network).
"""

import json
import uuid

import pytest
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.agent_runtime.gate import RunScope
from openmarketer_core.db.chat import chat_history, create_chat
from openmarketer_core.db.chat_turns import (
    MAX_FOLLOW_UP_TURNS,
    ChatBusy,
    FollowUpGivenUp,
    start_turn,
)
from openmarketer_core.db.models import ChatRole, ChatTurnTrigger, Project, Workspace
from openmarketer_core.db.research_runs import (
    ResearchRunNotFound,
    mark_run_failed,
    request_research_run,
    research_run,
)
from openmarketer_core.db.run_events import events_of_chat
from openmarketer_core.db.session import session_factory, transaction
from openmarketer_core.llm import ChatReply
from openmarketer_core.orchestrator import follow_up as follow_up_module
from openmarketer_core.orchestrator.chat_agent.rules import SUBMIT, ChatTurnError
from openmarketer_core.orchestrator.follow_up import (
    NOT_TAKEN,
    ResearchRunNotEnded,
    follow_up_on_research,
    record_follow_up_not_taken,
)
from openmarketer_core.orchestrator.reads_postgres import PostgresChatReads
from openmarketer_core.research.questions import ResearchRequest
from openmarketer_core.research.request import REQUESTED_BY_THE_CHAT
from openmarketer_core.run_events import TURN_ANSWERED, TURN_STARTED


def call(name: str, **arguments) -> dict:
    return {"id": f"c{id(arguments)}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}  # fmt: skip


class ScriptedModel:
    """Replays prepared assistant turns and records what it was sent."""

    def __init__(self, *turns):
        self.turns = list(turns)
        self.requests: list[dict] = []

    def chat(self, role, messages, *, tools=None, tool_choice=None):
        self.requests.append({"messages": [dict(m) for m in messages], "tools": tools})
        turn = self.turns.pop(0) if self.turns else [call("get_project_status")]
        if isinstance(turn, Exception):
            raise turn
        message = {"role": "assistant", "content": None, "tool_calls": turn}
        return ChatReply(message=message, model="fake/model", cost_usd=0.01)


@pytest.fixture
def sessions(engine) -> sessionmaker[Session]:
    return session_factory(engine.url.render_as_string(hide_password=False))


@pytest.fixture
def scope(sessions) -> RunScope:
    with transaction(sessions) as session:
        workspace = Workspace(name="Acme")
        session.add(workspace)
        session.flush()
        project = Project(
            workspace_id=workspace.id, name="Example", source_repo_url="https://example.com/r.git"
        )
        session.add(project)
        session.flush()
        return RunScope(workspace_id=workspace.id, project_id=project.id)


@pytest.fixture
def chat_id(sessions, scope) -> uuid.UUID:
    with transaction(sessions) as session:
        return create_chat(session, workspace_id=scope.workspace_id, project_id=scope.project_id).id


@pytest.fixture
def run_id(sessions, scope, chat_id) -> uuid.UUID:
    """A research run the chat asked for, which failed."""
    with transaction(sessions) as session:
        run = request_research_run(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            request=ResearchRequest(questions=["Who are the competitors?"]),
            requested_by=REQUESTED_BY_THE_CHAT,
            chat_thread_id=chat_id,
        )
        mark_run_failed(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            run_id=run.id,
            error="the research could not be handed to a worker",
        )
        return run.id


def follow_up(sessions, scope: RunScope, chat_id: uuid.UUID, run_id: uuid.UUID, model):
    with transaction(sessions) as reading:
        return follow_up_on_research(
            sessions,
            PostgresChatReads(reading, scope.workspace_id, scope.project_id),
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            chat_id=chat_id,
            run_id=run_id,
            model=model,
        )


def history(sessions, scope: RunScope, chat_id: uuid.UUID):
    with transaction(sessions) as session:
        return chat_history(
            session, workspace_id=scope.workspace_id, project_id=scope.project_id, chat_id=chat_id
        )


def test_a_follow_up_stores_one_answer_with_no_question_before_it(sessions, scope, chat_id, run_id):
    done = follow_up(
        sessions, scope, chat_id, run_id, ScriptedModel([call(SUBMIT, answer="It failed.")])
    )

    assert done is not None
    [message] = history(sessions, scope, chat_id)
    assert (message.role, message.content) == (ChatRole.ASSISTANT, "It failed.")
    assert message.id == done.answer.id
    assert message.turn_id is not None


def test_a_follow_up_records_its_turn_as_one_that_followed_up(sessions, scope, chat_id, run_id):
    done = follow_up(sessions, scope, chat_id, run_id, ScriptedModel([call(SUBMIT, answer="x")]))
    assert done is not None

    with transaction(sessions) as session:
        recorded = events_of_chat(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            chat_id=chat_id,
            after=None,
            limit=100,
        )
    assert [(event.kind, event.detail) for event in recorded] == [
        (TURN_STARTED, {"trigger": "research_finished"}),
        (TURN_ANSWERED, {"message_id": str(done.answer.id)}),
    ]


def test_the_model_is_told_of_the_run_and_the_instruction_is_not_stored(
    sessions, scope, chat_id, run_id
):
    model = ScriptedModel([call(SUBMIT, answer="It failed.")])

    follow_up(sessions, scope, chat_id, run_id, model)

    instruction = model.requests[0]["messages"][-1]
    assert instruction["role"] == "user"
    assert str(run_id) in instruction["content"]
    assert "failed" in instruction["content"]
    assert "read_research_report" in instruction["content"]
    assert [message.content for message in history(sessions, scope, chat_id)] == ["It failed."]


def test_a_follow_up_is_not_offered_to_start_another_run(sessions, scope, chat_id, run_id):
    model = ScriptedModel([call(SUBMIT, answer="It failed.")])

    follow_up(sessions, scope, chat_id, run_id, model)

    offered = [schema["function"]["name"] for schema in model.requests[0]["tools"]]
    assert "start_research" not in offered
    assert "read_research_report" in offered


def test_a_follow_up_that_calls_start_research_anyway_starts_nothing(
    sessions, scope, chat_id, run_id
):
    model = ScriptedModel(
        [call("start_research", questions=["Again?"])], [call(SUBMIT, answer="Not started.")]
    )

    follow_up(sessions, scope, chat_id, run_id, model)

    [result] = [m for m in model.requests[1]["messages"] if m["role"] == "tool"]
    assert result["content"].startswith("error:")


def test_a_follow_up_asked_for_again_after_it_answered_does_nothing(
    sessions, scope, chat_id, run_id
):
    follow_up(sessions, scope, chat_id, run_id, ScriptedModel([call(SUBMIT, answer="Once.")]))
    again = ScriptedModel([call(SUBMIT, answer="Twice.")])

    assert follow_up(sessions, scope, chat_id, run_id, again) is None
    assert again.requests == []
    assert [message.content for message in history(sessions, scope, chat_id)] == ["Once."]


def test_a_follow_up_that_failed_is_taken_again_when_asked_again(sessions, scope, chat_id, run_id):
    with pytest.raises(ChatTurnError):
        follow_up(sessions, scope, chat_id, run_id, ScriptedModel(ChatTurnError("no answer")))

    done = follow_up(sessions, scope, chat_id, run_id, ScriptedModel([call(SUBMIT, answer="x")]))

    assert done is not None


def test_a_follow_up_in_a_busy_chat_is_busy(sessions, scope, chat_id, run_id):
    with transaction(sessions) as session:
        start_turn(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            chat_id=chat_id,
            trigger=ChatTurnTrigger.MESSAGE,
        )
    model = ScriptedModel([call(SUBMIT, answer="unreached")])

    with pytest.raises(ChatBusy):
        follow_up(sessions, scope, chat_id, run_id, model)

    assert model.requests == []


def test_a_follow_up_on_a_run_the_chat_did_not_ask_for_is_not_found(sessions, scope, chat_id):
    model = ScriptedModel([call(SUBMIT, answer="unreached")])

    with pytest.raises(ResearchRunNotFound):
        follow_up(sessions, scope, chat_id, uuid.uuid4(), model)

    assert model.requests == []


def test_a_run_that_has_not_ended_has_nothing_to_follow_up_on(sessions, scope, chat_id):
    with transaction(sessions) as session:
        queued = request_research_run(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            request=ResearchRequest(questions=["Who are the competitors?"]),
            requested_by=REQUESTED_BY_THE_CHAT,
            chat_thread_id=chat_id,
        ).id
    model = ScriptedModel([call(SUBMIT, answer="unreached")])

    with pytest.raises(ResearchRunNotEnded):
        follow_up(sessions, scope, chat_id, queued, model)

    assert model.requests == []


def test_an_attempt_that_answers_while_another_prepares_stops_the_other_at_its_start(
    sessions, scope, chat_id, run_id, monkeypatch
):
    # Attempt A answers between attempt B's first read and B's turn: B must not answer again.
    read_the_run = research_run
    first = ScriptedModel([call(SUBMIT, answer="From A.")])

    def read_while_a_answers(*args, **kwargs):
        monkeypatch.setattr(follow_up_module, "research_run", read_the_run)
        follow_up(sessions, scope, chat_id, run_id, first)
        return read_the_run(*args, **kwargs)

    monkeypatch.setattr(follow_up_module, "research_run", read_while_a_answers)
    second = ScriptedModel([call(SUBMIT, answer="From B.")])

    assert follow_up(sessions, scope, chat_id, run_id, second) is None
    assert second.requests == []
    assert [message.content for message in history(sessions, scope, chat_id)] == ["From A."]


def test_a_follow_up_that_failed_its_last_allowed_turn_is_given_up(
    sessions, scope, chat_id, run_id
):
    for _ in range(MAX_FOLLOW_UP_TURNS):
        with pytest.raises(ChatTurnError):
            follow_up(sessions, scope, chat_id, run_id, ScriptedModel(ChatTurnError("no")))
    model = ScriptedModel([call(SUBMIT, answer="unreached")])

    with pytest.raises(FollowUpGivenUp):
        follow_up(sessions, scope, chat_id, run_id, model)

    assert model.requests == []


def test_a_follow_up_not_taken_tells_the_chat_why_in_a_fixed_sentence(
    sessions, scope, chat_id, run_id
):
    recorded = record_follow_up_not_taken(
        sessions,
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        chat_id=chat_id,
        run_id=run_id,
        cause="the chat stayed busy",
    )

    assert recorded
    with transaction(sessions) as session:
        last = events_of_chat(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            chat_id=chat_id,
            after=None,
            limit=100,
        )[-1]
    assert last.detail == {"reason": NOT_TAKEN.format(cause="the chat stayed busy")}
    assert list(history(sessions, scope, chat_id)) == []
