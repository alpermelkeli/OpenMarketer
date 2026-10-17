"""Tests for asking the chat agent one message: the turn's lifecycle, its events, the exchange.

They need PostgreSQL (see the root ``conftest.py``). The use case commits its own short
transactions, so rows are committed here too and read back from other sessions, as a screen
reading the chat would. The model is scripted (no network), and the project's records are
read from the database, as the API reads them.
"""

import json
import uuid
from collections.abc import Callable

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.agent_runtime.gate import RunScope
from openmarketer_core.db.chat import ChatMessage, ChatNotFound, chat_history, create_chat
from openmarketer_core.db.chat_turns import ChatBusy, start_turn
from openmarketer_core.db.models import (
    ChatRole,
    ChatTurnRecord,
    ChatTurnStatus,
    ChatTurnTrigger,
    Project,
    Workspace,
)
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.db.run_events import StoredRunEvent, events_of_chat
from openmarketer_core.db.session import session_factory, transaction
from openmarketer_core.llm import ChatReply, LLMError
from openmarketer_core.orchestrator.ask import ask_project
from openmarketer_core.orchestrator.chat_agent.rules import (
    MODEL_UNAVAILABLE_REASON,
    SUBMIT,
    UNANSWERED_REASON,
    ChatTurnError,
)
from openmarketer_core.orchestrator.events_postgres import PostgresTurnEvents
from openmarketer_core.orchestrator.reads_postgres import PostgresChatReads
from openmarketer_core.run_events import (
    TOOL_ANSWERED,
    TOOL_CALLED,
    TURN_ANSWERED,
    TURN_FAILED,
    TURN_STARTED,
    RunEvent,
)


