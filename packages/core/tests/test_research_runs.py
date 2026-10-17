"""Tests for requesting research runs, and for the action that asked for one finding it again.

They need PostgreSQL (see the root ``conftest.py``). The chat's reads of the runs they store
are covered by ``test_chat_reads.py``; this is the store's own rule: a run another run asked
for through an action is keyed by that action's id, so an attempt that is tried again finds
the run it made instead of creating a second one. Deleting a finished run is here too, with
what it takes with it.
"""

import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from openmarketer_core.db.chat import ChatNotFound, create_chat
from openmarketer_core.db.chat_turns import start_turn
from openmarketer_core.db.models import (
    ChatTurnTrigger,
    Project,
    ResearchPageRecord,
    ResearchReportRecord,
    ResearchRunRecord,
    Workspace,
)
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.db.research_pages import NewPage, store_page
from openmarketer_core.db.research_runs import (
    ResearchRun,
    ResearchRunNotFound,
    ResearchRunUnfinished,
    delete_research_run,
    mark_run_failed,
    mark_run_started,
    mark_run_succeeded,
    request_research_run,
    research_run,
)
from openmarketer_core.db.research_store import save_research_report
from openmarketer_core.research.questions import ResearchRequest
from openmarketer_core.research.research_agent.rules import Report

REQUEST = ResearchRequest(questions=["What did competitors post last month?"])


@dataclass(frozen=True)
class ProjectInWorkspace:
    workspace_id: uuid.UUID
    project_id: uuid.UUID


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


@pytest.fixture
def project(session) -> ProjectInWorkspace:
    return new_project(session)


def stored_origins(session: Session, project: ProjectInWorkspace) -> list[str | None]:
    return list(
        session.scalars(
            select(ResearchRunRecord.origin_action_id)
            .where(ResearchRunRecord.project_id == project.project_id)
            .order_by(ResearchRunRecord.created_at)
        )
    )


def test_a_run_a_person_asked_for_has_no_action_behind_it(session, project):
    run = request_research_run(
        session,
        workspace_id=project.workspace_id,
        project_id=project.project_id,
        request=REQUEST,
        requested_by="person",
    )

    assert run.requested_by == "person"
    assert stored_origins(session, project) == [None]


def test_a_run_an_action_asked_for_stores_the_actions_id(session, project):
    request_research_run(
        session,
        workspace_id=project.workspace_id,
        project_id=project.project_id,
        request=REQUEST,
        requested_by="run",
        origin_action_id="research-run:1:2",
    )

    assert stored_origins(session, project) == ["research-run:1:2"]


def test_an_action_that_asks_again_finds_the_run_it_made(session, project):
    first = request_research_run(
        session,
        workspace_id=project.workspace_id,
        project_id=project.project_id,
        request=REQUEST,
        requested_by="run",
        origin_action_id="research-run:1:2",
    )
    again = request_research_run(
        session,
        workspace_id=project.workspace_id,
        project_id=project.project_id,
        request=REQUEST,
        requested_by="run",
        origin_action_id="research-run:1:2",
    )

    assert again.id == first.id
    assert stored_origins(session, project) == ["research-run:1:2"]


def test_two_actions_of_run_kinds_make_two_runs(session, project):
    first = request_research_run(
        session,
        workspace_id=project.workspace_id,
        project_id=project.project_id,
        request=REQUEST,
        requested_by="run",
        origin_action_id="research-run:1:2",
    )
    second = request_research_run(
        session,
        workspace_id=project.workspace_id,
        project_id=project.project_id,
        request=REQUEST,
        requested_by="run",
        origin_action_id="research-run:1:3",
    )

    assert first.id != second.id
    assert stored_origins(session, project) == ["research-run:1:2", "research-run:1:3"]


