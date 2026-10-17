"""Tests for the run event store: appending a run's events, and reading a chat's back.

They need PostgreSQL (see the root ``conftest.py``).
"""

import threading
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from openmarketer_core.agent_runtime.gate import RunScope
from openmarketer_core.db.chat import ChatNotFound, create_chat
from openmarketer_core.db.chat_turns import start_turn
from openmarketer_core.db.models import ChatTurnTrigger, Project, RunEventRecord, Workspace
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.db.research_runs import request_research_run
from openmarketer_core.db.run_events import append_events, events_of_chat, latest_events_of_chat
from openmarketer_core.db.studio_runs import request_studio_run
from openmarketer_core.orchestrator.chat_agent.definitions import CHAT_TURN_KIND
from openmarketer_core.research.questions import ResearchRequest
from openmarketer_core.research.workflow_contract import RESEARCH_RUN_KIND
from openmarketer_core.run_events import (
    SEARCHED,
    TOOL_CALLED,
    TURN_ANSWERED,
    TURN_STARTED,
    RunEvent,
)
from openmarketer_core.studio.platforms import Platform
from openmarketer_core.studio.requests import AssetRequest
from openmarketer_core.studio.workflow_contract import STUDIO_RUN_KIND

REQUEST = ResearchRequest(questions=["What did competitors post last month?"])
ASSET_REQUEST = AssetRequest(goal="Announce the launch", platform=Platform.X_POST)


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


def new_chat(session: Session, scope: RunScope) -> uuid.UUID:
    return create_chat(session, workspace_id=scope.workspace_id, project_id=scope.project_id).id


@pytest.fixture
def chat_id(session, scope) -> uuid.UUID:
    return new_chat(session, scope)


def turn_of(session: Session, scope: RunScope, chat_id: uuid.UUID) -> uuid.UUID:
    return start_turn(
        session,
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        chat_id=chat_id,
        trigger=ChatTurnTrigger.MESSAGE,
    ).id


def research_of(session: Session, scope: RunScope, chat_id: uuid.UUID | None) -> uuid.UUID:
    return request_research_run(
        session,
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        request=REQUEST,
        requested_by="orchestrator",
        chat_thread_id=chat_id,
    ).id


def studio_run_of(session: Session, scope: RunScope, chat_id: uuid.UUID | None) -> uuid.UUID:
    return request_studio_run(
        session,
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        request=ASSET_REQUEST,
        requested_by="orchestrator",
        chat_thread_id=chat_id,
    ).id


def record(session: Session, scope: RunScope, run_kind: str, run_id: uuid.UUID, *kinds: str):
    return append_events(
        session,
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        run_kind=run_kind,
        run_id=run_id,
        events=[RunEvent(kind) for kind in kinds],
    )


def read(session: Session, scope: RunScope, chat_id: uuid.UUID, after=None, limit=100):
    return events_of_chat(
        session,
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        chat_id=chat_id,
        after=after,
        limit=limit,
    )


def kinds(events) -> list[str]:
    return [event.kind for event in events]


def test_appended_events_are_stored_in_order_with_their_detail(session, scope, chat_id):
    turn_id = turn_of(session, scope, chat_id)

    stored = append_events(
        session,
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        run_kind=CHAT_TURN_KIND,
        run_id=turn_id,
        events=[RunEvent(TURN_STARTED), RunEvent(TOOL_CALLED, {"tool": "read_profile"})],
    )

    assert kinds(stored) == [TURN_STARTED, TOOL_CALLED]
    assert stored[0].id < stored[1].id
    assert stored[1].detail == {"tool": "read_profile"}
    assert (stored[1].run_kind, stored[1].run_id) == (CHAT_TURN_KIND, turn_id)


def test_no_events_write_nothing(session, scope, statements):
    statements.clear()

    assert record(session, scope, CHAT_TURN_KIND, uuid.uuid4()) == ()
    assert statements == []


def test_a_kind_of_run_that_could_not_name_a_thread_is_refused(session, scope):
    with pytest.raises(ValueError):
        record(session, scope, "Chat Turn", uuid.uuid4(), TURN_STARTED)


def test_events_are_appended_only_to_a_project_of_the_workspace(session, scope):
    other = new_scope(session)

    with pytest.raises(ProjectNotFound):
        record(
            session,
            RunScope(workspace_id=other.workspace_id, project_id=scope.project_id),
            CHAT_TURN_KIND,
            uuid.uuid4(),
            TURN_STARTED,
        )
    assert session.scalar(select(func.count()).select_from(RunEventRecord)) == 0


def test_a_chat_reads_its_turns_and_its_research_runs_events_oldest_first(session, scope, chat_id):
    turn_id = turn_of(session, scope, chat_id)
    run_id = research_of(session, scope, chat_id)
    record(session, scope, CHAT_TURN_KIND, turn_id, TURN_STARTED)
    record(session, scope, RESEARCH_RUN_KIND, run_id, SEARCHED)
    record(session, scope, CHAT_TURN_KIND, turn_id, TURN_ANSWERED)

    events = read(session, scope, chat_id)

    assert kinds(events) == [TURN_STARTED, SEARCHED, TURN_ANSWERED]
    assert [event.id for event in events] == sorted(event.id for event in events)


