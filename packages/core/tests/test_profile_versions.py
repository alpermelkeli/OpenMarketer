"""Tests for editing, approving and reading profile versions.

They need PostgreSQL (see the root ``conftest.py``).
"""

import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.db.evidence_store import save_analysis
from openmarketer_core.db.models import ProductProfileRecord, ProfileStatus, Workspace
from openmarketer_core.db.pages import MAX_PAGE_SIZE
from openmarketer_core.db.profile_versions import (
    ProfileAlreadyApproved,
    ProfileVersion,
    ProfileVersionNotFound,
    ProfileVersionSummary,
    approve_profile_version,
    latest_approved_profile,
    latest_draft_profile,
    list_profile_versions,
    profile_version,
    save_edited_profile,
)
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.intake import Snapshot
from openmarketer_core.profile import ProductProfile

REVIEWER = uuid.UUID("11111111-1111-4111-8111-111111111111")


@dataclass(frozen=True)
class AnalysedProject:
    """A project whose first analysis run is stored as draft version 1."""

    workspace_id: uuid.UUID
    project_id: uuid.UUID
    snapshot_id: uuid.UUID
    profile_id: uuid.UUID

    @property
    def scope(self) -> dict[str, uuid.UUID]:
        return {"workspace_id": self.workspace_id, "project_id": self.project_id}


def profile_named(name: str = "Example App") -> ProductProfile:
    return ProductProfile.model_validate({"product": {"name": name, "type": "dev_tool"}})


def new_workspace(session: Session) -> uuid.UUID:
    workspace = Workspace(name="Acme")
    session.add(workspace)
    session.flush()
    return workspace.id


def analysed_project(session: Session, workspace_id: uuid.UUID) -> AnalysedProject:
    saved = save_analysis(
        session,
        workspace_id=workspace_id,
        snapshot=Snapshot(
            root=Path("/unused"),
            source_url="https://example.com/acme/app.git",
            commit_sha="a" * 40,
            ref="main",
        ),
        facts=[],
        profile=profile_named(),
    )
    return AnalysedProject(workspace_id, saved.project_id, saved.snapshot_id, saved.profile_id)


@pytest.fixture
def project(session) -> AnalysedProject:
    return analysed_project(session, new_workspace(session))


def edit(session: Session, project: AnalysedProject, version: int, name: str) -> ProfileVersion:
    return save_edited_profile(
        session, **project.scope, edited_version=version, profile=profile_named(name)
    )


def approve(session: Session, project: AnalysedProject, version: int) -> ProfileVersion:
    return approve_profile_version(session, **project.scope, version=version, approved_by=REVIEWER)


def stored_row(session: Session, profile_id: uuid.UUID) -> str:
    return session.execute(
        text("SELECT to_jsonb(pp)::text FROM product_profile pp WHERE id = :id"),
        {"id": profile_id},
    ).scalar_one()


def test_edit_is_stored_as_the_next_version_and_a_draft(session, project):
    edited = edit(session, project, 1, "Renamed")
    assert edited.version == 2
    assert edited.status is ProfileStatus.DRAFT
    assert edited.profile == profile_named("Renamed")
    assert (edited.approved_by, edited.approved_at) == (None, None)
    assert edited.created_at.tzinfo is not None


def test_edit_leaves_the_edited_version_unchanged(session, project):
    before = stored_row(session, project.profile_id)
    edit(session, project, 1, "Renamed")
    assert stored_row(session, project.profile_id) == before


def test_edit_keeps_the_snapshot_and_records_the_version_it_came_from(session, project):
    edited = edit(session, project, 1, "Renamed")
    assert edited.snapshot_id == project.snapshot_id
    assert edited.edited_from_id == project.profile_id


def test_edit_of_an_older_version_still_takes_the_next_number(session, project):
    edit(session, project, 1, "Second")
    third = edit(session, project, 1, "Third")
    assert third.version == 3
    assert third.edited_from_id == project.profile_id


def test_edit_of_an_approved_version_is_a_new_draft(session, project):
    approve(session, project, 1)
    edited = edit(session, project, 1, "Renamed")
    assert (edited.version, edited.status) == (2, ProfileStatus.DRAFT)