def test_many_runs_asked_for_by_people_live_beside_each_other(session, project):
    # The key is unique where it is set and absent where it is not: PostgreSQL keeps any
    # number of nulls under its unique constraint, so person-asked runs need no key of their own.
    for _ in range(2):
        request_research_run(
            session,
            workspace_id=project.workspace_id,
            project_id=project.project_id,
            request=REQUEST,
            requested_by="person",
        )

    assert stored_origins(session, project) == [None, None]


def test_a_run_of_a_project_the_workspace_does_not_have_is_refused(session, project):
    with pytest.raises(ProjectNotFound):
        request_research_run(
            session,
            workspace_id=uuid.uuid4(),
            project_id=project.project_id,
            request=REQUEST,
            requested_by="person",
        )


def test_a_run_a_chat_asked_for_names_the_chat(session, project):
    chat = create_chat(session, workspace_id=project.workspace_id, project_id=project.project_id)

    run = request_research_run(
        session,
        workspace_id=project.workspace_id,
        project_id=project.project_id,
        request=REQUEST,
        requested_by="orchestrator",
        chat_thread_id=chat.id,
    )

    assert run.chat_thread_id == chat.id
    read_back = research_run(
        session, workspace_id=project.workspace_id, project_id=project.project_id, run_id=run.id
    )
    assert read_back.chat_thread_id == chat.id


def test_a_run_asked_for_elsewhere_names_no_chat(session, project):
    run = request_research_run(
        session,
        workspace_id=project.workspace_id,
        project_id=project.project_id,
        request=REQUEST,
        requested_by="person",
    )

    assert run.chat_thread_id is None


def test_a_run_cannot_name_a_chat_of_another_project(session, project):
    other = new_project(session)
    foreign = create_chat(session, workspace_id=other.workspace_id, project_id=other.project_id)

    with pytest.raises(ChatNotFound):
        request_research_run(
            session,
            workspace_id=project.workspace_id,
            project_id=project.project_id,
            request=REQUEST,
            requested_by="orchestrator",
            chat_thread_id=foreign.id,
        )
    assert stored_origins(session, project) == []


def ids(project: ProjectInWorkspace) -> dict:
    return {"workspace_id": project.workspace_id, "project_id": project.project_id}


def run_with_pages(
    session: Session, project: ProjectInWorkspace, *, storage_keys: tuple[str, ...]
) -> ResearchRun:
    """A run that fetched one page per key and is still unfinished."""
    run = request_research_run(session, **ids(project), request=REQUEST, requested_by="person")
    mark_run_started(session, **ids(project), run_id=run.id)
    for key in storage_keys:
        store_page(
            session,
            **ids(project),
            run_id=run.id,
            page_id=uuid.uuid4(),
            page=NewPage(
                url=f"https://example.com/{key}",
                title="A page",
                content_type="text/html",
                size_bytes=10,
                storage_key=key,
                sha256="a" * 64,
                flags=(),
            ),
        )
    return run


def finished_run_with_pages(
    session: Session, project: ProjectInWorkspace, *, storage_keys: tuple[str, ...]
) -> ResearchRun:
    """A run that succeeded, with a report and one page per key."""
    run = run_with_pages(session, project, storage_keys=storage_keys)
    report = save_research_report(
        session,
        **ids(project),
        run_id=run.id,
        report=Report(
            questions=list(REQUEST.questions),
            findings=[],
            notes=[],
            cost_usd=0.01,
            model="fake/model",
        ),
    )
    return mark_run_succeeded(session, **ids(project), run_id=run.id, report_id=report.id)


def rows_for_run(session: Session, run: ResearchRun) -> tuple[int, int, int]:
    """How many rows the run has left: the run, its pages, and its report."""
    report = 0
    if run.report_id is not None:
        report = (
            session.scalar(
                select(func.count())
                .select_from(ResearchReportRecord)
                .where(ResearchReportRecord.id == run.report_id)
            )
            or 0
        )
    rows = (
        session.scalar(
            select(func.count())
            .select_from(ResearchRunRecord)
            .where(ResearchRunRecord.id == run.id)
        )
        or 0,
        session.scalar(
            select(func.count())
            .select_from(ResearchPageRecord)
            .where(ResearchPageRecord.run_id == run.id)
        )
        or 0,
    )
    return rows + (report,)


