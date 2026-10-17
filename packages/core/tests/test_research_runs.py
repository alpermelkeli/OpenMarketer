"""Tests for requesting research runs, and for the action that asked for one finding it again.

They need PostgreSQL (see the root ``conftest.py``). The chat's reads of the runs they store
are covered by ``test_chat_reads.py``; this is the store's own rule: a run another run asked
for through an action is keyed by that action's id, so an attempt that is tried again finds
the run it made instead of creating a second one.
"""

import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from openmarketer_core.db.chat import ChatNotFound, create_chat
from openmarketer_core.db.models import Project, ResearchRunRecord, Workspace
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.db.research_runs import request_research_run, research_run
from openmarketer_core.research.questions import ResearchRequest

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
