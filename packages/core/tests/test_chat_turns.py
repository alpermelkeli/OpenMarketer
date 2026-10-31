"""Tests for chat turns: one running turn per chat, how a turn ends, and a follow-up's key.

They need PostgreSQL (see the root ``conftest.py``).
"""

import threading
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from openmarketer_core.agent_runtime.gate import RunScope
from openmarketer_core.db import chat_turns
from openmarketer_core.db.chat import ChatNotFound, ChatTurnNotFound, create_chat
from openmarketer_core.db.chat_turns import (
    ABANDONED_TURN_ERROR,
    MAX_FOLLOW_UP_TURNS,
    ChatBusy,
    ChatTurn,
    ChatTurnFinished,
    FollowUpAnswered,
    FollowUpGivenUp,
    fail_turn,
    finish_turn,
    record_unstarted_follow_up,
    running_turn,
    start_turn,
)
from openmarketer_core.db.models import (
    CHAT_TURN_ERROR_MAX_LENGTH,
    ChatTurnRecord,
    ChatTurnStatus,
    ChatTurnTrigger,
    Project,
    Workspace,
)
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.db.research_runs import ResearchRunNotFound, request_research_run
from openmarketer_core.db.run_events import StoredRunEvent, events_of_chat
from openmarketer_core.db.studio_runs import StudioRunNotFound, request_studio_run
from openmarketer_core.db.writing_runs import WritingRunNotFound, request_writing_run
from openmarketer_core.research.questions import ResearchRequest
from openmarketer_core.run_events import TURN_FAILED, TURN_STARTED
from openmarketer_core.studio.platforms import Platform
from openmarketer_core.studio.requests import AssetRequest
from openmarketer_core.writing.brief import WritingBrief

REQUEST = ResearchRequest(questions=["What did competitors post last month?"])
ASSET_REQUEST = AssetRequest(goal="Announce the launch", platform=Platform.X_POST)
BRIEF = WritingBrief.model_validate({"platform": "x", "goal": "introduce"})


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
def chat_id(session, scope) -> uuid.UUID:
    return create_chat(session, workspace_id=scope.workspace_id, project_id=scope.project_id).id


def ids(scope: RunScope, chat_id: uuid.UUID) -> dict[str, uuid.UUID]:
    return {"workspace_id": scope.workspace_id, "project_id": scope.project_id, "chat_id": chat_id}


def start(session: Session, scope: RunScope, chat_id: uuid.UUID) -> ChatTurn:
    return start_turn(session, **ids(scope, chat_id), trigger=ChatTurnTrigger.MESSAGE)


def research_of(session: Session, scope: RunScope, chat_id: uuid.UUID | None) -> uuid.UUID:
    return request_research_run(
        session,
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        request=REQUEST,
        requested_by="orchestrator",
        chat_thread_id=chat_id,
    ).id


def follow_up(session: Session, scope: RunScope, chat_id: uuid.UUID, run_id: uuid.UUID):
    return start_turn(
        session,
        **ids(scope, chat_id),
        trigger=ChatTurnTrigger.RESEARCH_FINISHED,
        research_run_id=run_id,
    )


def studio_run_of(session: Session, scope: RunScope, chat_id: uuid.UUID | None) -> uuid.UUID:
    return request_studio_run(
        session,
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        request=ASSET_REQUEST,
        requested_by="orchestrator",
        chat_thread_id=chat_id,
    ).id


def studio_follow_up(session: Session, scope: RunScope, chat_id: uuid.UUID, run_id: uuid.UUID):
    return start_turn(
        session,
        **ids(scope, chat_id),
        trigger=ChatTurnTrigger.STUDIO_FINISHED,
        studio_run_id=run_id,
    )


def writing_run_of(session: Session, scope: RunScope, chat_id: uuid.UUID | None) -> uuid.UUID:
    return request_writing_run(
        session,
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        brief=BRIEF,
        requested_by="orchestrator",
        chat_thread_id=chat_id,
    ).id


