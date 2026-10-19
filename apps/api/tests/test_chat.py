"""Tests for the chat routes: a real database and store, a scripted model instead of a provider.

They need PostgreSQL (see the root ``conftest.py``) and reach no network: the model the route
would resolve is replaced by a scripted one (``chat.chat_model``), while the store, the
project's reads and the key resolution stay the real ones. Each test works in a workspace of
its own, committed, since a turn commits its own transaction.
"""

import base64
import dataclasses
import json
import os
import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_api.dependencies import Services, event_stream_timing, open_event_streams
from openmarketer_api.event_stream import OpenStreams, StreamTiming
from openmarketer_api.identity import LOCAL_USER_ID, current_workspace_id
from openmarketer_api.routes import chat
from openmarketer_core.agent_runtime.gate import RunScope
from openmarketer_core.credentials.catalog import OPENROUTER_API_KEY
from openmarketer_core.credentials.cipher import CredentialCipher
from openmarketer_core.db.chat import append_exchange, create_chat
from openmarketer_core.db.chat_turns import finish_turn, start_turn
from openmarketer_core.db.credentials import store_credential
from openmarketer_core.db.models import ChatTurnTrigger, Project, ResearchRunRecord, Workspace
from openmarketer_core.db.run_events import append_events
from openmarketer_core.db.session import session_factory, transaction
from openmarketer_core.llm import ChatReply, LLMError, RouterChatModel
from openmarketer_core.orchestrator.chat_agent.definitions import CHAT_TURN_KIND
from openmarketer_core.orchestrator.chat_agent.rules import SUBMIT, ChatTurnError
from openmarketer_core.research.workflow_contract import (
    RESEARCH_TASK_QUEUE,
    RESEARCH_WORKFLOW,
    ResearchRunInput,
)
from openmarketer_core.run_events import RunEvent
from openmarketer_core.tracing import (
    NOT_TRACING,
    TracedChatModel,
    Tracing,
    TracingSettings,
    start_tracing,
)
from openmarketer_core.workflow_start import WorkflowNotStarted, WorkflowStart


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

    def tool_results(self) -> list[str]:
        return [m["content"] for m in self.requests[-1]["messages"] if m["role"] == "tool"]


class NoWorkflows:
    """The workflow engine of these tests: a chat turn starts nothing and tells no run."""

    async def start(self, workflow) -> None:
        raise AssertionError("a chat turn starts no workflow")

    async def tell_decided(self, workflow_id: str, proposal_id: uuid.UUID) -> None:
        raise AssertionError("a chat turn tells no run of a decision")


class RecordingWorkflows:
    """The workflow engine of one test: every workflow it was asked to start, and nothing else."""

    def __init__(self) -> None:
        self.started: list[WorkflowStart] = []

    async def start(self, workflow: WorkflowStart) -> None:
        self.started.append(workflow)

    async def tell_decided(self, workflow_id: str, proposal_id: uuid.UUID) -> None:
        raise AssertionError("a chat turn tells no run of a decision")


class RefusingWorkflows(RecordingWorkflows):
    """A workflow engine that cannot be reached: every start is refused."""

    async def start(self, workflow: WorkflowStart) -> None:
        raise WorkflowNotStarted("Temporal is not reachable")


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
            workspace_id=workspace.id, name="App", source_repo_url="https://example.com/a.git"
        )
        session.add(project)
        session.flush()
        return RunScope(workspace_id=workspace.id, project_id=project.id)


@pytest.fixture
def services(app, sessions, scope, repository_models, no_catalog, page_store, asset_store) -> None:
    """The application wired to the test database, this test's workspace, no stored key."""
    app.state.services = Services(
        sessions=sessions,
        workflows=NoWorkflows(),
        environment={},
        credential_cipher=None,
        model_router=repository_models,
        model_catalog=no_catalog,
        page_store=page_store,
        asset_store=asset_store,
    )
    app.dependency_overrides[current_workspace_id] = lambda: scope.workspace_id


def with_the_workflows(app, workflows) -> None:
    """Give the running application another workflow engine, for the test at hand."""
    app.state.services = dataclasses.replace(app.state.services, workflows=workflows)


def wired(app, *turns: object) -> ScriptedModel:
    """Replaces the route's own model resolution with a scripted model, and returns it.

    A turn is one prepared assistant turn: a list of tool calls, a text, or an
    exception to raise.
    """
    model = ScriptedModel(*turns)
    app.dependency_overrides[chat.chat_model] = lambda: model
    return model


