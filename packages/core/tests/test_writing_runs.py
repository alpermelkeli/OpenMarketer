"""Tests for writing runs: requesting one, how it moves, and reading it back.

They need PostgreSQL (see the root ``conftest.py``). The content item a run stores is covered
by ``test_content_items.py``; here it is only what a run ends with.
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import TypedDict

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from openmarketer_core.db.chat import ChatNotFound, create_chat
from openmarketer_core.db.content_items import (
    NewContentItem,
    content_item,
    settle_content_item,
    store_proposed_content_item,
)
from openmarketer_core.db.models import (
    ContentItemStatus,
    ProductProfileRecord,
    Project,
    Workspace,
    WritingRunRecord,
    WritingRunStatus,
)
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.db.writing_runs import (
    ProfileVersionNotApproved,
    WritingRunFinished,
    WritingRunNotFound,
    WritingRunNotStarted,
    WritingRunUnsettled,
    WritingRunVersionChosen,
    list_writing_runs,
    mark_run_failed,
    mark_run_started,
    mark_run_succeeded,
    request_writing_run,
    requested_brief,
    run_thread_id,
    run_to_execute,
    writing_run,
)
from openmarketer_core.writing.brief import StoredWritingBriefInvalid, WritingBrief

BRIEF = WritingBrief.model_validate({"platform": "x", "goal": "announce", "feature": "sync"})
REVIEWER = uuid.UUID("11111111-1111-4111-8111-111111111111")
ITEM = NewContentItem(
    text="Sync is here.", plan={}, check_report={}, critic_report={}, cost_usd=Decimal(0)
)


@dataclass(frozen=True)
class ProjectInWorkspace:
    workspace_id: uuid.UUID
    project_id: uuid.UUID


class RunIds(TypedDict):
    workspace_id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID


def new_project(session: Session) -> ProjectInWorkspace:
    workspace = Workspace(name="Acme")
    session.add(workspace)
    session.flush()
    project = Project(
        workspace_id=workspace.id, name="App", source_repo_url="https://example.com/a.git"
    )
    session.add(project)
    session.flush()
    return ProjectInWorkspace(workspace_id=workspace.id, project_id=project.id)


def profile_version(session: Session, project_id: uuid.UUID, *, approved: bool) -> uuid.UUID:
    """A version of the project's profile; approved by UPDATE, as the trigger allows."""
    number = 1 + len(
        session.scalars(
            select(ProductProfileRecord.id).where(ProductProfileRecord.project_id == project_id)
        ).all()
    )
    record = ProductProfileRecord(
        project_id=project_id, version=number, content={"product": {"name": "App"}}
    )
    session.add(record)
    session.flush()
    if approved:
        session.execute(
            text(
                "UPDATE product_profile SET status = 'approved', approved_by = :by,"
                " approved_at = now() WHERE id = :id"
            ),
            {"by": REVIEWER, "id": record.id},
        )
    return record.id


@pytest.fixture
def project(session) -> ProjectInWorkspace:
    return new_project(session)


def scope(project: ProjectInWorkspace, run_id: uuid.UUID) -> RunIds:
    return RunIds(workspace_id=project.workspace_id, project_id=project.project_id, run_id=run_id)


def requested(session: Session, project: ProjectInWorkspace, **fields):
    return request_writing_run(
        session,
        workspace_id=project.workspace_id,
        project_id=project.project_id,
        brief=BRIEF,
        requested_by="person",
        **fields,
    )


def started(session: Session, project: ProjectInWorkspace):
    """A running run that writes from an approved version of the project's profile."""
    run = requested(session, project)
    version_id = profile_version(session, project.project_id, approved=True)
    return mark_run_started(session, **scope(project, run.id), profile_version_id=version_id)


def proposed(session: Session, project: ProjectInWorkspace):
    run = started(session, project)
    item = store_proposed_content_item(session, **scope(project, run.id), item=ITEM)
    return run, item


def test_a_requested_run_is_queued_with_its_brief(session, project):
    run = requested(session, project)

    assert run.status is WritingRunStatus.QUEUED
    assert run.requested_by == "person"
    assert run.profile_version_id is None and run.content_item_id is None
    assert requested_brief(session, **scope(project, run.id)) == BRIEF


def test_a_run_of_a_project_the_workspace_does_not_have_is_refused(session, project):
    with pytest.raises(ProjectNotFound):
        request_writing_run(
            session,
            workspace_id=new_project(session).workspace_id,
            project_id=project.project_id,
            brief=BRIEF,
            requested_by="person",
        )


