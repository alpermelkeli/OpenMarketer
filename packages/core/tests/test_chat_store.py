"""Tests for the chat store: a project's chats, their exchanges, and reading them back.

They need PostgreSQL (see the root ``conftest.py``).
"""

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from openmarketer_core.agent_runtime.gate import RunScope
from openmarketer_core.db.chat import (
    DEFAULT_CHAT_TITLE,
    ChatNotFound,
    ChatThread,
    append_exchange,
    chat_history,
    create_chat,
    delete_chat,
    get_chat,
    list_chat_messages,
    list_chats,
    mark_chat_read,
)
from openmarketer_core.db.models import ChatMessageRecord, ChatRole, Project, Workspace
from openmarketer_core.db.projects import ProjectNotFound


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


def open_chat(session: Session, scope: RunScope, title: str | None = None) -> ChatThread:
    return create_chat(
        session, workspace_id=scope.workspace_id, project_id=scope.project_id, title=title
    )


def store(session: Session, scope: RunScope, chat_id: uuid.UUID, question: str, answer: str):
    return append_exchange(
        session,
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        chat_id=chat_id,
        question=question,
        answer=answer,
    )


def listed(session: Session, scope: RunScope, chat_id: uuid.UUID, **kwargs) -> list[str]:
    page = list_chat_messages(
        session,
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        chat_id=chat_id,
        **kwargs,
    )
    return [message.content for message in page.items]


def stored_thread(session: Session, scope: RunScope, chat_id: uuid.UUID) -> ChatThread:
    return get_chat(
        session, workspace_id=scope.workspace_id, project_id=scope.project_id, chat_id=chat_id
    )


def test_a_new_chat_is_empty_and_not_unread(session, scope):
    chat = open_chat(session, scope)

    assert (chat.project_id, chat.title) == (scope.project_id, DEFAULT_CHAT_TITLE)
    assert (chat.last_read_at, chat.unread) == (None, False)
    assert listed(session, scope, chat.id, limit=10) == []


def test_a_chat_is_created_with_the_name_it_was_given(session, scope):
    chat = open_chat(session, scope, title="Q4 plan")

    assert chat.title == "Q4 plan"


def test_an_exchange_reads_back_as_its_question_then_its_answer(session, scope):
    chat = open_chat(session, scope)

    question, answer = store(session, scope, chat.id, "Is it ready?", "Yes.")

    assert (question.role, question.content) == (ChatRole.USER, "Is it ready?")
    assert (answer.role, answer.content) == (ChatRole.ASSISTANT, "Yes.")
    assert (question.project_id, answer.project_id) == (scope.project_id, scope.project_id)
    assert (question.thread_id, answer.thread_id) == (chat.id, chat.id)
    assert question.created_at < answer.created_at
    assert [
        message.content
        for message in chat_history(
            session, workspace_id=scope.workspace_id, project_id=scope.project_id, chat_id=chat.id
        )
    ] == ["Is it ready?", "Yes."]


def test_the_list_holds_the_newest_message_first(session, scope):
    chat = open_chat(session, scope)
    store(session, scope, chat.id, "first question", "first answer")
    store(session, scope, chat.id, "second question", "second answer")

    page = list_chat_messages(
        session,
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        chat_id=chat.id,
        limit=10,
    )

    assert [message.content for message in page.items] == [
        "second answer",
        "second question",
        "first answer",
        "first question",
    ]
    assert page.next_before is None


def test_the_history_holds_every_message_oldest_first(session, scope):
    chat = open_chat(session, scope)
    store(session, scope, chat.id, "first question", "first answer")
    store(session, scope, chat.id, "second question", "second answer")

    history = chat_history(
        session, workspace_id=scope.workspace_id, project_id=scope.project_id, chat_id=chat.id
    )

    assert [message.content for message in history] == [
        "first question",
        "first answer",
        "second question",
        "second answer",
    ]


def test_the_list_pages_backwards_from_the_last_message_of_the_page(session, scope):
    chat = open_chat(session, scope)
    store(session, scope, chat.id, "q1", "a1")
    store(session, scope, chat.id, "q2", "a2")
    store(session, scope, chat.id, "q3", "a3")

    first = list_chat_messages(
        session,
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        chat_id=chat.id,
        limit=2,
    )
    second = list_chat_messages(
        session,
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        chat_id=chat.id,
        limit=2,
        before=first.next_before,
    )
    third = list_chat_messages(
        session,
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        chat_id=chat.id,
        limit=2,
        before=second.next_before,
    )

    assert [[m.content for m in page.items] for page in (first, second, third)] == [
        ["a3", "q3"],
        ["a2", "q2"],
        ["a1", "q1"],
    ]
    assert (first.next_before is not None, second.next_before is not None) == (True, True)
    assert third.next_before is None


