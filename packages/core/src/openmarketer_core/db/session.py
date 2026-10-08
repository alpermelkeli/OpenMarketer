"""Database connection: the session factory and the unit of work around it.

Entry points build one factory from the database URL and open a transaction
per operation. This module does not read the environment and holds no queries;
those live in the store modules, which receive the session.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker


class DatabaseError(Exception):
    """The database could not be reached, or refused the work."""


def session_factory(database_url: str) -> sessionmaker[Session]:
    """Sessions for ``database_url``. No connection is opened until one is used."""
    try:
        engine = create_engine(database_url, pool_pre_ping=True)
    except (SQLAlchemyError, ValueError) as e:
        raise DatabaseError(f"invalid database URL: {e}") from e
    except ImportError as e:
        raise DatabaseError(f"database driver is not installed: {e}") from e
    return sessionmaker(engine, expire_on_commit=False)


@contextmanager
def transaction(sessions: sessionmaker[Session]) -> Iterator[Session]:
    """One unit of work: committed when the block ends, rolled back if it raises."""
    try:
        with sessions.begin() as session:
            yield session
    except SQLAlchemyError as e:
        raise DatabaseError(_first_line(e)) from e


def _first_line(error: SQLAlchemyError) -> str:
    """The driver's first line names the problem; the rest is the SQL statement."""
    lines = str(getattr(error, "orig", None) or error).strip().splitlines()
    return lines[0] if lines else type(error).__name__
