"""Tests for the project store. They need PostgreSQL (see the root ``conftest.py``)."""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from openmarketer_core.db.analysis_runs import (
    mark_run_failed,
    mark_run_started,
    mark_run_succeeded,
    request_analysis_run,
)
from openmarketer_core.db.evidence_store import save_analysis_of_project
from openmarketer_core.db.models import Project, Workspace
from openmarketer_core.db.pages import MAX_PAGE_SIZE, CreatedPosition
from openmarketer_core.db.profile_versions import approve_profile_version, save_edited_profile
from openmarketer_core.db.projects import (
    ProjectAlreadyExists,
    ProjectNotFound,
    ProjectOverview,
    create_project,
    get_project,
    list_projects,
    project_overview,
)
from openmarketer_core.intake import IntakeError, Snapshot
from openmarketer_core.profile import ProductProfile

REPOSITORY = "https://example.com/acme/app.git"
REVIEWER = uuid.UUID("11111111-1111-4111-8111-111111111111")
PROFILE = ProductProfile.model_validate({"product": {"name": "Example App", "type": "dev_tool"}})
NOON = datetime(2026, 10, 1, 12, tzinfo=UTC)


def new_workspace(session: Session) -> uuid.UUID:
    workspace = Workspace(name="Acme")
    session.add(workspace)
    session.flush()
    return workspace.id


@pytest.fixture
def workspace_id(session) -> uuid.UUID:
    return new_workspace(session)


def test_created_project_is_stored_in_the_workspace(session, workspace_id):
    created = create_project(
        session, workspace_id=workspace_id, name="Example App", source_repo_url=REPOSITORY
    )
    row = session.get_one(Project, created.id)
    assert (row.workspace_id, row.name, row.source_repo_url) == (
        workspace_id,
        "Example App",
        REPOSITORY,
    )


def test_created_project_carries_when_it_was_created(session, workspace_id):
    created = create_project(
        session, workspace_id=workspace_id, name="Example App", source_repo_url=REPOSITORY
    )
    assert created.created_at == session.get_one(Project, created.id).created_at


@pytest.mark.parametrize("source", ["/etc", "file:///etc", "http://example.com/repo.git"])
def test_project_needs_an_https_repository(session, workspace_id, source):
    with pytest.raises(IntakeError):
        create_project(session, workspace_id=workspace_id, name="App", source_repo_url=source)


def test_repository_can_have_only_one_project_in_a_workspace(session, workspace_id):
    create_project(session, workspace_id=workspace_id, name="First", source_repo_url=REPOSITORY)
    with pytest.raises(ProjectAlreadyExists):
        create_project(
            session, workspace_id=workspace_id, name="Second", source_repo_url=REPOSITORY
        )


def test_refusal_names_the_project_that_has_the_repository(session, workspace_id):
    first = create_project(
        session, workspace_id=workspace_id, name="First", source_repo_url=REPOSITORY
    )
    with pytest.raises(ProjectAlreadyExists) as refusal:
        create_project(
            session, workspace_id=workspace_id, name="Second", source_repo_url=REPOSITORY
        )
    assert refusal.value.project_id == first.id
    assert str(refusal.value) == f"a project for {REPOSITORY} already exists"


def test_two_workspaces_can_have_a_project_for_the_same_repository(session, workspace_id):
    create_project(session, workspace_id=workspace_id, name="Ours", source_repo_url=REPOSITORY)
    theirs = create_project(
        session, workspace_id=new_workspace(session), name="Theirs", source_repo_url=REPOSITORY
    )
    assert theirs.name == "Theirs"


def test_project_is_read_back_inside_its_workspace(session, workspace_id):
    created = create_project(
        session, workspace_id=workspace_id, name="Example App", source_repo_url=REPOSITORY
    )
    assert get_project(session, workspace_id=workspace_id, project_id=created.id) == created


def test_project_of_another_workspace_is_not_found(session, workspace_id):
    created = create_project(
        session, workspace_id=workspace_id, name="Example App", source_repo_url=REPOSITORY
    )
    with pytest.raises(ProjectNotFound):
        get_project(session, workspace_id=new_workspace(session), project_id=created.id)


def test_unknown_project_is_not_found(session, workspace_id):
    with pytest.raises(ProjectNotFound):
        get_project(session, workspace_id=workspace_id, project_id=uuid.uuid4())