def answering(app, answer: str) -> ScriptedModel:
    """A scripted model whose one turn submits ``answer``."""
    return wired(app, [call(SUBMIT, answer=answer)])


def chats_url(scope: RunScope) -> str:
    return f"/v1/projects/{scope.project_id}/chats"


def chat_url(scope: RunScope, chat_id: object) -> str:
    return f"{chats_url(scope)}/{chat_id}"


def messages_url(scope: RunScope, chat_id: object) -> str:
    return chat_url(scope, chat_id) + "/messages"


def read_url(scope: RunScope, chat_id: object) -> str:
    return chat_url(scope, chat_id) + "/read"


def events_url(scope: RunScope, chat_id: object) -> str:
    return chat_url(scope, chat_id) + "/events"


def stream_url(scope: RunScope, chat_id: object) -> str:
    return events_url(scope, chat_id) + "/stream"


def open_chat(client: TestClient, scope: RunScope, title: str | None = None) -> dict:
    response = client.post(chats_url(scope), json=None if title is None else {"title": title})
    assert response.status_code == 201, response.text
    return response.json()


def store(
    sessions: sessionmaker[Session],
    scope: RunScope,
    chat_id: uuid.UUID,
    question: str,
    answer: str,
) -> None:
    in_chat = {
        "workspace_id": scope.workspace_id,
        "project_id": scope.project_id,
        "chat_id": chat_id,
    }
    with transaction(sessions) as session:
        turn = start_turn(session, trigger=ChatTurnTrigger.MESSAGE, **in_chat)
        finish_turn(session, turn_id=turn.id, **in_chat)
        append_exchange(session, turn_id=turn.id, question=question, answer=answer, **in_chat)


def test_a_chat_is_created_empty_with_the_default_name(client, services, scope):
    response = client.post(chats_url(scope), json={})

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "New chat"
    assert body["project_id"] == str(scope.project_id)
    assert (body["last_read_at"], body["unread"], body["busy"]) == (None, False, False)
    listed = client.get(chats_url(scope)).json()
    assert [chat["id"] for chat in listed["chats"]] == [body["id"]]
    assert listed["next_cursor"] is None


def test_a_chat_is_created_with_the_title_it_was_given(client, services, scope):
    response = client.post(chats_url(scope), json={"title": "Q4 plan"})

    assert response.status_code == 201
    assert response.json()["title"] == "Q4 plan"


def test_a_blank_or_too_long_title_is_refused(client, services, scope):
    for title in ("", "   ", "x" * 201, 7):
        response = client.post(chats_url(scope), json={"title": title})
        assert response.status_code == 422, title
    extra = client.post(chats_url(scope), json={"title": "ok", "name": "x"})
    assert extra.status_code == 422
    assert client.get(chats_url(scope)).json()["chats"] == []


def test_the_chats_page_most_recently_active_first(client, services, scope):
    open_chat(client, scope, title="first")
    open_chat(client, scope, title="second")

    page = client.get(chats_url(scope), params={"limit": 1}).json()

    assert [chat["title"] for chat in page["chats"]] == ["second"]
    assert page["next_cursor"] is not None
    rest = client.get(chats_url(scope), params={"limit": 1, "cursor": page["next_cursor"]}).json()
    assert [chat["title"] for chat in rest["chats"]] == ["first"]
    assert rest["next_cursor"] is None


def test_a_cursor_this_list_did_not_write_is_refused(client, services, scope):
    chat = open_chat(client, scope)

    assert client.get(chats_url(scope), params={"cursor": "not-a-cursor"}).status_code == 422
    response = client.get(messages_url(scope, chat["id"]), params={"cursor": "not-a-cursor"})
    assert response.status_code == 422