def writing_follow_up(session: Session, scope: RunScope, chat_id: uuid.UUID, run_id: uuid.UUID):
    return start_turn(
        session,
        **ids(scope, chat_id),
        trigger=ChatTurnTrigger.WRITING_FINISHED,
        writing_run_id=run_id,
    )


def chat_events(session: Session, scope: RunScope, chat_id: uuid.UUID) -> list[StoredRunEvent]:
    return events_of_chat(session, **ids(scope, chat_id), after=None, limit=100)


def started_ago(session: Session, turn_id: uuid.UUID, minutes: int) -> None:
    session.execute(
        update(ChatTurnRecord)
        .where(ChatTurnRecord.id == turn_id)
        .values(created_at=func.now() - timedelta(minutes=minutes))
    )


def test_a_turn_starts_running(session, scope, chat_id):
    turn = start(session, scope, chat_id)

    assert turn.status is ChatTurnStatus.RUNNING
    assert turn.thread_id == chat_id
    assert turn.finished_at is None
    assert running_turn(session, **ids(scope, chat_id)) == turn


def test_a_chat_takes_one_turn_at_a_time(session, scope, chat_id):
    start(session, scope, chat_id)

    with pytest.raises(ChatBusy):
        start(session, scope, chat_id)


def test_another_chat_is_not_busy_with_this_ones_turn(session, scope, chat_id):
    start(session, scope, chat_id)
    other = create_chat(session, workspace_id=scope.workspace_id, project_id=scope.project_id)

    assert start(session, scope, other.id).status is ChatTurnStatus.RUNNING


def test_an_answered_turn_frees_the_chat(session, scope, chat_id):
    turn = start(session, scope, chat_id)

    answered = finish_turn(session, **ids(scope, chat_id), turn_id=turn.id)

    assert answered.status is ChatTurnStatus.ANSWERED
    assert answered.finished_at is not None
    assert running_turn(session, **ids(scope, chat_id)) is None
    assert start(session, scope, chat_id).status is ChatTurnStatus.RUNNING


def test_a_failed_turn_keeps_its_sentence_and_frees_the_chat(session, scope, chat_id):
    turn = start(session, scope, chat_id)

    failed = fail_turn(session, **ids(scope, chat_id), turn_id=turn.id, error="It ran out.")

    assert failed.status is ChatTurnStatus.FAILED
    assert failed.error == "It ran out."
    assert running_turn(session, **ids(scope, chat_id)) is None


def test_a_long_failure_is_shortened(session, scope, chat_id):
    turn = start(session, scope, chat_id)

    failed = fail_turn(session, **ids(scope, chat_id), turn_id=turn.id, error="x" * 5000)

    assert failed.error == "x" * CHAT_TURN_ERROR_MAX_LENGTH


def test_finishing_again_changes_nothing(session, scope, chat_id):
    turn = start(session, scope, chat_id)
    first = finish_turn(session, **ids(scope, chat_id), turn_id=turn.id)

    assert finish_turn(session, **ids(scope, chat_id), turn_id=turn.id) == first


def test_failing_again_keeps_the_first_sentence(session, scope, chat_id):
    turn = start(session, scope, chat_id)
    fail_turn(session, **ids(scope, chat_id), turn_id=turn.id, error="first")

    again = fail_turn(session, **ids(scope, chat_id), turn_id=turn.id, error="second")

    assert again.error == "first"


def test_a_failed_turn_cannot_answer(session, scope, chat_id):
    turn = start(session, scope, chat_id)
    fail_turn(session, **ids(scope, chat_id), turn_id=turn.id, error="It ran out.")

    with pytest.raises(ChatTurnFinished):
        finish_turn(session, **ids(scope, chat_id), turn_id=turn.id)


