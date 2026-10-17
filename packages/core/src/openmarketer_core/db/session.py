"""Database connection: the session factory and the unit of work around it.

Entry points build one factory from the database URL and open a transaction
per operation. A use case whose check and write must not interleave with another
transaction's runs them in ``serializable``, which tries the work again when
PostgreSQL could not serialise it. This module does not read the environment and
holds no queries; those live in the store modules, which receive the session.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

# How often a serialisable unit of work is tried before it is given up. A failure means another
# transaction won a race, so a few tries are enough unless something writes without pause.
SERIALIZABLE_ATTEMPTS = 5

# What PostgreSQL answers when it could not serialise a transaction, or broke a deadlock by
# ending it: the work, tried again from the start, can succeed.
_TRY_AGAIN = frozenset({"40001", "40P01"})


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


def serializable[T](
    sessions: sessionmaker[Session],
    work: Callable[[Session], T],
    *,
    attempts: int = SERIALIZABLE_ATTEMPTS,
) -> T:
    """``work`` in one SERIALIZABLE transaction, committed when it returns.

    ``work`` is called again, in a new transaction, when PostgreSQL could not serialise the
    one before, so it must do nothing outside the session. What it raises rolls the
    transaction back and reaches the caller; a database failure is ``DatabaseError``, also
    the last of ``attempts`` failures to serialise.
    """
    for attempt in range(1, attempts + 1):
        try:
            with sessions() as session, session.begin():
                session.connection(execution_options={"isolation_level": "SERIALIZABLE"})
                return work(session)
        except SQLAlchemyError as e:
            if attempt == attempts or not _worth_trying_again(e):
                raise DatabaseError(_first_line(e)) from e
    raise ValueError("a serializable unit of work needs at least one attempt")


def _worth_trying_again(error: SQLAlchemyError) -> bool:
    return getattr(getattr(error, "orig", None), "sqlstate", None) in _TRY_AGAIN


def _first_line(error: SQLAlchemyError) -> str:
    """The driver's first line names the problem; the rest is the SQL statement."""
    lines = str(getattr(error, "orig", None) or error).strip().splitlines()
    return lines[0] if lines else type(error).__name__