def test_a_sent_message_is_answered_and_the_exchange_is_stored(client, services, scope, app):
    model = answering(app, "It is ready to analyse.")
    chat = open_chat(client, scope)

    response = client.post(messages_url(scope, chat["id"]), json={"message": "Can I analyse it?"})

    assert response.status_code == 201
    body = response.json()
    assert (body["question"]["role"], body["question"]["content"]) == (
        "user",
        "Can I analyse it?",
    )
    assert (body["question"]["chat_id"], body["answer"]["chat_id"]) == (
        chat["id"],
        chat["id"],
    )
    assert (body["answer"]["role"], body["answer"]["content"]) == (
        "assistant",
        "It is ready to analyse.",
    )
    assert (body["model"], body["cost_usd"]) == ("fake/model", 0.01)
    assert body["question"]["turn_id"] is not None
    assert body["answer"]["turn_id"] == body["question"]["turn_id"]
    assert model.requests[0]["role"] == "orchestrator"
    listed = client.get(messages_url(scope, chat["id"])).json()
    assert [message["content"] for message in listed["messages"]] == [
        "It is ready to analyse.",
        "Can I analyse it?",
    ]
    assert listed["next_cursor"] is None
    assert {message["turn_id"] for message in listed["messages"]} == {body["answer"]["turn_id"]}
    reloaded = client.get(chat_url(scope, chat["id"])).json()
    assert reloaded["title"] == "Can I analyse it?"
    assert (reloaded["unread"], reloaded["busy"]) == (True, False)
    assert reloaded["last_activity_at"] > reloaded["created_at"]


def test_the_turn_reads_the_project_of_the_request_through_the_database(
    client, services, scope, app
):
    model = wired(app, [call("get_project_status")], [call(SUBMIT, answer="ok")])
    chat = open_chat(client, scope)

    client.post(messages_url(scope, chat["id"]), json={"message": "How does it stand?"})

    [read] = model.tool_results()
    assert '"App"' in read


def test_the_turn_is_given_the_stored_conversation(client, services, scope, app, sessions):
    chat = open_chat(client, scope)
    store(sessions, scope, uuid.UUID(chat["id"]), "first question", "first answer")
    model = answering(app, "second answer")

    client.post(messages_url(scope, chat["id"]), json={"message": "and now?"})

    sent = model.requests[0]["messages"]
    conversation = [m for m in sent if m["role"] != "system"]
    assert [(m["role"], m["content"]) for m in conversation] == [
        ("user", "first question"),
        ("assistant", "first answer"),
        ("user", "and now?"),
    ]


def test_the_stored_question_has_no_whitespace_around_it(client, services, scope, app):
    answering(app, "ok")
    chat = open_chat(client, scope)

    client.post(messages_url(scope, chat["id"]), json={"message": "  What is it?  "})

    listed = client.get(messages_url(scope, chat["id"])).json()
    assert listed["messages"][-1]["content"] == "What is it?"


def test_messages_page_newest_first_beside_a_next_cursor(client, services, scope, sessions):
    chat = open_chat(client, scope)
    chat_id = uuid.UUID(chat["id"])
    store(sessions, scope, chat_id, "first question", "first answer")
    store(sessions, scope, chat_id, "second question", "second answer")

    first = client.get(messages_url(scope, chat_id), params={"limit": 2}).json()

    assert [message["content"] for message in first["messages"]] == [
        "second answer",
        "second question",
    ]
    assert first["next_cursor"] is not None
    rest = client.get(
        messages_url(scope, chat_id), params={"limit": 2, "cursor": first["next_cursor"]}
    ).json()
    assert [message["content"] for message in rest["messages"]] == [
        "first answer",
        "first question",
    ]
    assert rest["next_cursor"] is None


def test_messages_of_one_chat_do_not_read_into_another(client, services, scope, sessions):
    first = open_chat(client, scope, title="first")
    second = open_chat(client, scope, title="second")
    store(sessions, scope, uuid.UUID(first["id"]), "first question", "first answer")

    listed = client.get(messages_url(scope, second["id"])).json()

    assert listed["messages"] == []


def test_a_message_that_is_blank_or_too_long_is_refused(client, services, scope, app):
    answering(app, "unreached")
    chat = open_chat(client, scope)

    for body in ("", "   ", "x" * 10_001, None):
        response = client.post(messages_url(scope, chat["id"]), json={"message": body})
        assert response.status_code == 422, body
    extra = client.post(messages_url(scope, chat["id"]), json={"message": "hello", "role": "user"})
    assert extra.status_code == 422


def test_reading_a_chat_marks_it_read(client, services, scope, app):
    wired(app, [call(SUBMIT, answer="ok")], [call(SUBMIT, answer="again")])
    chat = open_chat(client, scope)
    client.post(messages_url(scope, chat["id"]), json={"message": "hello"})
    assert client.get(chat_url(scope, chat["id"])).json()["unread"] is True

    response = client.post(read_url(scope, chat["id"]))

    assert response.status_code == 200
    body = response.json()
    assert body["unread"] is False
    assert body["last_read_at"] is not None
    assert client.get(chat_url(scope, chat["id"])).json()["unread"] is False
    client.post(messages_url(scope, chat["id"]), json={"message": "again"})
    assert client.get(chat_url(scope, chat["id"])).json()["unread"] is True