def test_an_answered_turn_cannot_fail(session, scope, chat_id):
    turn = start(session, scope, chat_id)
    finish_turn(session, **ids(scope, chat_id), turn_id=turn.id)

    with pytest.raises(ChatTurnFinished):
        fail_turn(session, **ids(scope, chat_id), turn_id=turn.id, error="late")


def test_a_turn_left_running_too_long_is_failed_by_the_next(session, scope, chat_id):
    abandoned = start(session, scope, chat_id)
    started_ago(session, abandoned.id, minutes=16)

    assert running_turn(session, **ids(scope, chat_id)) is None
    new = start(session, scope, chat_id)

    assert new.status is ChatTurnStatus.RUNNING
    given_up = session.get(ChatTurnRecord, abandoned.id, populate_existing=True)
    assert given_up is not None
    assert given_up.status is ChatTurnStatus.FAILED
    assert given_up.error == ABANDONED_TURN_ERROR


def test_a_turn_running_for_a_while_still_keeps_the_chat_busy(session, scope, chat_id):
    turn = start(session, scope, chat_id)
    started_ago(session, turn.id, minutes=14)

    with pytest.raises(ChatBusy):
        start(session, scope, chat_id)


def test_two_starts_at_once_give_one_turn(engine):
    # Real transactions, committed into this module's own temporary database.
    with Session(engine) as setup:
        scope = new_scope(setup)
        chat_id = create_chat(
            setup, workspace_id=scope.workspace_id, project_id=scope.project_id
        ).id
        setup.commit()
    outcome: list[str] = []

    def start_second() -> None:
        with Session(engine) as second:
            try:
                start(second, scope, chat_id)
                second.commit()
                outcome.append("started")
            except ChatBusy:
                outcome.append("busy")

    with Session(engine) as first:
        start(first, scope, chat_id)
        waiting = threading.Thread(target=start_second)
        waiting.start()
        waiting.join(timeout=0.5)
        assert waiting.is_alive()  # held by the workspace lock until the first commits
        first.commit()
    waiting.join(timeout=10)

    assert outcome == ["busy"]


def test_a_start_that_loses_a_race_to_the_index_is_busy(session, scope, chat_id, monkeypatch):
    start(session, scope, chat_id)
    monkeypatch.setattr(chat_turns, "_running_record", lambda *_: None)

    with pytest.raises(ChatBusy):
        start(session, scope, chat_id)


def test_the_database_holds_one_running_turn_per_chat(session, scope, chat_id):
    start(session, scope, chat_id)
    session.add(
        ChatTurnRecord(
            project_id=scope.project_id, thread_id=chat_id, trigger=ChatTurnTrigger.MESSAGE
        )
    )

    with pytest.raises(IntegrityError, match="uq_chat_turn_thread_id_running"):
        session.flush()


@pytest.mark.parametrize(
    ("columns", "constraint"),
    [
        ({"trigger": ChatTurnTrigger.RESEARCH_FINISHED}, "ck_chat_turn_research_run_id"),
        ({"trigger": ChatTurnTrigger.STUDIO_FINISHED}, "ck_chat_turn_studio_run_id"),
        ({"studio_run_id": uuid.uuid4()}, "ck_chat_turn_studio_run_id"),
        ({"trigger": ChatTurnTrigger.WRITING_FINISHED}, "ck_chat_turn_writing_run_id"),
        ({"writing_run_id": uuid.uuid4()}, "ck_chat_turn_writing_run_id"),
        ({"status": ChatTurnStatus.FAILED, "finished_at": func.now()}, "ck_chat_turn_error"),
        ({"status": ChatTurnStatus.ANSWERED}, "ck_chat_turn_finished_at"),
        ({"error": "why"}, "ck_chat_turn_error"),
    ],
)
def test_an_inconsistent_turn_is_refused(session, scope, chat_id, columns, constraint):
    session.add(
        ChatTurnRecord(
            **{
                "project_id": scope.project_id,
                "thread_id": chat_id,
                "trigger": ChatTurnTrigger.MESSAGE,
                **columns,
            }
        )
    )

    with pytest.raises(IntegrityError, match=constraint):
        session.flush()