def call(name: str, **arguments) -> dict:
    return {"id": f"c{id(arguments)}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}  # fmt: skip


class ScriptedModel:
    """Replays prepared assistant turns and records what it was sent.

    A turn that is an exception is raised instead of answered; ``before_each`` is called
    before each answer, for a test that looks at the database while the turn runs.
    """

    def __init__(self, *turns, before_each: Callable[[], None] | None = None):
        self.turns = list(turns)
        self.before_each = before_each
        self.requests: list[dict] = []

    def chat(self, role, messages, *, tools=None, tool_choice=None):
        self.requests.append({"role": role, "messages": [dict(m) for m in messages]})
        if self.before_each is not None:
            self.before_each()
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


def ask(
    sessions: sessionmaker[Session],
    scope: RunScope,
    chat_id: uuid.UUID,
    model: ScriptedModel,
    message: str = "Is it ready?",
    project_id: uuid.UUID | None = None,
):
    # The reads have a session of their own, as the API's request session is.
    with transaction(sessions) as reading:
        return ask_project(
            sessions,
            PostgresChatReads(reading, scope.workspace_id, scope.project_id),
            workspace_id=scope.workspace_id,
            project_id=project_id or scope.project_id,
            chat_id=chat_id,
            message=message,
            model=model,
        )


def stored(sessions, scope: RunScope, chat_id: uuid.UUID) -> list[ChatMessage]:
    with transaction(sessions) as session:
        return list(
            chat_history(
                session,
                workspace_id=scope.workspace_id,
                project_id=scope.project_id,
                chat_id=chat_id,
            )
        )


def events(sessions, scope: RunScope, chat_id: uuid.UUID) -> list[StoredRunEvent]:
    with transaction(sessions) as session:
        return events_of_chat(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            chat_id=chat_id,
            after=None,
            limit=100,
        )


def turns(sessions, chat_id: uuid.UUID) -> list[ChatTurnRecord]:
    with transaction(sessions) as session:
        return list(
            session.scalars(select(ChatTurnRecord).where(ChatTurnRecord.thread_id == chat_id))
        )


def test_a_turn_that_answers_stores_the_exchange_and_reports_the_turn(sessions, scope, chat_id):
    exchange = ask(sessions, scope, chat_id, ScriptedModel([call(SUBMIT, answer="Yes, ready.")]))

    assert (exchange.question.role, exchange.question.content) == (ChatRole.USER, "Is it ready?")
    assert (exchange.answer.role, exchange.answer.content) == (ChatRole.ASSISTANT, "Yes, ready.")
    assert (exchange.model, exchange.cost_usd) == ("fake/model", 0.01)
    assert [message.content for message in stored(sessions, scope, chat_id)] == [
        "Is it ready?",
        "Yes, ready.",
    ]


def test_an_answered_turn_is_recorded_as_answered_and_owns_its_exchange(sessions, scope, chat_id):
    exchange = ask(sessions, scope, chat_id, ScriptedModel([call(SUBMIT, answer="Yes.")]))

    [turn] = turns(sessions, chat_id)
    assert (turn.trigger, turn.status) == (ChatTurnTrigger.MESSAGE, ChatTurnStatus.ANSWERED)
    assert exchange.question.turn_id == exchange.answer.turn_id == turn.id


def test_an_answered_turn_records_its_start_its_tools_and_its_answer(sessions, scope, chat_id):
    model = ScriptedModel([call("get_project_status")], [call(SUBMIT, answer="Yes.")])

    exchange = ask(sessions, scope, chat_id, model)

    recorded = events(sessions, scope, chat_id)
    assert [event.kind for event in recorded] == [
        TURN_STARTED,
        TOOL_CALLED,
        TOOL_ANSWERED,
        TURN_ANSWERED,
    ]
    assert recorded[0].detail == {"trigger": "message"}
    assert recorded[-1].detail == {"message_id": str(exchange.answer.id)}


def test_the_turn_and_its_events_are_visible_to_others_while_it_runs(sessions, scope, chat_id):
    seen: list[tuple[list[str], list[ChatTurnStatus]]] = []

    def look() -> None:
        seen.append(
            (
                [event.kind for event in events(sessions, scope, chat_id)],
                [turn.status for turn in turns(sessions, chat_id)],
            )
        )

    model = ScriptedModel(
        [call("get_project_status")], [call(SUBMIT, answer="Yes.")], before_each=look
    )

    ask(sessions, scope, chat_id, model)

    assert seen == [
        ([TURN_STARTED], [ChatTurnStatus.RUNNING]),
        ([TURN_STARTED, TOOL_CALLED, TOOL_ANSWERED], [ChatTurnStatus.RUNNING]),
    ]


def test_the_turn_is_given_the_stored_conversation_and_the_new_message(sessions, scope, chat_id):
    ask(sessions, scope, chat_id, ScriptedModel([call(SUBMIT, answer="first answer")]), "first")
    model = ScriptedModel([call(SUBMIT, answer="second answer")])

    ask(sessions, scope, chat_id, model, "second")

    conversation = [m for m in model.requests[0]["messages"] if m["role"] != "system"]
    assert [(m["role"], m["content"]) for m in conversation] == [
        ("user", "first"),
        ("assistant", "first answer"),
        ("user", "second"),
    ]
    assert model.requests[0]["role"] == "orchestrator"


def test_a_turn_the_agent_gave_no_answer_for_stores_nothing(sessions, scope, chat_id):
    with pytest.raises(ChatTurnError):
        ask(sessions, scope, chat_id, ScriptedModel(ChatTurnError("no answer after 8 calls")))

    assert stored(sessions, scope, chat_id) == []


def test_a_turn_that_failed_is_recorded_failed_with_a_sentence_for_the_operator(
    sessions, scope, chat_id
):
    with pytest.raises(ChatTurnError):
        ask(sessions, scope, chat_id, ScriptedModel(ChatTurnError("no answer after 8 calls")))

    [turn] = turns(sessions, chat_id)
    assert (turn.status, turn.error) == (ChatTurnStatus.FAILED, UNANSWERED_REASON)
    last = events(sessions, scope, chat_id)[-1]
    assert (last.kind, last.detail) == (TURN_FAILED, {"reason": UNANSWERED_REASON})


def test_a_provider_that_cannot_answer_is_recorded_without_what_it_wrote(sessions, scope, chat_id):
    refusal = LLMError("the model provider answered HTTP 429: slow down", status=429)

    with pytest.raises(LLMError):
        ask(sessions, scope, chat_id, ScriptedModel(refusal))

    assert stored(sessions, scope, chat_id) == []
    last = events(sessions, scope, chat_id)[-1]
    assert (last.kind, last.detail) == (TURN_FAILED, {"reason": MODEL_UNAVAILABLE_REASON})
    assert turns(sessions, chat_id)[0].error == MODEL_UNAVAILABLE_REASON


def test_a_chat_that_is_answering_another_turn_is_busy(sessions, scope, chat_id):
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
        ask(sessions, scope, chat_id, model)

    assert model.requests == []
    assert len(turns(sessions, chat_id)) == 1


def test_asking_about_a_project_the_workspace_does_not_have_is_not_found(sessions, scope, chat_id):
    model = ScriptedModel([call(SUBMIT, answer="unreached")])

    with pytest.raises(ProjectNotFound):
        ask(sessions, scope, chat_id, model, project_id=uuid.uuid4())

    assert model.requests == []


def test_asking_in_a_chat_the_project_does_not_have_is_not_found(sessions, scope):
    model = ScriptedModel([call(SUBMIT, answer="unreached")])

    with pytest.raises(ChatNotFound):
        ask(sessions, scope, uuid.uuid4(), model)

    assert model.requests == []


def test_an_event_of_a_project_removed_while_the_turn_runs_is_dropped(sessions, scope):
    events_of_a_gone_project = PostgresTurnEvents(
        sessions, scope.workspace_id, uuid.uuid4(), uuid.uuid4()
    )

    events_of_a_gone_project.record(RunEvent(TOOL_CALLED, {"tool": "read_profile"}))
