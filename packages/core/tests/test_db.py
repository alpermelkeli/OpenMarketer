"""Tests for the migrations and table constraints.

They need PostgreSQL; the fixtures are in the ``conftest.py`` at the repository root.
"""

import uuid
from datetime import UTC, datetime

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from openmarketer_core.db.models import (
    Evidence,
    ProductProfileRecord,
    ProfileStatus,
    Project,
    ProjectStatus,
    RepoSnapshot,
    Workspace,
)
from openmarketer_core.profile import ProductProfile

TABLES = {"workspace", "project", "repo_snapshot", "evidence", "product_profile"}


@pytest.fixture
def project(session):
    workspace = Workspace(name="Acme")
    session.add(workspace)
    session.flush()
    project = Project(
        workspace_id=workspace.id, name="Example App", source_repo_url="https://example.com/r.git"
    )
    session.add(project)
    session.flush()
    session.refresh(project)
    return project


def profile_content() -> dict:
    profile = ProductProfile.model_validate({"product": {"name": "X", "type": "dev_tool"}})
    return profile.model_dump(mode="json")


def test_migrations_create_the_tables(engine):
    assert TABLES <= set(inspect(engine).get_table_names())


def test_models_match_migrations(engine, alembic_config):
    command.check(alembic_config)  # raises if autogenerate would produce changes


def test_project_defaults(project):
    assert isinstance(project.id, uuid.UUID)
    assert project.status is ProjectStatus.ONBOARDING
    assert project.autonomy_level == 0
    assert project.created_at.tzinfo is not None


def test_profile_content_round_trips(session, project):
    session.add(ProductProfileRecord(project_id=project.id, version=1, content=profile_content()))
    session.flush()
    session.expire_all()
    record = session.query(ProductProfileRecord).one()
    assert record.status is ProfileStatus.DRAFT
    assert ProductProfile.model_validate(record.content).product.name == "X"


def test_profile_version_is_unique_per_project(session, project):
    for _ in range(2):
        session.add(
            ProductProfileRecord(project_id=project.id, version=1, content=profile_content())
        )
    with pytest.raises(IntegrityError, match="uq_product_profile_project_id_version"):
        session.flush()


def stored_draft(session, project) -> ProductProfileRecord:
    record = ProductProfileRecord(project_id=project.id, version=1, content=profile_content())
    session.add(record)
    session.flush()
    return record


def stored_approved(session, project) -> ProductProfileRecord:
    record = stored_draft(session, project)
    change(session, record, APPROVAL)
    return record


def another_project(session, project) -> Project:
    other = Project(
        workspace_id=project.workspace_id, name="Other", source_repo_url="https://example.com/o.git"
    )
    session.add(other)
    session.flush()
    return other


APPROVAL = "status = 'approved', approved_by = gen_random_uuid(), approved_at = now()"
REWRITES = {
    "content": "content = '{}'::jsonb",
    "renumbered": "version = 50",
    "moved to another project": "project_id = :other_project",
    "another snapshot": "snapshot_id = gen_random_uuid()",
    "another creation time": "created_at = now() - interval '1 day'",
}


def change(session, record, assignments: str, **values) -> None:
    session.execute(
        text(f"UPDATE product_profile SET {assignments} WHERE id = :id"),
        {"id": record.id, **values},
    )


def test_approval_needs_an_approval_time(session, project):
    draft = stored_draft(session, project)
    with pytest.raises(IntegrityError, match="ck_product_profile_approval"):
        change(session, draft, "status = 'approved', approved_by = gen_random_uuid()")


def test_approval_needs_an_approver(session, project):
    draft = stored_draft(session, project)
    with pytest.raises(IntegrityError, match="ck_product_profile_approval"):
        change(session, draft, "status = 'approved', approved_at = now()")


def test_draft_profile_cannot_name_an_approver(session, project):
    session.add(
        ProductProfileRecord(
            project_id=project.id, version=1, content=profile_content(), approved_by=uuid.uuid4()
        )
    )
    with pytest.raises(IntegrityError, match="ck_product_profile_approval"):
        session.flush()


def test_draft_is_approved_by_an_update_with_approver_and_time(session, project):
    record = stored_approved(session, project)
    session.refresh(record)
    assert record.status is ProfileStatus.APPROVED