def test_a_follow_up_names_the_run_it_is_about(session, scope, chat_id):
    run_id = research_of(session, scope, chat_id)

    turn = follow_up(session, scope, chat_id, run_id)

    assert turn.trigger is ChatTurnTrigger.RESEARCH_FINISHED
    assert turn.research_run_id == run_id


def test_a_follow_up_about_a_run_another_chat_asked_for_is_refused(session, scope, chat_id):
    other = create_chat(session, workspace_id=scope.workspace_id, project_id=scope.project_id)
    run_id = research_of(session, scope, other.id)

    with pytest.raises(ResearchRunNotFound):
        follow_up(session, scope, chat_id, run_id)


def test_a_follow_up_about_a_run_no_chat_asked_for_is_refused(session, scope, chat_id):
    run_id = research_of(session, scope, None)

    with pytest.raises(ResearchRunNotFound):
        follow_up(session, scope, chat_id, run_id)


def test_a_research_run_is_named_only_for_a_follow_up(session, scope, chat_id):
    run_id = research_of(session, scope, chat_id)

    with pytest.raises(ValueError):
        start_turn(
            session, **ids(scope, chat_id), trigger=ChatTurnTrigger.MESSAGE, research_run_id=run_id
        )
    with pytest.raises(ValueError):
        start_turn(session, **ids(scope, chat_id), trigger=ChatTurnTrigger.RESEARCH_FINISHED)


def test_a_follow_up_names_the_studio_run_it_is_about(session, scope, chat_id):
    run_id = studio_run_of(session, scope, chat_id)

    turn = studio_follow_up(session, scope, chat_id, run_id)

    assert turn.trigger is ChatTurnTrigger.STUDIO_FINISHED
    assert (turn.studio_run_id, turn.research_run_id) == (run_id, None)


def test_a_follow_up_about_a_studio_run_another_chat_asked_for_is_refused(session, scope, chat_id):
    other = create_chat(session, workspace_id=scope.workspace_id, project_id=scope.project_id)
    run_id = studio_run_of(session, scope, other.id)

    with pytest.raises(StudioRunNotFound):
        studio_follow_up(session, scope, chat_id, run_id)


def test_a_studio_run_is_named_only_for_its_follow_up(session, scope, chat_id):
    run_id = studio_run_of(session, scope, chat_id)

    with pytest.raises(ValueError):
        start_turn(
            session,
            **ids(scope, chat_id),
            trigger=ChatTurnTrigger.RESEARCH_FINISHED,
            studio_run_id=run_id,
        )
    with pytest.raises(ValueError):
        start_turn(session, **ids(scope, chat_id), trigger=ChatTurnTrigger.STUDIO_FINISHED)


def test_a_studio_run_a_turn_has_answered_about_is_not_followed_up_again(session, scope, chat_id):
    run_id = studio_run_of(session, scope, chat_id)
    turn = studio_follow_up(session, scope, chat_id, run_id)
    finish_turn(session, **ids(scope, chat_id), turn_id=turn.id)

    with pytest.raises(FollowUpAnswered):
        studio_follow_up(session, scope, chat_id, run_id)


def test_a_studio_follow_up_that_never_got_to_a_turn_is_recorded_as_one_that_failed(
    session, scope, chat_id
):
    run_id = studio_run_of(session, scope, chat_id)

    recorded = record_unstarted_follow_up(
        session, **ids(scope, chat_id), studio_run_id=run_id, error="It could not start."
    )

    assert recorded is not None
    assert recorded.studio_run_id == run_id
    assert [(e.kind, e.detail) for e in chat_events(session, scope, chat_id)][0] == (
        TURN_STARTED,
        {"trigger": "studio_finished"},
    )