def test_deleting_a_finished_run_removes_it_with_its_report_and_pages(session, project):
    run = finished_run_with_pages(session, project, storage_keys=("pages/a.html", "pages/b.html"))
    assert rows_for_run(session, run) == (1, 2, 1)

    keys = delete_research_run(session, **ids(project), run_id=run.id)

    assert sorted(keys) == ["pages/a.html", "pages/b.html"]
    assert rows_for_run(session, run) == (0, 0, 0)
    with pytest.raises(ResearchRunNotFound):
        delete_research_run(session, **ids(project), run_id=run.id)


def test_deleting_one_run_keeps_the_projects_other_runs(session, project):
    deleted = finished_run_with_pages(session, project, storage_keys=("pages/a.html",))
    kept = finished_run_with_pages(session, project, storage_keys=("pages/b.html",))

    delete_research_run(session, **ids(project), run_id=deleted.id)

    assert rows_for_run(session, deleted) == (0, 0, 0)
    assert rows_for_run(session, kept) == (1, 1, 1)


def test_a_failed_run_is_deleted_too(session, project):
    run = run_with_pages(session, project, storage_keys=("pages/a.html",))
    mark_run_failed(session, **ids(project), run_id=run.id, error="boom")

    keys = delete_research_run(session, **ids(project), run_id=run.id)

    assert list(keys) == ["pages/a.html"]
    assert rows_for_run(session, run) == (0, 0, 0)


@pytest.mark.parametrize("started", [False, True])
def test_an_unfinished_run_is_not_deleted_and_keeps_its_pages(session, project, started):
    run = request_research_run(session, **ids(project), request=REQUEST, requested_by="person")
    if started:
        mark_run_started(session, **ids(project), run_id=run.id)

    with pytest.raises(ResearchRunUnfinished):
        delete_research_run(session, **ids(project), run_id=run.id)

    assert rows_for_run(session, run)[0] == 1


def test_deleting_an_unknown_run_is_not_found(session, project):
    with pytest.raises(ResearchRunNotFound):
        delete_research_run(session, **ids(project), run_id=uuid.uuid4())


def test_deleting_a_run_of_another_project_is_not_found_and_keeps_it(session, project):
    run = finished_run_with_pages(session, project, storage_keys=("pages/a.html",))
    other = new_project(session)

    with pytest.raises(ResearchRunNotFound):
        delete_research_run(session, **ids(other), run_id=run.id)

    assert rows_for_run(session, run) == (1, 1, 1)


def test_a_run_a_follow_up_named_is_deleted_on_its_own(session, project):
    chat = create_chat(session, workspace_id=project.workspace_id, project_id=project.project_id)
    run = request_research_run(
        session,
        **ids(project),
        request=REQUEST,
        requested_by="orchestrator",
        chat_thread_id=chat.id,
    )
    mark_run_started(session, **ids(project), run_id=run.id)
    report = save_research_report(
        session,
        **ids(project),
        run_id=run.id,
        report=Report(
            questions=list(REQUEST.questions),
            findings=[],
            notes=[],
            cost_usd=0.01,
            model="fake/model",
        ),
    )
    run = mark_run_succeeded(session, **ids(project), run_id=run.id, report_id=report.id)
    start_turn(
        session,
        workspace_id=project.workspace_id,
        project_id=project.project_id,
        chat_id=chat.id,
        trigger=ChatTurnTrigger.RESEARCH_FINISHED,
        research_run_id=run.id,
    )

    keys = delete_research_run(session, **ids(project), run_id=run.id)

    assert keys == ()
    assert rows_for_run(session, run) == (0, 0, 0)