def test_marking_a_chat_read_takes_no_body(client, services, scope):
    chat = open_chat(client, scope)

    response = client.post(read_url(scope, chat["id"]), json={"read": True})

    assert response.status_code == 422


def test_answering_needs_the_models_key(client, services, scope):
    chat = open_chat(client, scope)

    response = client.post(messages_url(scope, chat["id"]), json={"message": "hello"})

    assert (response.status_code, response.json()["code"]) == (409, "model_key_not_set")
    assert "Settings" in response.json()["message"]
    assert client.get(messages_url(scope, chat["id"])).json()["messages"] == []


def test_a_stored_key_that_cannot_be_opened_is_reported(client, services, scope, sessions):
    chat = open_chat(client, scope)
    cipher = CredentialCipher(base64.b64encode(os.urandom(32)).decode())
    with transaction(sessions) as session:
        sealed = cipher.seal(
            "sk-or-v1-something",
            workspace_id=scope.workspace_id,
            credential_name=OPENROUTER_API_KEY.name,
        )
        store_credential(
            session,
            workspace_id=scope.workspace_id,
            name=OPENROUTER_API_KEY.name,
            sealed=sealed,
            set_by=LOCAL_USER_ID,
        )

    response = client.post(messages_url(scope, chat["id"]), json={"message": "hello"})

    assert (response.status_code, response.json()["code"]) == (409, "credential_unreadable")


def test_a_provider_that_cannot_answer_is_reported_without_its_words(client, services, scope, app):
    chat = open_chat(client, scope)
    wired(app, LLMError("the model provider answered HTTP 429", status=429))

    response = client.post(messages_url(scope, chat["id"]), json={"message": "hello"})

    assert (response.status_code, response.json()) == (
        503,
        {"code": "model_unavailable", "message": "the model provider answered HTTP 429"},
    )
    assert client.get(messages_url(scope, chat["id"])).json()["messages"] == []


def test_a_failed_turn_is_answered_with_a_fixed_message_and_stores_nothing(
    client, services, scope, app
):
    chat = open_chat(client, scope)
    wired(app, ChatTurnError("no answer after 8 model calls"), [call(SUBMIT, answer="second try")])

    first = client.post(messages_url(scope, chat["id"]), json={"message": "hello"})

    assert (
        first.status_code,
        first.json(),
    ) == (
        503,
        {
            "code": "chat_unanswered",
            "message": "the chat agent gave no answer; sending the message again may answer it",
        },
    )
    assert client.get(messages_url(scope, chat["id"])).json()["messages"] == []
    second = client.post(messages_url(scope, chat["id"]), json={"message": "hello"})
    assert second.status_code == 201


def test_a_message_to_a_chat_that_is_answering_another_turn_is_a_conflict(
    client, services, scope, app, sessions
):
    chat = open_chat(client, scope)
    with transaction(sessions) as session:
        start_turn(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            chat_id=uuid.UUID(chat["id"]),
            trigger=ChatTurnTrigger.MESSAGE,
        )
    model = wired(app, [call(SUBMIT, answer="unreached")])

    response = client.post(messages_url(scope, chat["id"]), json={"message": "hello"})

    assert (response.status_code, response.json()["code"]) == (409, "chat_busy")
    assert model.requests == []


def test_the_chat_of_a_project_the_workspace_does_not_have_is_not_found(
    client, services, scope, app
):
    wired(app, [call(SUBMIT, answer="unreached")])
    unknown = uuid.uuid4()
    url = f"/v1/projects/{unknown}/chats"

    assert client.get(url).status_code == 404
    assert client.post(url).status_code == 404
    assert client.get(f"{url}/{uuid.uuid4()}").status_code == 404
    assert client.get(f"{url}/{uuid.uuid4()}/messages").status_code == 404
    assert client.get(f"{url}/{uuid.uuid4()}/events").status_code == 404
    assert client.get(f"{url}/{uuid.uuid4()}/events/stream").status_code == 404
    assert (
        client.post(f"{url}/{uuid.uuid4()}/messages", json={"message": "hello"}).status_code == 404
    )
    assert client.post(f"{url}/{uuid.uuid4()}/read").status_code == 404
    assert client.delete(f"{url}/{uuid.uuid4()}").status_code == 404