def test_an_unstarted_follow_up_is_about_exactly_one_run(session, scope, chat_id):
    with pytest.raises(ValueError):
        record_unstarted_follow_up(session, **ids(scope, chat_id), error="Neither.")
    with pytest.raises(ValueError):
        record_unstarted_follow_up(
            session,
            **ids(scope, chat_id),
            studio_run_id=studio_run_of(session, scope, chat_id),
            writing_run_id=writing_run_of(session, scope, chat_id),
            error="Both.",
        )


def test_a_follow_up_names_the_writing_run_it_is_about(session, scope, chat_id):
    run_id = writing_run_of(session, scope, chat_id)

    turn = writing_follow_up(session, scope, chat_id, run_id)

    assert turn.trigger is ChatTurnTrigger.WRITING_FINISHED
    assert (turn.writing_run_id, turn.studio_run_id, turn.research_run_id) == (run_id, None, None)


def test_a_follow_up_about_a_writing_run_another_chat_asked_for_is_refused(session, scope, chat_id):
    other = create_chat(session, workspace_id=scope.workspace_id, project_id=scope.project_id)
    run_id = writing_run_of(session, scope, other.id)

    with pytest.raises(WritingRunNotFound):
        writing_follow_up(session, scope, chat_id, run_id)


def test_a_writing_run_is_named_only_for_its_follow_up(session, scope, chat_id):
    run_id = writing_run_of(session, scope, chat_id)

    with pytest.raises(ValueError):
        start_turn(
            session,
            **ids(scope, chat_id),
            trigger=ChatTurnTrigger.STUDIO_FINISHED,
            writing_run_id=run_id,
        )
    with pytest.raises(ValueError):
        start_turn(session, **ids(scope, chat_id), trigger=ChatTurnTrigger.WRITING_FINISHED)


def test_a_writing_run_a_turn_has_answered_about_is_not_followed_up_again(session, scope, chat_id):
    run_id = writing_run_of(session, scope, chat_id)
    turn = writing_follow_up(session, scope, chat_id, run_id)
    finish_turn(session, **ids(scope, chat_id), turn_id=turn.id)

    with pytest.raises(FollowUpAnswered):
        writing_follow_up(session, scope, chat_id, run_id)


def test_a_writing_follow_up_that_never_got_to_a_turn_is_recorded_as_one_that_failed(
    session, scope, chat_id
):
    run_id = writing_run_of(session, scope, chat_id)

    recorded = record_unstarted_follow_up(
        session, **ids(scope, chat_id), writing_run_id=run_id, error="It could not start."
    )

    assert recorded is not None
    assert recorded.writing_run_id == run_id
    assert [(e.kind, e.detail) for e in chat_events(session, scope, chat_id)][0] == (
        TURN_STARTED,
        {"trigger": "writing_finished"},
    )


def test_a_run_a_turn_has_answered_about_is_not_followed_up_again(session, scope, chat_id):
    run_id = research_of(session, scope, chat_id)
    turn = follow_up(session, scope, chat_id, run_id)
    finish_turn(session, **ids(scope, chat_id), turn_id=turn.id)

    with pytest.raises(FollowUpAnswered):
        follow_up(session, scope, chat_id, run_id)


def test_a_failed_follow_up_may_be_taken_again(session, scope, chat_id):
    run_id = research_of(session, scope, chat_id)
    turn = follow_up(session, scope, chat_id, run_id)
    fail_turn(session, **ids(scope, chat_id), turn_id=turn.id, error="It ran out.")

    assert follow_up(session, scope, chat_id, run_id).status is ChatTurnStatus.RUNNING


def test_a_follow_up_is_given_up_after_its_last_turn(session, scope, chat_id):
    run_id = research_of(session, scope, chat_id)
    for _ in range(MAX_FOLLOW_UP_TURNS):
        turn = follow_up(session, scope, chat_id, run_id)
        fail_turn(session, **ids(scope, chat_id), turn_id=turn.id, error="It ran out.")

    with pytest.raises(FollowUpGivenUp):
        follow_up(session, scope, chat_id, run_id)


