"""Tests for the migration that adds stored credentials.

They need PostgreSQL (see the root ``conftest.py``) and run the migrations
themselves, so they do not use the ``engine`` fixture.
"""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect

BEFORE = "70a50205df52"
ADDS_CREDENTIALS = "e96ec2276ea4"


@pytest.fixture(scope="module")
def database(alembic_config):
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    yield engine
    engine.dispose()


def tables(engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def test_upgrade_adds_the_credential_table_keyed_by_workspace_and_name(alembic_config, database):
    command.upgrade(alembic_config, ADDS_CREDENTIALS)
    columns = {column["name"] for column in inspect(database).get_columns("credential")}
    assert columns == {"workspace_id", "name", "nonce", "ciphertext", "set_by", "set_at"}
    key = inspect(database).get_pk_constraint("credential")
    assert key["constrained_columns"] == ["workspace_id", "name"]


def test_downgrade_removes_the_credential_table_and_nothing_else(alembic_config, database):
    before = tables(database)
    command.downgrade(alembic_config, BEFORE)
    assert before - tables(database) == {"credential"}


def test_upgrade_can_be_repeated_after_a_downgrade(alembic_config, database):
    command.upgrade(alembic_config, ADDS_CREDENTIALS)
    assert "credential" in tables(database)