def test_a_chat_the_project_does_not_have_is_not_found(client, services, app, scope):
    open_chat(client, scope)
    wired(app, [call(SUBMIT, answer="unreached")])
    unknown = uuid.uuid4()

    responses = [
        client.get(chat_url(scope, unknown)),
        client.get(messages_url(scope, unknown)),
        client.post(messages_url(scope, unknown), json={"message": "hello"}),
        client.post(read_url(scope, unknown)),
        client.get(events_url(scope, unknown)),
        client.get(stream_url(scope, unknown)),
        client.delete(chat_url(scope, unknown)),
    ]

    assert all(response.status_code == 404 for response in responses)
    assert all(response.json()["code"] == "chat_not_found" for response in responses)


def test_a_chat_of_another_project_is_not_found(client, services, app, scope, sessions):
    wired(app, [call(SUBMIT, answer="unreached")])
    with transaction(sessions) as session:
        other = Project(
            workspace_id=scope.workspace_id,
            name="Other",
            source_repo_url="https://example.com/other.git",
        )
        session.add(other)
        session.flush()
        foreign = create_chat(session, workspace_id=scope.workspace_id, project_id=other.id)

    for response in (
        client.get(chat_url(scope, foreign.id)),
        client.get(messages_url(scope, foreign.id)),
        client.post(messages_url(scope, foreign.id), json={"message": "hello"}),
        client.post(read_url(scope, foreign.id)),
        client.get(events_url(scope, foreign.id)),
        client.get(stream_url(scope, foreign.id)),
        client.delete(chat_url(scope, foreign.id)),
    ):
        assert (response.status_code, response.json()["code"]) == (404, "chat_not_found")


# ------------------------------------------------------------- the events
def running_turn_of(sessions: sessionmaker[Session], scope: RunScope, chat_id: object) -> uuid.UUID:
    with transaction(sessions) as session:
        turn = start_turn(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            chat_id=uuid.UUID(str(chat_id)),
            trigger=ChatTurnTrigger.MESSAGE,
        )
        return turn.id


def record(
    sessions: sessionmaker[Session], scope: RunScope, turn_id: uuid.UUID, *tools: str
) -> list[int]:
    """Store a `tool_called` event of the turn for each tool, and return their identifiers."""
    with transaction(sessions) as session:
        stored = append_events(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            run_kind=CHAT_TURN_KIND,
            run_id=turn_id,
            events=[
                RunEvent("tool_called", {"tool": tool, "label": f"Reading {tool}"})
                for tool in tools
            ],
        )
        return [event.id for event in stored]


def quick_streams(app, *, keep_alive_every: float = 60, close_after: float = 0.2) -> None:
    app.dependency_overrides[event_stream_timing] = lambda: StreamTiming(
        poll_every=0.01,
        keep_alive_every=keep_alive_every,
        close_after=close_after,
        reconnect_after=0.5,
    )


def blocks(body: str) -> list[str]:
    return [block for block in body.split("\n\n") if block]


def streamed_events(body: str) -> list[dict]:
    """The events of a stream's body: each one's fields, with its data read as JSON."""
    found = []
    for block in blocks(body):
        fields = dict(line.split(": ", 1) for line in block.split("\n") if not line.startswith(":"))
        if "data" in fields:
            found.append({**fields, "data": json.loads(fields["data"])})
    return found


def test_a_chat_with_a_running_turn_is_busy(client, services, scope, sessions):
    chat = open_chat(client, scope)
    running_turn_of(sessions, scope, chat["id"])

    assert client.get(chat_url(scope, chat["id"])).json()["busy"] is True
    assert [listed["busy"] for listed in client.get(chats_url(scope)).json()["chats"]] == [True]


def test_a_sent_message_is_followed_by_its_turns_events(client, services, scope, app):
    answering(app, "ok")
    chat = open_chat(client, scope)

    turn = client.post(messages_url(scope, chat["id"]), json={"message": "hello"}).json()

    listed = client.get(events_url(scope, chat["id"])).json()["events"]
    assert listed
    assert {(event["run_kind"], event["run_id"]) for event in listed} == {
        (CHAT_TURN_KIND, turn["answer"]["turn_id"])
    }
    assert listed[0]["kind"] == "turn_answered"
    assert listed[0]["detail"] == {"message_id": turn["answer"]["id"]}