def test_the_answer_to_another_runs_follow_up_does_not_count(session, scope, chat_id):
    answered_run = research_of(session, scope, chat_id)
    turn = follow_up(session, scope, chat_id, answered_run)
    finish_turn(session, **ids(scope, chat_id), turn_id=turn.id)
    other_run = research_of(session, scope, chat_id)

    assert follow_up(session, scope, chat_id, other_run).status is ChatTurnStatus.RUNNING


def test_an_abandoned_turn_given_up_records_that_it_failed(session, scope, chat_id):
    turn = start(session, scope, chat_id)
    started_ago(session, turn.id, minutes=16)

    start(session, scope, chat_id)

    [event] = [e for e in chat_events(session, scope, chat_id) if e.run_id == turn.id]
    assert (event.kind, event.detail) == (TURN_FAILED, {"reason": ABANDONED_TURN_ERROR})


def test_a_follow_up_that_never_got_to_a_turn_is_recorded_as_one_that_failed(
    session, scope, chat_id
):
    run_id = research_of(session, scope, chat_id)

    recorded = record_unstarted_follow_up(
        session, **ids(scope, chat_id), research_run_id=run_id, error="It could not start."
    )

    assert recorded is not None
    assert (recorded.status, recorded.error) == (ChatTurnStatus.FAILED, "It could not start.")
    assert [(e.kind, e.detail) for e in chat_events(session, scope, chat_id)] == [
        (TURN_STARTED, {"trigger": "research_finished"}),
        (TURN_FAILED, {"reason": "It could not start."}),
    ]


def test_a_follow_up_that_started_a_turn_is_not_recorded_again_as_never_started(
    session, scope, chat_id
):
    run_id = research_of(session, scope, chat_id)
    turn = follow_up(session, scope, chat_id, run_id)
    fail_turn(session, **ids(scope, chat_id), turn_id=turn.id, error="It ran out.")

    recorded = record_unstarted_follow_up(
        session, **ids(scope, chat_id), research_run_id=run_id, error="It could not start."
    )

    assert recorded is None


def test_a_follow_up_never_started_does_not_keep_the_chat_busy(session, scope, chat_id):
    run_id = research_of(session, scope, chat_id)
    start(session, scope, chat_id)

    recorded = record_unstarted_follow_up(
        session, **ids(scope, chat_id), research_run_id=run_id, error="The chat stayed busy."
    )

    assert recorded is not None


def test_a_turn_of_another_chat_is_not_found(session, scope, chat_id):
    other = create_chat(session, workspace_id=scope.workspace_id, project_id=scope.project_id)
    foreign = start(session, scope, other.id)

    with pytest.raises(ChatTurnNotFound):
        finish_turn(session, **ids(scope, chat_id), turn_id=foreign.id)
    with pytest.raises(ChatTurnNotFound):
        fail_turn(session, **ids(scope, chat_id), turn_id=foreign.id, error="no")


def test_a_chat_of_another_project_is_not_found(session, scope):
    other = new_scope(session)
    foreign = create_chat(session, workspace_id=other.workspace_id, project_id=other.project_id)

    with pytest.raises(ChatNotFound):
        start(session, scope, foreign.id)
    with pytest.raises(ChatNotFound):
        running_turn(session, **ids(scope, foreign.id))


def test_a_project_of_another_workspace_is_not_found(session, scope, chat_id):
    other = new_scope(session)
    elsewhere = RunScope(workspace_id=other.workspace_id, project_id=scope.project_id)

    with pytest.raises(ProjectNotFound):
        start(session, elsewhere, chat_id)
    with pytest.raises(ProjectNotFound):
        running_turn(session, **ids(elsewhere, chat_id))
    with pytest.raises(ProjectNotFound):
        record_unstarted_follow_up(
            session, **ids(elsewhere, chat_id), research_run_id=uuid.uuid4(), error="no"
        )
