"""Tests for asking the chat agent one message and storing the exchange in a chat.

They need PostgreSQL (see the root ``conftest.py``); the model is scripted (no
network), and the project's records are read from the database, as the API reads them.
"""

import json
import uuid

import pytest
from sqlalchemy.orm import Session

from openmarketer_core.agent_runtime.gate import RunScope
from openmarketer_core.db.chat import (
    ChatMessage,
    ChatNotFound,
    ChatThread,
    chat_history,
    create_chat,
)
from openmarketer_core.db.models import ChatRole, Project, Workspace
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.llm import ChatReply, LLMError
from openmarketer_core.orchestrator.ask import ask_project
from openmarketer_core.orchestrator.chat_agent.rules import SUBMIT, ChatTurnError
from openmarketer_core.orchestrator.reads_postgres import PostgresChatReads


def call(name: str, **arguments) -> dict:
    return {"id": f"c{id(arguments)}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}  # fmt: skip


class ScriptedModel:
    """Replays prepared assistant turns and records what it was sent.

    A turn that is an exception is raised instead of answered.
    """

    def __init__(self, *turns, cost: float = 0.01):
        self.turns = list(turns)
        self.cost = cost
        self.requests: list[dict] = []

    def chat(self, role, messages, *, tools=None, tool_choice=None):
        self.requests.append(
            {"role": role, "messages": [dict(m) for m in messages], "tool_choice": tool_choice}
        )
        turn = self.turns.pop(0) if self.turns else [call("get_project_status")]
        if isinstance(turn, Exception):
            raise turn
        if isinstance(turn, str):
            message = {"role": "assistant", "content": turn}
        else:
            message = {"role": "assistant", "content": None, "tool_calls": turn}
        return ChatReply(message=message, model="fake/model", cost_usd=self.cost)


def new_scope(session: Session) -> RunScope:
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
def scope(session) -> RunScope:
    return new_scope(session)


@pytest.fixture
def chat(session, scope) -> ChatThread:
    return create_chat(session, workspace_id=scope.workspace_id, project_id=scope.project_id)


def reads_of(session: Session, scope: RunScope) -> PostgresChatReads:
    return PostgresChatReads(session, scope.workspace_id, scope.project_id)


def stored(session: Session, scope: RunScope, chat: ChatThread) -> list[ChatMessage]:
    return list(
        chat_history(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            chat_id=chat.id,
        )
    )


def test_a_turn_that_answers_stores_the_exchange_and_reports_the_turn(session, scope, chat):
    model = ScriptedModel([call(SUBMIT, answer="Yes, it is ready.")])

    exchange = ask_project(
        session,
        reads_of(session, scope),
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        chat_id=chat.id,
        message="Is it ready?",
        model=model,
    )

    assert (exchange.question.role, exchange.question.content) == (
        ChatRole.USER,
        "Is it ready?",
    )
    assert (exchange.answer.role, exchange.answer.content) == (
        ChatRole.ASSISTANT,
        "Yes, it is ready.",
    )
    assert (exchange.question.thread_id, exchange.answer.thread_id) == (chat.id, chat.id)
    assert (exchange.model, exchange.cost_usd) == ("fake/model", 0.01)
    assert [message.content for message in stored(session, scope, chat)] == [
        "Is it ready?",
        "Yes, it is ready.",
    ]


def test_the_turn_is_given_the_stored_conversation_and_the_new_message(session, scope, chat):
    first = ScriptedModel([call(SUBMIT, answer="first answer")])
    ask_project(
        session,
        reads_of(session, scope),
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        chat_id=chat.id,
        message="first question",
        model=first,
    )
    model = ScriptedModel([call(SUBMIT, answer="second answer")])

    ask_project(
        session,
        reads_of(session, scope),
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        chat_id=chat.id,
        message="second question",
        model=model,
    )

    sent = model.requests[0]["messages"]
    conversation = [m for m in sent if m["role"] != "system"]
    assert [(m["role"], m["content"]) for m in conversation] == [
        ("user", "first question"),
        ("assistant", "first answer"),
        ("user", "second question"),
    ]
    assert model.requests[0]["role"] == "orchestrator"


def test_a_turn_the_agent_gave_no_answer_for_stores_nothing(session, scope, chat):
    model = ScriptedModel(ChatTurnError("no answer after 8 model calls"))

    with pytest.raises(ChatTurnError):
        ask_project(
            session,
            reads_of(session, scope),
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            chat_id=chat.id,
            message="Is it ready?",
            model=model,
        )

    assert stored(session, scope, chat) == []


def test_a_provider_that_cannot_answer_stores_nothing(session, scope, chat):
    model = ScriptedModel(LLMError("the model provider answered HTTP 429", status=429))

    with pytest.raises(LLMError):
        ask_project(
            session,
            reads_of(session, scope),
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            chat_id=chat.id,
            message="Is it ready?",
            model=model,
        )

    assert stored(session, scope, chat) == []


def test_asking_about_a_project_the_workspace_does_not_have_is_not_found(session, scope, chat):
    model = ScriptedModel([call(SUBMIT, answer="unreached")])

    with pytest.raises(ProjectNotFound):
        ask_project(
            session,
            PostgresChatReads(session, scope.workspace_id, uuid.uuid4()),
            workspace_id=scope.workspace_id,
            project_id=uuid.uuid4(),
            chat_id=chat.id,
            message="Is it ready?",
            model=model,
        )

    assert model.requests == []


def test_asking_in_a_chat_the_project_does_not_have_is_not_found(session, scope):
    model = ScriptedModel([call(SUBMIT, answer="unreached")])

    with pytest.raises(ChatNotFound):
        ask_project(
            session,
            reads_of(session, scope),
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            chat_id=uuid.uuid4(),
            message="Is it ready?",
            model=model,
        )

    assert model.requests == []