def test_edit_of_a_missing_version_fails(session, project):
    with pytest.raises(ProfileVersionNotFound, match="no profile version 7"):
        edit(session, project, 7, "Renamed")


def test_approval_stores_the_approver_and_the_time(session, project):
    approved = approve(session, project, 1)
    assert approved.status is ProfileStatus.APPROVED
    assert approved.approved_by == REVIEWER
    assert approved.approved_at is not None
    assert approved.approved_at.tzinfo is not None
    assert latest_approved_profile(session, **project.scope) == approved


def test_approval_leaves_the_content_unchanged(session, project):
    assert approve(session, project, 1).profile == profile_named()


def test_approving_an_approved_version_fails(session, project):
    approve(session, project, 1)
    with pytest.raises(ProfileAlreadyApproved, match="version 1 .* already approved"):
        approve(session, project, 1)


def test_approval_of_a_missing_version_fails(session, project):
    with pytest.raises(ProfileVersionNotFound, match="no profile version 2"):
        approve(session, project, 2)


def test_project_without_approval_has_no_latest_approved_profile(session, project):
    edit(session, project, 1, "Second")
    assert latest_approved_profile(session, **project.scope) is None


def test_latest_draft_is_the_highest_draft_version(session, project):
    second = edit(session, project, 1, "Second")
    assert latest_draft_profile(session, **project.scope) == second


def test_newer_draft_does_not_replace_the_latest_approved_profile(session, project):
    approved = approve(session, project, 1)
    draft = edit(session, project, 1, "Second")
    assert latest_approved_profile(session, **project.scope) == approved
    assert latest_draft_profile(session, **project.scope) == draft


def test_latest_approved_profile_is_the_highest_approved_version(session, project):
    edit(session, project, 1, "Second")
    second = approve(session, project, 2)
    approve(session, project, 1)
    assert latest_approved_profile(session, **project.scope) == second


def test_latest_draft_may_be_older_than_the_latest_approved_profile(session, project):
    edit(session, project, 1, "Second")
    approve(session, project, 2)
    latest_draft = latest_draft_profile(session, **project.scope)
    assert latest_draft is not None
    assert latest_draft.version == 1


def test_project_with_every_version_approved_has_no_latest_draft(session, project):
    approve(session, project, 1)
    assert latest_draft_profile(session, **project.scope) is None


def test_unknown_project_has_no_profiles(session, project):
    unknown = {"workspace_id": project.workspace_id, "project_id": uuid.uuid4()}
    assert latest_draft_profile(session, **unknown) is None
    assert latest_approved_profile(session, **unknown) is None


def test_another_workspace_cannot_read_a_projects_profiles(session, project):
    approve(session, project, 1)
    edit(session, project, 1, "Second")
    elsewhere = {"workspace_id": new_workspace(session), "project_id": project.project_id}
    assert latest_draft_profile(session, **elsewhere) is None
    assert latest_approved_profile(session, **elsewhere) is None


def test_another_workspace_cannot_edit_a_projects_profile(session, project):
    with pytest.raises(ProfileVersionNotFound):
        save_edited_profile(
            session,
            workspace_id=new_workspace(session),
            project_id=project.project_id,
            edited_version=1,
            profile=profile_named("Renamed"),
        )
    latest_draft = latest_draft_profile(session, **project.scope)
    assert latest_draft is not None
    assert latest_draft.version == 1


def test_another_workspace_cannot_approve_a_projects_profile(session, project):
    with pytest.raises(ProfileVersionNotFound):
        approve_profile_version(
            session,
            workspace_id=new_workspace(session),
            project_id=project.project_id,
            version=1,
            approved_by=REVIEWER,
        )
    assert latest_approved_profile(session, **project.scope) is None


# ------------------------------------------------------------ one version
COMMIT = "a" * 40


def test_version_is_read_by_its_number(session, project):
    edited = edit(session, project, 1, "Renamed")
    assert profile_version(session, **project.scope, version=2) == edited


def test_version_that_does_not_exist_is_not_found(session, project):
    with pytest.raises(ProfileVersionNotFound, match="no profile version 7"):
        profile_version(session, **project.scope, version=7)


def test_another_workspace_cannot_read_a_version_by_its_number(session, project):
    with pytest.raises(ProfileVersionNotFound):
        profile_version(
            session, workspace_id=new_workspace(session), project_id=project.project_id, version=1
        )