def test_events_page_newest_first_beside_a_next_cursor(client, services, scope, sessions):
    chat = open_chat(client, scope)
    turn_id = running_turn_of(sessions, scope, chat["id"])
    first, second, third = record(sessions, scope, turn_id, "status", "profile", "runs")

    page = client.get(events_url(scope, chat["id"]), params={"limit": 2}).json()

    assert [event["id"] for event in page["events"]] == [third, second]
    assert page["events"][0] == {
        "id": third,
        "run_kind": CHAT_TURN_KIND,
        "run_id": str(turn_id),
        "kind": "tool_called",
        "detail": {"tool": "runs", "label": "Reading runs"},
        "created_at": page["events"][0]["created_at"],
    }
    rest = client.get(
        events_url(scope, chat["id"]), params={"limit": 2, "cursor": page["next_cursor"]}
    ).json()
    assert ([event["id"] for event in rest["events"]], rest["next_cursor"]) == ([first], None)


def test_an_event_cursor_this_list_did_not_write_is_refused(client, services, scope):
    chat = open_chat(client, scope)

    for cursor in ("not-a-cursor", "0", "-1", "1.5", "9" * 30):
        response = client.get(events_url(scope, chat["id"]), params={"cursor": cursor})
        assert response.status_code == 422, cursor


def test_events_of_one_chat_do_not_read_into_another(client, services, scope, sessions, app):
    quick_streams(app)
    first = open_chat(client, scope, title="first")
    second = open_chat(client, scope, title="second")
    record(sessions, scope, running_turn_of(sessions, scope, first["id"]), "status")

    assert client.get(events_url(scope, second["id"])).json()["events"] == []
    assert streamed_events(client.get(stream_url(scope, second["id"])).text) == []


def test_the_stream_sends_the_chats_events_as_server_sent_events(
    client, services, scope, sessions, app
):
    quick_streams(app)
    chat = open_chat(client, scope)
    turn_id = running_turn_of(sessions, scope, chat["id"])
    first, second = record(sessions, scope, turn_id, "status", "profile")

    response = client.get(stream_url(scope, chat["id"]))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert blocks(response.text)[0] == "retry: 500"
    listed = client.get(events_url(scope, chat["id"])).json()["events"]
    assert streamed_events(response.text) == [
        {"id": str(event["id"]), "event": "tool_called", "data": event}
        for event in reversed(listed)
    ]
    assert [int(event["id"]) for event in streamed_events(response.text)] == [first, second]


def test_the_stream_starts_after_the_cursor_it_is_given(client, services, scope, sessions, app):
    quick_streams(app)
    chat = open_chat(client, scope)
    first, second, third = record(
        sessions, scope, running_turn_of(sessions, scope, chat["id"]), "a", "b", "c"
    )

    response = client.get(stream_url(scope, chat["id"]), params={"after": first})

    assert [int(event["id"]) for event in streamed_events(response.text)] == [second, third]


def test_a_reconnecting_streams_last_event_id_wins_over_its_after(
    client, services, scope, sessions, app
):
    quick_streams(app)
    chat = open_chat(client, scope)
    first, second, third = record(
        sessions, scope, running_turn_of(sessions, scope, chat["id"]), "a", "b", "c"
    )

    response = client.get(
        stream_url(scope, chat["id"]),
        params={"after": first},
        headers={"Last-Event-ID": str(second)},
    )

    assert [int(event["id"]) for event in streamed_events(response.text)] == [third]


def test_a_cursor_of_the_stream_that_is_not_an_event_id_is_refused(client, services, scope, app):
    quick_streams(app)
    chat = open_chat(client, scope)

    assert client.get(stream_url(scope, chat["id"]), params={"after": "x"}).status_code == 422
    assert client.get(stream_url(scope, chat["id"]), params={"after": -1}).status_code == 422
    response = client.get(stream_url(scope, chat["id"]), headers={"Last-Event-ID": "seven"})
    assert response.status_code == 422


def test_an_idle_stream_sends_keep_alive_comments_until_its_time_is_up(
    client, services, scope, app
):
    quick_streams(app, keep_alive_every=0.03, close_after=0.3)
    chat = open_chat(client, scope)

    response = client.get(stream_url(scope, chat["id"]))

    assert response.status_code == 200
    assert blocks(response.text)[0] == "retry: 500"
    assert blocks(response.text)[1:]
    assert set(blocks(response.text)[1:]) == {": keep-alive"}