def test_a_run_a_chat_asked_for_names_the_chat(session, project):
    chat = create_chat(
        session, workspace_id=project.workspace_id, project_id=project.project_id, title="Launch"
    )

    assert requested(session, project, chat_thread_id=chat.id).chat_thread_id == chat.id


def test_a_run_cannot_name_a_chat_of_another_project(session, project):
    other = new_project(session)
    chat = create_chat(
        session, workspace_id=other.workspace_id, project_id=other.project_id, title="Launch"
    )

    with pytest.raises(ChatNotFound):
        requested(session, project, chat_thread_id=chat.id)


def test_starting_a_run_twice_changes_nothing(session, project):
    run = requested(session, project)
    first = mark_run_started(session, **scope(project, run.id))

    assert mark_run_started(session, **scope(project, run.id)) == first
    assert first.status is WritingRunStatus.RUNNING
    assert first.started_at is not None


def test_the_start_records_the_profile_version_once(session, project):
    run = started(session, project)
    assert run.profile_version_id is not None

    again = mark_run_started(
        session, **scope(project, run.id), profile_version_id=run.profile_version_id
    )
    assert again == run
    with pytest.raises(WritingRunVersionChosen):
        mark_run_started(
            session,
            **scope(project, run.id),
            profile_version_id=profile_version(session, project.project_id, approved=True),
        )


def test_a_version_can_be_recorded_after_the_start(session, project):
    run = requested(session, project)
    mark_run_started(session, **scope(project, run.id))
    version_id = profile_version(session, project.project_id, approved=True)

    recorded = mark_run_started(session, **scope(project, run.id), profile_version_id=version_id)

    assert recorded.profile_version_id == version_id


def test_a_run_is_read_and_listed_with_the_number_of_its_version(session, project):
    waiting = requested(session, project)
    run = started(session, project)
    ids = {"workspace_id": project.workspace_id, "project_id": project.project_id}

    assert (waiting.profile_version, run.profile_version) == (None, 1)
    assert writing_run(session, **scope(project, run.id)).profile_version == 1
    listed = {r.id: r.profile_version for r in list_writing_runs(session, **ids, limit=5).items}
    assert listed == {waiting.id: None, run.id: 1}


def test_a_draft_version_is_not_written_from(session, project):
    run = requested(session, project)
    draft = profile_version(session, project.project_id, approved=False)

    with pytest.raises(ProfileVersionNotApproved):
        mark_run_started(session, **scope(project, run.id), profile_version_id=draft)


def test_a_version_of_another_project_is_not_written_from(session, project):
    run = requested(session, project)
    elsewhere = profile_version(session, new_project(session).project_id, approved=True)

    with pytest.raises(ProfileVersionNotApproved):
        mark_run_started(session, **scope(project, run.id), profile_version_id=elsewhere)


def test_a_run_succeeds_once_its_item_is_settled(session, project):
    run, item = proposed(session, project)
    settle_content_item(
        session,
        workspace_id=project.workspace_id,
        project_id=project.project_id,
        item_id=item.id,
        status=ContentItemStatus.REJECTED,
    )

    finished = mark_run_succeeded(session, **scope(project, run.id), cost_usd=Decimal("0.12"))

    assert finished.status is WritingRunStatus.SUCCEEDED
    assert finished.content_item_id == item.id
    assert finished.cost_usd == Decimal("0.12")
    assert finished.finished_at is not None
    again = mark_run_succeeded(session, **scope(project, run.id), cost_usd=Decimal("0.12"))
    assert again == finished


def test_a_run_whose_item_waits_does_not_succeed(session, project):
    run, _ = proposed(session, project)

    with pytest.raises(WritingRunUnsettled):
        mark_run_succeeded(session, **scope(project, run.id), cost_usd=Decimal(0))


def test_a_run_without_an_item_does_not_succeed(session, project):
    run = started(session, project)

    with pytest.raises(WritingRunUnsettled):
        mark_run_succeeded(session, **scope(project, run.id), cost_usd=Decimal(0))


def test_a_queued_run_cannot_succeed(session, project):
    run = requested(session, project)

    with pytest.raises(WritingRunNotStarted):
        mark_run_succeeded(session, **scope(project, run.id), cost_usd=Decimal(0))


def test_a_failure_keeps_its_first_message_and_its_cost(session, project):
    run = started(session, project)
    failed = mark_run_failed(
        session, **scope(project, run.id), error="no live feature", cost_usd=Decimal("0.03")
    )

    again = mark_run_failed(session, **scope(project, run.id), error="another")

    assert again == failed
    assert failed.error == "no live feature"
    assert failed.cost_usd == Decimal("0.03")