# ------------------------------------------------------------------- list
def project_created(session: Session, workspace_id: uuid.UUID, minutes_ago: int) -> uuid.UUID:
    """A project with a creation time of its own.

    The rows of one test are written in one transaction and would otherwise
    all carry its start time.
    """
    project = Project(
        workspace_id=workspace_id,
        name=f"App of {minutes_ago} minutes ago",
        source_repo_url=f"https://example.com/acme/{uuid.uuid4()}.git",
        created_at=NOON - timedelta(minutes=minutes_ago),
    )
    session.add(project)
    session.flush()
    return project.id


def listed_ids(
    session: Session,
    workspace_id: uuid.UUID,
    limit: int = 10,
    before: CreatedPosition | None = None,
) -> list[uuid.UUID]:
    listed = list_projects(session, workspace_id=workspace_id, limit=limit, before=before)
    return [overview.project.id for overview in listed.items]


def test_projects_are_listed_newest_first(session, workspace_id):
    old = project_created(session, workspace_id, minutes_ago=30)
    new = project_created(session, workspace_id, minutes_ago=10)
    middle = project_created(session, workspace_id, minutes_ago=20)
    assert listed_ids(session, workspace_id) == [new, middle, old]


def test_workspace_without_projects_has_an_empty_list(session, workspace_id):
    listed = list_projects(session, workspace_id=workspace_id, limit=10)
    assert (listed.items, listed.next_before) == ((), None)


def test_projects_of_another_workspace_are_not_listed(session, workspace_id):
    ours = project_created(session, workspace_id, minutes_ago=10)
    project_created(session, new_workspace(session), minutes_ago=5)
    assert listed_ids(session, workspace_id) == [ours]


def test_page_holds_no_more_projects_than_the_limit(session, workspace_id):
    projects = [project_created(session, workspace_id, minutes_ago=n) for n in range(1, 4)]
    assert listed_ids(session, workspace_id, limit=2) == projects[:2]


def test_next_page_starts_after_the_last_project_of_the_page_before(session, workspace_id):
    projects = [project_created(session, workspace_id, minutes_ago=n) for n in range(1, 4)]
    first = list_projects(session, workspace_id=workspace_id, limit=2)
    assert listed_ids(session, workspace_id, limit=2, before=first.next_before) == projects[2:]


def test_page_that_ends_the_list_names_no_next_page(session, workspace_id):
    for n in range(1, 3):
        project_created(session, workspace_id, minutes_ago=n)
    assert list_projects(session, workspace_id=workspace_id, limit=2).next_before is None


def test_project_added_between_two_pages_neither_repeats_nor_hides_one(session, workspace_id):
    projects = [project_created(session, workspace_id, minutes_ago=n) for n in range(1, 5)]
    first = list_projects(session, workspace_id=workspace_id, limit=2)
    project_created(session, workspace_id, minutes_ago=0)
    second = listed_ids(session, workspace_id, limit=2, before=first.next_before)
    assert [overview.project.id for overview in first.items] + second == projects


def test_projects_created_at_the_same_moment_are_each_listed_once(session, workspace_id):
    projects = {project_created(session, workspace_id, minutes_ago=7) for _ in range(5)}
    first = list_projects(session, workspace_id=workspace_id, limit=2)
    second = list_projects(session, workspace_id=workspace_id, limit=2, before=first.next_before)
    third = list_projects(session, workspace_id=workspace_id, limit=2, before=second.next_before)
    walked = [overview.project.id for page in (first, second, third) for overview in page.items]
    assert (len(walked), set(walked), third.next_before) == (5, projects, None)


@pytest.mark.parametrize("limit", [0, -1, MAX_PAGE_SIZE + 1])
def test_page_size_outside_the_bounds_is_refused(session, workspace_id, limit):
    with pytest.raises(ValueError, match="a page holds between 1 and"):
        list_projects(session, workspace_id=workspace_id, limit=limit)


def test_largest_page_size_is_accepted(session, workspace_id):
    assert list_projects(session, workspace_id=workspace_id, limit=MAX_PAGE_SIZE).items == ()


def test_list_is_one_statement_however_many_projects_it_holds(session, workspace_id, statements):
    for n in range(1, 6):
        analysed(session, workspace_id, project_created(session, workspace_id, minutes_ago=n))
    statements.clear()
    assert len(list_projects(session, workspace_id=workspace_id, limit=10).items) == 5
    assert len(statements) == 1