def test_profile_cannot_be_inserted_already_approved(session, project):
    session.add(
        ProductProfileRecord(
            project_id=project.id,
            version=1,
            content=profile_content(),
            status=ProfileStatus.APPROVED,
            approved_by=uuid.uuid4(),
            approved_at=datetime.now(UTC),
        )
    )
    with pytest.raises(IntegrityError, match="stored as a draft and approved afterwards"):
        session.flush()


@pytest.mark.parametrize("assignments", REWRITES.values(), ids=REWRITES.keys())
def test_draft_profile_cannot_be_rewritten(session, project, assignments):
    draft = stored_draft(session, project)
    other = another_project(session, project)
    with pytest.raises(IntegrityError, match="version 1 can only be approved, not changed"):
        change(session, draft, assignments, other_project=other.id)


@pytest.mark.parametrize("assignments", REWRITES.values(), ids=REWRITES.keys())
def test_approval_cannot_rewrite_the_draft_in_the_same_statement(session, project, assignments):
    draft = stored_draft(session, project)
    other = another_project(session, project)
    with pytest.raises(IntegrityError, match="version 1 can only be approved, not changed"):
        change(session, draft, f"{APPROVAL}, {assignments}", other_project=other.id)


@pytest.mark.parametrize("assignments", REWRITES.values(), ids=REWRITES.keys())
def test_approved_profile_cannot_be_rewritten(session, project, assignments):
    approved = stored_approved(session, project)
    other = another_project(session, project)
    with pytest.raises(IntegrityError, match="approved product profile version 1 cannot be"):
        change(session, approved, assignments, other_project=other.id)


def test_approved_profile_cannot_be_unapproved(session, project):
    approved = stored_approved(session, project)
    with pytest.raises(IntegrityError, match="approved product profile version 1 cannot be"):
        change(session, approved, "status = 'draft', approved_by = NULL, approved_at = NULL")


def test_approved_profile_cannot_get_another_approver(session, project):
    approved = stored_approved(session, project)
    with pytest.raises(IntegrityError, match="approved product profile version 1 cannot be"):
        change(session, approved, "approved_by = gen_random_uuid()")


def test_approved_profile_cannot_be_deleted(session, project):
    approved = stored_approved(session, project)
    with pytest.raises(IntegrityError, match="product profile version 1 cannot be deleted"):
        session.execute(text("DELETE FROM product_profile WHERE id = :id"), {"id": approved.id})


def test_draft_profile_cannot_be_deleted(session, project):
    draft = stored_draft(session, project)
    with pytest.raises(IntegrityError, match="product profile version 1 cannot be deleted"):
        session.execute(text("DELETE FROM product_profile WHERE id = :id"), {"id": draft.id})


def test_autonomy_level_is_limited_to_the_ladder(session, project):
    project.autonomy_level = 4
    with pytest.raises(IntegrityError, match="ck_project_autonomy_level"):
        session.flush()


def test_unknown_project_status_is_rejected(session, project):
    with pytest.raises(IntegrityError, match="ck_project_status"):
        session.execute(
            text("UPDATE project SET status = 'deleted' WHERE id = :id"), {"id": project.id}
        )


@pytest.mark.parametrize(
    ("start", "end", "constraint"),
    [(0, None, "ck_evidence_start_line"), (9, 3, "ck_evidence_end_line")],
)
def test_evidence_line_range_is_checked(session, project, start, end, constraint):
    snapshot = RepoSnapshot(project_id=project.id, commit_sha="a" * 40)
    session.add(snapshot)
    session.flush()
    session.add(
        Evidence(
            project_id=project.id,
            snapshot_id=snapshot.id,
            extractor="manifest",
            kind="product.name",
            value="Example App",
            file="package.json",
            start_line=start,
            end_line=end,
        )
    )
    with pytest.raises(IntegrityError, match=constraint):
        session.flush()


def test_downgrade_removes_everything(alembic_config, engine):
    # Runs last: it leaves the temporary database empty.
    engine.dispose()
    command.downgrade(alembic_config, "base")
    assert not TABLES & set(inspect(engine).get_table_names())