def test_a_failure_settles_the_item_that_waited_as_failed(session, project):
    run, item = proposed(session, project)

    mark_run_failed(session, **scope(project, run.id), error="the worker gave up")

    settled = content_item(
        session, workspace_id=project.workspace_id, project_id=project.project_id, item_id=item.id
    )
    assert settled.status is ContentItemStatus.FAILED
    assert settled.settled_at is not None


def test_a_queued_run_can_fail(session, project):
    run = requested(session, project)

    failed = mark_run_failed(session, **scope(project, run.id), error="not handed over")

    assert failed.status is WritingRunStatus.FAILED
    assert failed.started_at is None


def test_a_finished_run_is_not_started_or_given_another_outcome(session, project):
    run = started(session, project)
    mark_run_failed(session, **scope(project, run.id), error="no")

    with pytest.raises(WritingRunFinished):
        mark_run_started(session, **scope(project, run.id))
    with pytest.raises(WritingRunFinished):
        mark_run_succeeded(session, **scope(project, run.id), cost_usd=Decimal(0))


def test_a_succeeded_run_does_not_fail(session, project):
    run, item = proposed(session, project)
    settle_content_item(
        session,
        workspace_id=project.workspace_id,
        project_id=project.project_id,
        item_id=item.id,
        status=ContentItemStatus.REJECTED,
    )
    mark_run_succeeded(session, **scope(project, run.id), cost_usd=Decimal(0))

    with pytest.raises(WritingRunFinished):
        mark_run_failed(session, **scope(project, run.id), error="late")


def test_a_run_of_another_workspace_is_missing(session, project):
    run = requested(session, project)
    other = new_project(session)
    elsewhere = RunIds(
        workspace_id=other.workspace_id, project_id=project.project_id, run_id=run.id
    )

    with pytest.raises(WritingRunNotFound):
        writing_run(session, **elsewhere)
    with pytest.raises(WritingRunNotFound):
        mark_run_started(session, **elsewhere)


def test_the_worker_finds_a_runs_scope_and_brief_from_its_id(session, project):
    run = requested(session, project)

    found = run_to_execute(session, run.id)

    assert found is not None
    assert (found.workspace_id, found.project_id, found.run_id) == (
        project.workspace_id,
        project.project_id,
        run.id,
    )
    assert found.brief == BRIEF
    assert found.requested_by == "person"
    assert run_to_execute(session, uuid.uuid4()) is None


def test_a_stored_brief_that_no_longer_fits_is_refused(session, project):
    run = requested(session, project)
    session.execute(
        update(WritingRunRecord)
        .where(WritingRunRecord.id == run.id)
        .values(brief={"platform": "x", "goal": "announce"})
    )

    with pytest.raises(StoredWritingBriefInvalid):
        run_to_execute(session, run.id)
    with pytest.raises(StoredWritingBriefInvalid):
        requested_brief(session, **scope(project, run.id))


def test_runs_are_listed_newest_first_one_page_at_a_time(session, project):
    runs = [requested(session, project) for _ in range(3)]
    newest_first = sorted(runs, key=lambda r: (r.created_at, r.id), reverse=True)
    ids = {"workspace_id": project.workspace_id, "project_id": project.project_id}

    first = list_writing_runs(session, **ids, limit=2)
    rest = list_writing_runs(session, **ids, limit=2, before=first.next_before)

    assert [r.id for r in first.items + rest.items] == [r.id for r in newest_first]
    assert rest.next_before is None


def test_the_runs_of_a_project_the_workspace_does_not_have_are_not_listed(session, project):
    with pytest.raises(ProjectNotFound):
        list_writing_runs(
            session,
            workspace_id=new_project(session).workspace_id,
            project_id=project.project_id,
            limit=10,
        )


def test_the_thread_of_a_run_is_named_for_its_kind():
    run_id = uuid.uuid4()

    assert run_thread_id(run_id) == f"writing-run:{run_id}"


def test_the_database_refuses_a_success_without_an_item(session, project):
    run = started(session, project)

    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(
            update(WritingRunRecord)
            .where(WritingRunRecord.id == run.id)
            .values(status=WritingRunStatus.SUCCEEDED, finished_at=run.started_at)
        )


def test_the_database_refuses_an_item_of_another_run(session, project):
    _, item = proposed(session, project)
    other = started(session, project)

    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(
            update(WritingRunRecord)
            .where(WritingRunRecord.id == other.id)
            .values(content_item_id=item.id)
        )


def test_the_database_refuses_a_platform_it_does_not_know(session, project):
    run = requested(session, project)

    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(
            update(WritingRunRecord).where(WritingRunRecord.id == run.id).values(platform="tiktok")
        )