def test_a_chat_reads_the_events_of_the_studio_runs_it_asked_for(session, scope, chat_id):
    turn_id = turn_of(session, scope, chat_id)
    run_id = studio_run_of(session, scope, chat_id)
    record(session, scope, CHAT_TURN_KIND, turn_id, TURN_STARTED)
    record(session, scope, STUDIO_RUN_KIND, run_id, TOOL_CALLED)

    assert kinds(read(session, scope, chat_id)) == [TURN_STARTED, TOOL_CALLED]


def test_a_chat_does_not_read_the_events_of_another_chats_studio_run(session, scope, chat_id):
    other_chat = new_chat(session, scope)
    record(session, scope, STUDIO_RUN_KIND, studio_run_of(session, scope, other_chat), TOOL_CALLED)
    record(session, scope, STUDIO_RUN_KIND, studio_run_of(session, scope, None), TOOL_CALLED)
    record(session, scope, RESEARCH_RUN_KIND, studio_run_of(session, scope, chat_id), SEARCHED)

    assert read(session, scope, chat_id) == []


def test_a_chat_does_not_read_the_events_of_another_chat_or_run(session, scope, chat_id):
    other_chat = new_chat(session, scope)
    record(session, scope, CHAT_TURN_KIND, turn_of(session, scope, other_chat), TURN_STARTED)
    record(session, scope, RESEARCH_RUN_KIND, research_of(session, scope, other_chat), SEARCHED)
    record(session, scope, RESEARCH_RUN_KIND, research_of(session, scope, None), SEARCHED)

    assert read(session, scope, chat_id) == []


def test_an_event_under_another_kind_with_a_turns_id_is_not_the_chats(session, scope, chat_id):
    turn_id = turn_of(session, scope, chat_id)
    record(session, scope, RESEARCH_RUN_KIND, turn_id, SEARCHED)

    assert read(session, scope, chat_id) == []


def test_a_reader_continues_after_the_last_event_it_got(session, scope, chat_id):
    turn_id = turn_of(session, scope, chat_id)
    record(session, scope, CHAT_TURN_KIND, turn_id, TURN_STARTED, TOOL_CALLED, TURN_ANSWERED)

    first = read(session, scope, chat_id, limit=2)
    rest = read(session, scope, chat_id, after=first[-1].id, limit=2)

    assert kinds(first) == [TURN_STARTED, TOOL_CALLED]
    assert kinds(rest) == [TURN_ANSWERED]
    assert read(session, scope, chat_id, after=rest[-1].id) == []


@pytest.mark.parametrize("limit", [0, 101])
def test_a_read_asks_for_a_bounded_number_of_events(session, scope, chat_id, limit):
    with pytest.raises(ValueError):
        read(session, scope, chat_id, limit=limit)


def test_the_history_pages_newest_first(session, scope, chat_id):
    turn_id = turn_of(session, scope, chat_id)
    record(session, scope, CHAT_TURN_KIND, turn_id, TURN_STARTED, TOOL_CALLED, TURN_ANSWERED)
    ids = {"workspace_id": scope.workspace_id, "project_id": scope.project_id, "chat_id": chat_id}

    first = latest_events_of_chat(session, **ids, limit=2)
    assert first.next_before is not None
    rest = latest_events_of_chat(session, **ids, limit=2, before=first.next_before)

    assert kinds(first.items) == [TURN_ANSWERED, TOOL_CALLED]
    assert kinds(rest.items) == [TURN_STARTED]
    assert rest.next_before is None


def test_the_events_of_a_chat_of_another_project_are_not_found(session, scope):
    other = new_scope(session)
    foreign = new_chat(session, other)

    with pytest.raises(ChatNotFound):
        read(session, scope, foreign)
    with pytest.raises(ChatNotFound):
        latest_events_of_chat(
            session,
            workspace_id=scope.workspace_id,
            project_id=scope.project_id,
            chat_id=foreign,
            limit=10,
        )


def test_the_events_of_a_project_of_another_workspace_are_not_found(session, scope, chat_id):
    other = new_scope(session)

    with pytest.raises(ProjectNotFound):
        read(session, RunScope(workspace_id=other.workspace_id, project_id=scope.project_id),
             chat_id)  # fmt: skip


def test_events_of_one_workspace_become_visible_in_the_order_of_their_ids(engine):
    # Real transactions, committed into this module's own temporary database: a second
    # writer waits for the first, so it can never commit a smaller id after a larger one.
    with Session(engine) as setup:
        scope = new_scope(setup)
        setup.commit()
    run_id = uuid.uuid4()
    with Session(engine) as first:
        record(first, scope, CHAT_TURN_KIND, run_id, TURN_STARTED)

        def append_second() -> None:
            with Session(engine) as second:
                record(second, scope, CHAT_TURN_KIND, run_id, TURN_ANSWERED)
                second.commit()

        waiting = threading.Thread(target=append_second)
        waiting.start()
        waiting.join(timeout=0.5)
        assert waiting.is_alive()
        first.commit()
    waiting.join(timeout=10)

    with Session(engine) as reader:
        stored = reader.scalars(
            select(RunEventRecord.kind)
            .where(RunEventRecord.run_id == run_id)
            .order_by(RunEventRecord.id)
        )
        assert list(stored) == [TURN_STARTED, TURN_ANSWERED]
