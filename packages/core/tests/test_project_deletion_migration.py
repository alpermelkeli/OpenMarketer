"""Tests for the migration that lets a deleted project take its profile versions with it.

They need PostgreSQL (see the root ``conftest.py``) and run the migrations
themselves, so they do not use the ``engine`` fixture.
"""

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

BEFORE = "bbcf991fe9b4"
LETS_A_DELETED_PROJECT_TAKE_ITS_VERSIONS = "3c8d1e5f7a92"
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
DELETE_NAMING_THE_PROJECT = """
SELECT set_config('openmarketer.deleted_project', project_id::text, true) FROM product_profile;
DELETE FROM product_profile;
"""


@pytest.fixture(scope="module")
def database_with_a_draft(alembic_config):
    command.upgrade(alembic_config, BEFORE)
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    with engine.begin() as conn:
        conn.execute(text(STORED_DRAFT))
    yield engine
    engine.dispose()


def delete_naming_the_project(engine) -> int:
    """Delete every version while the transaction names their project; how many are left.

    The transaction is rolled back, so each test finds the draft again.
    """
    with engine.connect() as conn:
        conn.exec_driver_sql(DELETE_NAMING_THE_PROJECT)
        left = conn.execute(text("SELECT count(*) FROM product_profile")).scalar_one()
        conn.rollback()
    return left


def test_before_the_upgrade_no_version_can_be_deleted(database_with_a_draft):
    with pytest.raises(IntegrityError, match="version 1 cannot be deleted"):
        delete_naming_the_project(database_with_a_draft)


def test_upgrade_lets_a_deletion_naming_the_project_through(alembic_config, database_with_a_draft):
    command.upgrade(alembic_config, LETS_A_DELETED_PROJECT_TAKE_ITS_VERSIONS)
    assert delete_naming_the_project(database_with_a_draft) == 0


def test_downgrade_refuses_every_deletion_again(alembic_config, database_with_a_draft):
    # Runs after the upgrade test: it takes the same database one revision back.
    command.downgrade(alembic_config, BEFORE)
    with pytest.raises(IntegrityError, match="version 1 cannot be deleted"):
        delete_naming_the_project(database_with_a_draft)