def test_version_stored_by_an_analysis_names_its_commit_and_no_edited_version(session, project):
    analysed = profile_version(session, **project.scope, version=1)
    assert (analysed.commit_sha, analysed.edited_from_version) == (COMMIT, None)


def test_edit_names_the_number_of_the_version_it_was_made_from(session, project):
    edit(session, project, 1, "Second")
    third = edit(session, project, 2, "Third")
    assert third.edited_from_version == 2
    assert profile_version(session, **project.scope, version=3).edited_from_version == 2


def test_edit_keeps_the_commit_of_the_version_it_was_made_from(session, project):
    edited = edit(session, project, 1, "Renamed")
    assert edited.commit_sha == COMMIT
    assert profile_version(session, **project.scope, version=2).commit_sha == COMMIT


def test_approved_version_still_names_where_it_came_from(session, project):
    edit(session, project, 1, "Renamed")
    approved = approve(session, project, 2)
    assert (approved.commit_sha, approved.edited_from_version) == (COMMIT, 1)


def test_latest_draft_names_where_it_came_from(session, project):
    edit(session, project, 1, "Renamed")
    draft = latest_draft_profile(session, **project.scope)
    assert draft is not None
    assert (draft.commit_sha, draft.edited_from_version) == (COMMIT, 1)


def version_without_snapshot(session: Session, project: AnalysedProject) -> int:
    record = ProductProfileRecord(
        project_id=project.project_id, version=2, content=profile_named().model_dump(mode="json")
    )
    session.add(record)
    session.flush()
    return record.version


def test_version_without_a_snapshot_names_no_commit(session, project):
    version = version_without_snapshot(session, project)
    assert profile_version(session, **project.scope, version=version).commit_sha is None


# ------------------------------------------------------------------- list
def listed_versions(
    session: Session, project: AnalysedProject, limit: int = 10, before_version: int | None = None
) -> list[int]:
    listed = list_profile_versions(
        session, **project.scope, limit=limit, before_version=before_version
    )
    return [summary.version for summary in listed.items]


def test_versions_are_listed_highest_number_first(session, project):
    edit(session, project, 1, "Second")
    edit(session, project, 1, "Third")
    assert listed_versions(session, project) == [3, 2, 1]


def test_listed_version_is_the_stored_version_without_its_profile(session, project):
    edit(session, project, 1, "Renamed")
    approved = approve(session, project, 2)
    newest = list_profile_versions(session, **project.scope, limit=1).items[0]
    assert newest == ProfileVersionSummary(
        project_id=project.project_id,
        version=2,
        status=ProfileStatus.APPROVED,
        commit_sha=COMMIT,
        edited_from_version=1,
        created_at=approved.created_at,
        approved_by=REVIEWER,
        approved_at=approved.approved_at,
    )


def test_listed_version_of_an_analysis_names_no_edited_version(session, project):
    summary = list_profile_versions(session, **project.scope, limit=10).items[0]
    assert (summary.status, summary.commit_sha, summary.edited_from_version) == (
        ProfileStatus.DRAFT,
        COMMIT,
        None,
    )
    assert (summary.approved_by, summary.approved_at) == (None, None)


def test_listed_version_without_a_snapshot_names_no_commit(session, project):
    version_without_snapshot(session, project)
    assert list_profile_versions(session, **project.scope, limit=1).items[0].commit_sha is None


def test_page_holds_no_more_versions_than_the_limit(session, project):
    for name in ("Second", "Third"):
        edit(session, project, 1, name)
    assert listed_versions(session, project, limit=2) == [3, 2]


def test_next_page_starts_below_the_last_version_of_the_page_before(session, project):
    for name in ("Second", "Third"):
        edit(session, project, 1, name)
    first = list_profile_versions(session, **project.scope, limit=2)
    assert first.next_before == 2
    assert listed_versions(session, project, limit=2, before_version=first.next_before) == [1]


def test_page_that_ends_the_list_of_versions_names_no_next_page(session, project):
    edit(session, project, 1, "Second")
    assert list_profile_versions(session, **project.scope, limit=2).next_before is None