def test_messages_of_one_chat_do_not_read_into_another(session, scope):
    first = open_chat(session, scope)
    second = open_chat(session, scope)
    store(session, scope, first.id, "first question", "first answer")
    store(session, scope, second.id, "second question", "second answer")

    assert listed(session, scope, first.id, limit=10) == ["first answer", "first question"]
    assert listed(session, scope, second.id, limit=10) == ["second answer", "second question"]


def test_the_chats_are_listed_most_recently_active_first(session, scope):
    first = open_chat(session, scope)
    second = open_chat(session, scope)

    page = list_chats(session, workspace_id=scope.workspace_id, project_id=scope.project_id,
                     limit=10)  # fmt: skip
    assert [chat.id for chat in page.items] == [second.id, first.id]

    store(session, scope, first.id, "question", "answer")

    page = list_chats(session, workspace_id=scope.workspace_id, project_id=scope.project_id,
                     limit=10)  # fmt: skip
    assert [chat.id for chat in page.items] == [first.id, second.id]


def test_the_chat_list_pages_backwards_from_the_last_chat_of_the_page(session, scope):
    open_chat(session, scope, title="first")
    open_chat(session, scope, title="second")
    open_chat(session, scope, title="third")

    page = list_chats(session, workspace_id=scope.workspace_id, project_id=scope.project_id,
                     limit=2)  # fmt: skip
    assert [chat.title for chat in page.items] == ["third", "second"]
    assert page.next_before is not None

    rest = list_chats(session, workspace_id=scope.workspace_id, project_id=scope.project_id,
                      limit=2, before=page.next_before)  # fmt: skip
    assert [chat.title for chat in rest.items] == ["first"]
    assert rest.next_before is None


def test_a_message_leaves_the_chat_unread_until_it_is_marked_read(session, scope):
    chat = open_chat(session, scope)
    store(session, scope, chat.id, "question", "answer")

    assert stored_thread(session, scope, chat.id).unread is True
    assert list_chats(session, workspace_id=scope.workspace_id, project_id=scope.project_id,
                      limit=10).items[0].unread is True  # fmt: skip

    marked = mark_chat_read(
        session, workspace_id=scope.workspace_id, project_id=scope.project_id, chat_id=chat.id
    )

    assert (marked.unread, marked.last_read_at is not None) == (False, True)
    assert stored_thread(session, scope, chat.id).last_read_at == marked.last_read_at
    assert list_chats(session, workspace_id=scope.workspace_id, project_id=scope.project_id,
                      limit=10).items[0].unread is False  # fmt: skip

    store(session, scope, chat.id, "another question", "another answer")

    assert stored_thread(session, scope, chat.id).unread is True


def test_a_chat_takes_its_name_from_the_first_message(session, scope):
    chat = open_chat(session, scope)

    store(session, scope, chat.id, "Fix the pricing copy\nand the FAQ", "Done.")

    assert stored_thread(session, scope, chat.id).title == "Fix the pricing copy"


def test_the_automatic_name_is_cut_at_a_word_boundary(session, scope):
    chat = open_chat(session, scope)

    store(session, scope, chat.id, "a" * 59 + " " + "b" * 10, "Done.")

    assert stored_thread(session, scope, chat.id).title == "a" * 59


def test_a_message_without_a_first_line_keeps_the_default_name(session, scope):
    chat = open_chat(session, scope)

    store(session, scope, chat.id, "\nSecond line", "Done.")

    assert stored_thread(session, scope, chat.id).title == DEFAULT_CHAT_TITLE


def test_a_chat_named_by_the_operator_keeps_its_name(session, scope):
    chat = open_chat(session, scope, title="Q4 plan")

    store(session, scope, chat.id, "Turn it into a post", "Done.")

    assert stored_thread(session, scope, chat.id).title == "Q4 plan"


def test_the_chat_of_an_unknown_project_is_not_found(session, scope):
    unknown = uuid.uuid4()

    with pytest.raises(ProjectNotFound):
        list_chats(session, workspace_id=scope.workspace_id, project_id=unknown, limit=10)
    with pytest.raises(ProjectNotFound):
        open_chat(session, RunScope(workspace_id=scope.workspace_id, project_id=unknown))
    with pytest.raises(ProjectNotFound):
        stored_thread(session, RunScope(workspace_id=scope.workspace_id, project_id=unknown),
                      uuid.uuid4())  # fmt: skip


