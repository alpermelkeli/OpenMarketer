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


def test_approved_profile_needs_an_approval_time(session, project):
    session.add(
        ProductProfileRecord(
            project_id=project.id,
            version=1,
            content=profile_content(),
            status=ProfileStatus.APPROVED,
        )
    )
    with pytest.raises(IntegrityError, match="ck_product_profile_approved_at"):
        session.flush()


def test_approved_profile_with_time_is_accepted(session, project):
    session.add(
        ProductProfileRecord(
            project_id=project.id,
            version=1,
            content=profile_content(),
            status=ProfileStatus.APPROVED,
            approved_at=datetime.now(UTC),
        )
    )
    session.flush()


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