def test_version_stored_between_two_pages_neither_repeats_nor_hides_one(session, project):
    for name in ("Second", "Third", "Fourth"):
        edit(session, project, 1, name)
    first = list_profile_versions(session, **project.scope, limit=2)
    edit(session, project, 4, "Fifth")
    second = listed_versions(session, project, limit=2, before_version=first.next_before)
    assert [summary.version for summary in first.items] + second == [4, 3, 2, 1]


def test_versions_of_another_project_are_not_listed(session, project):
    other = analysed_project(session, new_workspace(session))
    edit(session, project, 1, "Second")
    assert listed_versions(session, other) == [1]


def test_versions_of_an_unknown_project_are_not_found(session, project):
    with pytest.raises(ProjectNotFound):
        list_profile_versions(
            session, workspace_id=project.workspace_id, project_id=uuid.uuid4(), limit=10
        )


def test_versions_of_a_project_of_another_workspace_are_not_found(session, project):
    with pytest.raises(ProjectNotFound):
        list_profile_versions(
            session, workspace_id=new_workspace(session), project_id=project.project_id, limit=10
        )


@pytest.mark.parametrize("limit", [0, MAX_PAGE_SIZE + 1])
def test_page_of_versions_outside_the_bounds_is_refused(session, project, limit):
    with pytest.raises(ValueError, match="a page holds between 1 and"):
        list_profile_versions(session, **project.scope, limit=limit)


def test_list_of_versions_never_reads_a_profile(session, project, statements):
    edit(session, project, 1, "Renamed")
    statements.clear()
    assert listed_versions(session, project) == [2, 1]
    assert not any("content" in statement for statement in statements)


def test_list_of_versions_is_two_statements_however_many_it_holds(session, project, statements):
    for name in ("Second", "Third", "Fourth"):
        edit(session, project, 1, name)
    statements.clear()
    assert len(listed_versions(session, project)) == 4
    assert len(statements) == 2


def test_reading_one_version_is_one_statement(session, project, statements):
    edit(session, project, 1, "Renamed")
    statements.clear()
    profile_version(session, **project.scope, version=2)
    assert len(statements) == 1
    assert "content" in statements[0]


def committed_project(sessions: sessionmaker[Session]) -> AnalysedProject:
    with sessions.begin() as setup:
        return analysed_project(setup, new_workspace(setup))


def test_concurrent_edits_take_different_version_numbers(engine):
    sessions = sessionmaker(engine)
    project = committed_project(sessions)
    later: list[ProfileVersion] = []

    def edit_later() -> None:
        with sessions.begin() as session:
            later.append(edit(session, project, 1, "Later"))

    with sessions.begin() as session:
        earlier = edit(session, project, 1, "Earlier")
        other_edit = threading.Thread(target=edit_later)
        other_edit.start()
        other_edit.join(timeout=0.5)  # long enough to save, were it not made to wait
        assert not later
    other_edit.join(timeout=10)

    assert (earlier.version, later[0].version) == (2, 3)


def test_concurrent_approvals_of_a_version_record_one_approval(engine):
    sessions = sessionmaker(engine)
    project = committed_project(sessions)
    refused: list[ProfileAlreadyApproved] = []

    def approve_later() -> None:
        with pytest.raises(ProfileAlreadyApproved) as refusal, sessions.begin() as session:
            approve(session, project, 1)
        refused.append(refusal.value)

    with sessions.begin() as session:
        approved = approve(session, project, 1)
        other_approval = threading.Thread(target=approve_later)
        other_approval.start()
        other_approval.join(timeout=0.5)  # long enough to approve, were it not made to wait
        assert not refused
    other_approval.join(timeout=10)

    assert len(refused) == 1
    with sessions() as session:
        assert latest_approved_profile(session, **project.scope) == approved


def test_approval_sees_an_approval_made_after_the_session_loaded_the_draft(engine):
    sessions = sessionmaker(engine)
    project = committed_project(sessions)

    with sessions.begin() as session:
        # Holding the row keeps it in the session, which would otherwise forget it.
        held = session.get_one(ProductProfileRecord, project.profile_id)
        assert held.status is ProfileStatus.DRAFT
        with sessions.begin() as other_session:
            approve(other_session, project, 1)
        with pytest.raises(ProfileAlreadyApproved):
            approve(session, project, 1)
