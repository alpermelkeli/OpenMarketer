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
from openmarketer_core.db.profile_versions import (
    ProfileAlreadyApproved,
    ProfileVersion,
    ProfileVersionNotFound,
    approve_profile_version,
    latest_approved_profile,
    latest_draft_profile,
    save_edited_profile,
)
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
