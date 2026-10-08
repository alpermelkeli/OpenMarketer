"""Tests for the migration that makes profile versions immutable, on a database with rows.

They need PostgreSQL (see the root ``conftest.py``) and run the migrations
themselves, so they do not use the ``engine`` fixture.
"""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

BEFORE = "9a5347e23b5c"
STORED_DRAFT = """
WITH workspace AS (
    INSERT INTO workspace (name) VALUES ('Acme') RETURNING id
), project AS (
    INSERT INTO project (workspace_id, name, source_repo_url)
    SELECT id, 'Example App', 'https://example.com/r.git' FROM workspace RETURNING id
)
INSERT INTO product_profile (project_id, version, content)
SELECT id, 1, '{"product": {"name": "X"}}'::jsonb FROM project
"""


@pytest.fixture(scope="module")
def database_with_a_draft(alembic_config):
    command.upgrade(alembic_config, BEFORE)
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    with engine.begin() as conn:
        conn.execute(text(STORED_DRAFT))
    yield engine
    engine.dispose()


def stored_drafts(engine) -> list[tuple[int, str]]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT version, status FROM product_profile")).all()
    return [(row.version, row.status) for row in rows]


def profile_columns(engine) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns("product_profile")}


def test_upgrade_keeps_stored_drafts_and_guards_them(alembic_config, database_with_a_draft):
    command.upgrade(alembic_config, "head")
    assert stored_drafts(database_with_a_draft) == [(1, "draft")]
    assert "edited_from_id" in profile_columns(database_with_a_draft)
    with (
        database_with_a_draft.begin() as conn,
        pytest.raises(IntegrityError, match="version 1 can only be approved, not changed"),
    ):
        conn.execute(text("UPDATE product_profile SET content = '{}'::jsonb"))


def test_downgrade_keeps_stored_drafts_and_removes_the_guard(alembic_config, database_with_a_draft):
    # Runs after the upgrade test: it takes the same database one revision back.
    command.downgrade(alembic_config, BEFORE)
    assert stored_drafts(database_with_a_draft) == [(1, "draft")]
    assert "edited_from_id" not in profile_columns(database_with_a_draft)
    with database_with_a_draft.begin() as conn:
        conn.execute(text("UPDATE product_profile SET content = '{}'::jsonb"))


def test_upgrade_can_be_repeated_after_a_downgrade(alembic_config, database_with_a_draft):
    command.upgrade(alembic_config, "head")
    assert stored_drafts(database_with_a_draft) == [(1, "draft")]
