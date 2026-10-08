"""Tests for the session factory and the transaction around a unit of work.

They need PostgreSQL (see the root ``conftest.py``), except for the unreachable
and invalid databases.
"""

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.db.models import Workspace
from openmarketer_core.db.session import DatabaseError, session_factory, transaction

# Port 1 is reserved and nothing listens on it.
UNREACHABLE_URL = "postgresql+psycopg://nobody:secret@127.0.0.1:1/nothing?connect_timeout=2"


@pytest.fixture
def sessions(engine) -> sessionmaker[Session]:
    return session_factory(engine.url.render_as_string(hide_password=False))


def workspaces_named(sessions: sessionmaker[Session], name: str) -> int:
    with sessions() as session:
        count = session.scalar(
            select(func.count()).select_from(Workspace).where(Workspace.name == name)
        )
    return count or 0


def test_work_is_committed_when_the_block_ends(sessions):
    with transaction(sessions) as session:
        session.add(Workspace(name="committed"))
    assert workspaces_named(sessions, "committed") == 1


def test_work_is_rolled_back_when_the_block_raises(sessions):
    with pytest.raises(RuntimeError, match="analysis went wrong"):
        with transaction(sessions) as session:
            session.add(Workspace(name="abandoned"))
            session.flush()
            raise RuntimeError("analysis went wrong")
    assert workspaces_named(sessions, "abandoned") == 0


def test_refused_work_is_a_database_error_and_is_rolled_back(sessions):
    with pytest.raises(DatabaseError, match="does not exist"):
        with transaction(sessions) as session:
            session.add(Workspace(name="before-the-error"))
            session.flush()
            session.execute(text("SELECT * FROM no_such_table"))
    assert workspaces_named(sessions, "before-the-error") == 0


def test_database_error_names_the_problem_without_the_statement(sessions):
    with pytest.raises(DatabaseError) as raised:
        with transaction(sessions) as session:
            session.execute(text("SELECT * FROM no_such_table"))
    assert str(raised.value) == 'relation "no_such_table" does not exist'


def test_unreachable_database_is_a_database_error():
    sessions = session_factory(UNREACHABLE_URL)
    with pytest.raises(DatabaseError) as raised:
        with transaction(sessions) as session:
            session.execute(text("SELECT 1"))
    assert "secret" not in str(raised.value)


def test_no_connection_is_opened_before_a_session_is_used():
    session_factory(UNREACHABLE_URL)


@pytest.mark.parametrize(
    "url",
    ["not a url", "nodialect://user@host/db", "postgresql+psycopg://user@host:notaport/db"],
)
def test_invalid_database_url_is_a_database_error(url):
    with pytest.raises(DatabaseError, match="invalid database URL"):
        session_factory(url)