def test_events_stored_while_the_stream_is_open_are_sent(client, services, scope, sessions, app):
    quick_streams(app, close_after=1.5)
    chat = open_chat(client, scope)
    turn_id = running_turn_of(sessions, scope, chat["id"])
    [before] = record(sessions, scope, turn_id, "status")
    stored_later: list[int] = []
    later = threading.Timer(0.3, lambda: stored_later.extend(record(sessions, scope, turn_id, "x")))

    later.start()
    response = client.get(stream_url(scope, chat["id"]))
    later.join()

    assert [int(event["id"]) for event in streamed_events(response.text)] == [before, *stored_later]


# ----------------------------------------------------------------- delete
def test_a_deleted_chat_is_gone_with_its_messages(client, services, scope, sessions):
    chat = open_chat(client, scope)
    store(sessions, scope, uuid.UUID(chat["id"]), "Is it ready?", "Yes.")

    response = client.delete(chat_url(scope, chat["id"]))

    assert (response.status_code, response.content) == (204, b"")
    assert client.get(chat_url(scope, chat["id"])).status_code == 404
    assert client.get(messages_url(scope, chat["id"])).status_code == 404
    assert client.get(chats_url(scope)).json()["chats"] == []


def test_deleting_one_chat_keeps_the_project_s_other_chats(client, services, scope):
    deleted = open_chat(client, scope, title="Gone")
    kept = open_chat(client, scope, title="Kept")

    client.delete(chat_url(scope, deleted["id"]))

    assert [chat["title"] for chat in client.get(chats_url(scope)).json()["chats"]] == ["Kept"]
    assert client.get(chat_url(scope, kept["id"])).status_code == 200


def test_deleting_a_chat_takes_no_body(client, services, scope):
    chat = open_chat(client, scope)
    response = client.request("DELETE", chat_url(scope, chat["id"]), json={"force": True})
    assert response.status_code == 422
    assert client.get(chat_url(scope, chat["id"])).status_code == 200


# ------------------------------------------------------- the request action
def test_the_turn_can_start_a_research_run_and_the_workflow_is_started(
    client, services, scope, app, sessions
):
    workflows = RecordingWorkflows()
    with_the_workflows(app, workflows)
    model = wired(
        app,
        [call("start_research", questions=["What did competitors post last month?"])],
        [call(SUBMIT, answer="Research is queued.")],
    )
    chat = open_chat(client, scope)

    response = client.post(
        messages_url(scope, chat["id"]), json={"message": "Research the competition."}
    )

    assert response.status_code == 201, response.text
    assert response.json()["answer"]["content"] == "Research is queued."
    [started] = workflows.started
    assert (started.workflow, started.task_queue) == (RESEARCH_WORKFLOW, RESEARCH_TASK_QUEUE)
    assert isinstance(started.input, ResearchRunInput)
    with transaction(sessions) as session:
        run = session.get(ResearchRunRecord, started.input.run_id)
    assert run is not None
    assert (run.requested_by, run.status.value) == ("orchestrator", "queued")
    # The run is the chat's, which hears back from it when it ends.
    assert str(run.chat_thread_id) == chat["id"]
    assert run.questions == ["What did competitors post last month?"]
    # The model was told the new run's id and status, so the answer can name it.
    told = json.loads(model.tool_results()[0])
    assert told == {
        "run_id": str(started.input.run_id),
        "status": "queued",
        "requested_by": "orchestrator",
    }


def test_a_research_run_whose_workflow_cannot_be_started_is_answered_in_words(
    client, services, scope, app, sessions
):
    with_the_workflows(app, RefusingWorkflows())
    wired(
        app,
        [call("start_research", questions=["What did competitors post last month?"])],
        [call(SUBMIT, answer="I could not start it.")],
    )
    chat = open_chat(client, scope)

    response = client.post(messages_url(scope, chat["id"]), json={"message": "Research it."})

    assert response.status_code == 201, response.text
    assert response.json()["answer"]["content"] == "I could not start it."
    with transaction(sessions) as session:
        stored = session.scalars(
            select(ResearchRunRecord).where(ResearchRunRecord.project_id == scope.project_id)
        ).all()
    # The run was stored and then recorded as failed: it will never be executed.
    assert len(stored) == 1
    assert stored[0].status.value == "failed"
    assert stored[0].error is not None


def test_a_turn_that_does_not_ask_for_research_starts_nothing(client, services, scope, app):
    workflows = RecordingWorkflows()
    with_the_workflows(app, workflows)
    answering(app, "Nothing to start.")
    chat = open_chat(client, scope)

    response = client.post(messages_url(scope, chat["id"]), json={"message": "How is it going?"})

    assert response.status_code == 201
    assert workflows.started == []