# --------------------------------------------------------------- overview
def analysed(session: Session, workspace_id: uuid.UUID, project_id: uuid.UUID) -> uuid.UUID:
    """Store an analysis of the project as its next draft version; the id of that version."""
    snapshot = Snapshot(
        root=Path("/unused"), source_url="https://unused", commit_sha="a" * 40, ref="main"
    )
    saved = save_analysis_of_project(
        session,
        workspace_id=workspace_id,
        project_id=project_id,
        snapshot=snapshot,
        facts=[],
        profile=PROFILE,
    )
    return saved.profile_id


@pytest.fixture
def scope(session, workspace_id) -> dict[str, uuid.UUID]:
    """A project of the workspace, as the arguments every store function takes."""
    project_id = project_created(session, workspace_id, minutes_ago=10)
    return {"workspace_id": workspace_id, "project_id": project_id}


def review_state(overview: ProjectOverview) -> tuple[int | None, int | None, uuid.UUID | None]:
    return (
        overview.latest_draft_version,
        overview.latest_approved_version,
        overview.unfinished_run_id,
    )


def test_overview_holds_the_project_as_it_is_read_alone(session, scope):
    assert project_overview(session, **scope).project == get_project(session, **scope)


def test_project_that_was_never_analysed_has_no_review_state(session, scope):
    assert review_state(project_overview(session, **scope)) == (None, None, None)


def test_project_with_drafts_only_names_its_highest_draft(session, scope):
    analysed(session, **scope)
    analysed(session, **scope)
    assert review_state(project_overview(session, **scope)) == (2, None, None)


def test_project_with_an_approved_version_and_a_newer_draft_names_both(session, scope):
    analysed(session, **scope)
    approve_profile_version(session, **scope, version=1, approved_by=REVIEWER)
    save_edited_profile(session, **scope, edited_version=1, profile=PROFILE)
    assert review_state(project_overview(session, **scope)) == (2, 1, None)


def test_latest_draft_of_an_overview_can_be_older_than_the_approved_version(session, scope):
    analysed(session, **scope)
    analysed(session, **scope)
    approve_profile_version(session, **scope, version=2, approved_by=REVIEWER)
    assert review_state(project_overview(session, **scope)) == (1, 2, None)


def test_project_with_every_version_approved_names_no_draft(session, scope):
    analysed(session, **scope)
    approve_profile_version(session, **scope, version=1, approved_by=REVIEWER)
    assert review_state(project_overview(session, **scope)) == (None, 1, None)


def test_queued_run_is_the_unfinished_run_of_its_project(session, scope):
    run = request_analysis_run(session, **scope)
    assert project_overview(session, **scope).unfinished_run_id == run.id


def test_running_run_is_the_unfinished_run_of_its_project(session, scope):
    run = request_analysis_run(session, **scope)
    mark_run_started(session, **scope, run_id=run.id)
    assert project_overview(session, **scope).unfinished_run_id == run.id


def test_project_whose_run_failed_has_no_unfinished_run(session, scope):
    run = request_analysis_run(session, **scope)
    mark_run_failed(session, **scope, run_id=run.id, error="clone failed")
    assert project_overview(session, **scope).unfinished_run_id is None


def test_project_whose_run_succeeded_has_its_draft_and_no_unfinished_run(session, scope):
    run = request_analysis_run(session, **scope)
    mark_run_started(session, **scope, run_id=run.id)
    mark_run_succeeded(session, **scope, run_id=run.id, profile_id=analysed(session, **scope))
    assert review_state(project_overview(session, **scope)) == (1, None, None)


def test_overview_counts_only_what_belongs_to_the_project(session, workspace_id, scope):
    other = project_created(session, workspace_id, minutes_ago=5)
    analysed(session, workspace_id, other)
    request_analysis_run(session, workspace_id=workspace_id, project_id=other)
    assert review_state(project_overview(session, **scope)) == (None, None, None)


def test_listed_project_carries_the_overview_it_has_when_read_alone(session, scope):
    analysed(session, **scope)
    approve_profile_version(session, **scope, version=1, approved_by=REVIEWER)
    analysed(session, **scope)
    run = request_analysis_run(session, **scope)
    listed = list_projects(session, workspace_id=scope["workspace_id"], limit=10).items
    assert listed == (project_overview(session, **scope),)
    assert review_state(listed[0]) == (2, 1, run.id)


def test_overview_of_an_unknown_project_is_not_found(session, workspace_id):
    with pytest.raises(ProjectNotFound):
        project_overview(session, workspace_id=workspace_id, project_id=uuid.uuid4())


def test_overview_of_a_project_of_another_workspace_is_not_found(session, scope):
    with pytest.raises(ProjectNotFound):
        project_overview(
            session, workspace_id=new_workspace(session), project_id=scope["project_id"]
        )