def test_a_chat_the_project_does_not_have_is_not_found(session, scope):
    unknown = uuid.uuid4()

    with pytest.raises(ChatNotFound):
        stored_thread(session, scope, unknown)
    with pytest.raises(ChatNotFound):
        listed(session, scope, unknown, limit=10)
    with pytest.raises(ChatNotFound):
        chat_history(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            chat_id=unknown,
        )
    with pytest.raises(ChatNotFound):
        store(session, scope, unknown, "question", "answer")
    with pytest.raises(ChatNotFound):
        mark_chat_read(
            session, workspace_id=scope.workspace_id, project_id=scope.project_id, chat_id=unknown
        )


def test_the_chat_of_another_project_is_not_found(session, scope):
    other = new_scope(session)
    foreign = open_chat(session, other)

    with pytest.raises(ChatNotFound):
        stored_thread(session, scope, foreign.id)
    with pytest.raises(ChatNotFound):
        listed(session, scope, foreign.id, limit=10)
    with pytest.raises(ChatNotFound):
        store(session, scope, foreign.id, "question", "answer")
    with pytest.raises(ChatNotFound):
        mark_chat_read(
            session, workspace_id=scope.workspace_id, project_id=scope.project_id,
            chat_id=foreign.id)  # fmt: skip


def test_reading_another_workspaces_project_under_this_workspace_is_not_found(session, scope):
    chat = open_chat(session, scope)
    store(session, scope, chat.id, "question", "answer")
    other = new_scope(session)

    with pytest.raises(ProjectNotFound):
        listed(session, RunScope(workspace_id=other.workspace_id,
                                 project_id=scope.project_id), chat.id, limit=10)  # fmt: skip
    with pytest.raises(ProjectNotFound):
        stored_thread(session, RunScope(workspace_id=other.workspace_id,
                                        project_id=scope.project_id), chat.id)  # fmt: skip


def test_appending_to_an_unknown_project_writes_nothing(session, scope):
    with pytest.raises(ProjectNotFound):
        store(session, RunScope(workspace_id=scope.workspace_id, project_id=uuid.uuid4()),
              uuid.uuid4(), "question", "answer")  # fmt: skip
    count = session.scalar(select(func.count()).select_from(ChatMessageRecord))
    assert count == 0


def test_a_deleted_chat_is_gone_with_its_messages(session, scope):
    chat = open_chat(session, scope)
    store(session, scope, chat.id, "Is it ready?", "Yes.")

    delete_chat(
        session, workspace_id=scope.workspace_id, project_id=scope.project_id, chat_id=chat.id
    )

    assert session.scalar(select(func.count()).select_from(ChatMessageRecord)) == 0
    with pytest.raises(ChatNotFound):
        stored_thread(session, scope, chat.id)


def test_deleting_one_chat_keeps_the_others(session, scope):
    deleted = open_chat(session, scope, title="Gone")
    kept = open_chat(session, scope, title="Kept")
    store(session, scope, kept.id, "question", "answer")

    delete_chat(
        session, workspace_id=scope.workspace_id, project_id=scope.project_id, chat_id=deleted.id
    )

    page = list_chats(
        session, workspace_id=scope.workspace_id, project_id=scope.project_id, limit=10
    )
    assert [chat.title for chat in page.items] == ["Kept"]
    assert listed(session, scope, kept.id, limit=10) == ["answer", "question"]


def test_deleting_an_unknown_chat_is_not_found(session, scope):
    with pytest.raises(ChatNotFound):
        delete_chat(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            chat_id=uuid.uuid4(),
        )


def test_deleting_a_chat_of_an_unknown_project_is_not_found(session, scope):
    with pytest.raises(ProjectNotFound):
        delete_chat(
            session,
            workspace_id=scope.workspace_id,
            project_id=uuid.uuid4(),
            chat_id=uuid.uuid4(),
        )


def test_deleting_a_chat_of_another_project_is_not_found_and_keeps_it(session, scope):
    other = new_scope(session)
    foreign = open_chat(session, other)
    store(session, other, foreign.id, "question", "answer")

    with pytest.raises(ChatNotFound):
        delete_chat(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            chat_id=foreign.id,
        )

    assert listed(session, other, foreign.id, limit=10) == ["answer", "question"]


def test_deleting_a_chat_of_another_workspace_is_not_found(session, scope):
    chat = open_chat(session, scope)
    other = new_scope(session)

    with pytest.raises(ProjectNotFound):
        delete_chat(
            session,
            workspace_id=other.workspace_id,
            project_id=scope.project_id,
            chat_id=chat.id,
        )

    assert stored_thread(session, scope, chat.id).id == chat.id