# -------------------------------------------------------------------- tracing
def started_tracing() -> tuple[Tracing, InMemorySpanExporter]:
    """Tracing as a test starts it: on, with the spans kept instead of sent to a Langfuse.

    A public key of its own, as Langfuse keeps one client per key.
    """
    exporter = InMemorySpanExporter()
    traced = start_tracing(
        TracingSettings(
            public_key=f"pk-lf-{uuid.uuid4()}",
            secret_key="sk-lf-made-up",
            base_url="http://127.0.0.1:1",
        ),
        span_exporter=exporter,
    )
    return traced, exporter


def stored_provider_key(sessions: sessionmaker[Session], scope: RunScope) -> CredentialCipher:
    """A key stored for the test's workspace, and the cipher it can be opened with."""
    cipher = CredentialCipher(base64.b64encode(os.urandom(32)).decode())
    with transaction(sessions) as session:
        sealed = cipher.seal(
            "sk-or-v1-made-up",
            workspace_id=scope.workspace_id,
            credential_name=OPENROUTER_API_KEY.name,
        )
        store_credential(
            session,
            workspace_id=scope.workspace_id,
            name=OPENROUTER_API_KEY.name,
            sealed=sealed,
            set_by=LOCAL_USER_ID,
        )
    return cipher


def test_the_turns_model_is_wrapped_for_tracing_when_tracing_is_on(
    sessions, scope, repository_models
):
    cipher = stored_provider_key(sessions, scope)
    tracing, _ = started_tracing()
    try:
        with transaction(sessions) as session:
            model = chat.chat_model(
                session, scope.workspace_id, repository_models, {}, cipher, tracing
            )
    finally:
        tracing.stop()

    assert isinstance(model, TracedChatModel)
    assert isinstance(model.inner, RouterChatModel)


def test_the_turns_model_is_used_as_it_is_while_tracing_is_off(sessions, scope, repository_models):
    cipher = stored_provider_key(sessions, scope)
    with transaction(sessions) as session:
        model = chat.chat_model(
            session, scope.workspace_id, repository_models, {}, cipher, NOT_TRACING
        )

    assert isinstance(model, RouterChatModel)
    assert not isinstance(model, TracedChatModel)


def test_a_traced_turn_sends_its_run_its_nodes_and_its_generations(client, services, scope, app):
    tracing, exporter = started_tracing()
    app.state.services = dataclasses.replace(app.state.services, tracing=tracing)
    model = tracing.model(ScriptedModel([call(SUBMIT, answer="hello there")]))
    app.dependency_overrides[chat.chat_model] = lambda: model
    opened = open_chat(client, scope)
    try:
        response = client.post(messages_url(scope, opened["id"]), json={"message": "hi"})
        assert response.status_code == 201, response.text
        tracing.flush()
    finally:
        tracing.stop()

    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert set(spans) >= {"orchestrator-chat", "call_model", "orchestrator"}
    by_id = {span.context.span_id: span.name for span in spans.values() if span.context}
    assert spans["call_model"].parent is not None
    assert spans["orchestrator"].parent is not None
    assert by_id[spans["call_model"].parent.span_id] == "orchestrator-chat"
    assert by_id[spans["orchestrator"].parent.span_id] == "call_model"


def test_a_stream_beyond_the_servers_limit_is_refused_until_one_ends(client, services, scope, app):
    quick_streams(app)
    streams = OpenStreams(limit=1)
    app.dependency_overrides[open_event_streams] = lambda: streams
    chat = open_chat(client, scope)
    held = streams.take()

    refused = client.get(stream_url(scope, chat["id"]))
    held.give_back()
    served = client.get(stream_url(scope, chat["id"]))

    assert (refused.status_code, refused.json()["code"]) == (503, "too_many_streams")
    assert served.status_code == 200
    # The stream that ended gave its place back.
    assert streams.count == 0


def test_a_title_with_a_line_separator_reaches_the_stream_whole(
    client, services, scope, sessions, app
):
    quick_streams(app)
    chat = open_chat(client, scope)
    turn_id = running_turn_of(sessions, scope, chat["id"])
    title = "Pricing\u2028page\u0085of a competitor"
    with transaction(sessions) as session:
        append_events(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            run_kind=CHAT_TURN_KIND,
            run_id=turn_id,
            events=[RunEvent("fetched", {"url": "https://a.example", "title": title})],
        )

    [event] = streamed_events(client.get(stream_url(scope, chat["id"])).text)

    assert event["data"]["detail"]["title"] == title
