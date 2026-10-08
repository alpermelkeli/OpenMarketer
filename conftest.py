"""Database fixtures shared by the tests of every package that need PostgreSQL.

They use the dev stack (``make up``) or DATABASE_URL. Each test module gets its
own temporary database, and the tests are skipped when no server is reachable.
"""

import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

DEV_STACK_URL = "postgresql+psycopg://openmarketer:openmarketer@localhost:5433/openmarketer"
ALEMBIC_INI = Path(__file__).resolve().parent / "alembic.ini"


@pytest.fixture(scope="module")
def alembic_config():
    server_url = make_url(os.environ.get("DATABASE_URL", DEV_STACK_URL))
    admin = create_engine(server_url, isolation_level="AUTOCOMMIT")
    name = f"om_test_{uuid.uuid4().hex[:12]}"
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except OperationalError:
        pytest.skip("PostgreSQL is not reachable (run `make up`)")

    config = Config(str(ALEMBIC_INI))
    url = server_url.set(database=name).render_as_string(hide_password=False)
    config.set_main_option("sqlalchemy.url", url)
    try:
        yield config
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="module")
def engine(alembic_config):
    command.upgrade(alembic_config, "head")
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    """A session whose work is rolled back after each test."""
    with engine.connect() as conn:
        transaction = conn.begin()
        with Session(conn, join_transaction_mode="create_savepoint") as session:
            yield session
        transaction.rollback()
