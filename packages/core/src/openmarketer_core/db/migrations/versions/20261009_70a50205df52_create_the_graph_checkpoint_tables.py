"""create the graph checkpoint tables

Creates the tables of LangGraph's PostgreSQL checkpointer, so that nothing has
to call the library's ``setup()`` at run time. The tables are not models of
this package: the statements are the library's own, numbers 0 to 9 of its
``MIGRATIONS`` list, run in order, and each number is recorded in the library's
``checkpoint_migrations`` table exactly as ``setup()`` records it. A later
``setup()`` therefore finds nothing to do.

Two things differ from ``setup()``. The range is fixed here, so this revision
means the same schema whatever the installed library adds later; statements
after number 9 belong in a new revision. And the three ``CREATE INDEX
CONCURRENTLY`` statements run without ``CONCURRENTLY``: it cannot run inside
the transaction Alembic migrates in, and it only matters for a table that
already has rows and writers, while these tables are created empty in this
same transaction.

The statements are text of an installed package, so what this revision runs is
pinned: ``STATEMENTS_SHA256`` is the SHA-256 of the statements exactly as they
are sent to the database (after ``CONCURRENTLY`` is taken out), and the
revision refuses to run anything else. The statements were read when the hash
was recorded; a library version that words them differently is not run unread.

Revision ID: 70a50205df52
Revises: 5a790b0f56b1
Create Date: 2026-10-09 11:09:39.443830
"""

import hashlib
from collections.abc import Sequence

from alembic import op
from langgraph.checkpoint.postgres.base import MIGRATIONS

revision: str = "70a50205df52"
down_revision: str | None = "5a790b0f56b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LAST_LIBRARY_MIGRATION = 9
# Of the statements this revision sends, as ``statements_digest`` computes it. Recorded with
# langgraph-checkpoint-postgres 3.1.2, after reading them.
STATEMENTS_SHA256 = "91c68b5da24f1951bead3e6663dd9b4385b9898706074bfe40b75a22a346047a"


def statements_digest(statements: Sequence[str]) -> str:
    """SHA-256 over the statements in order, each preceded by its length in bytes."""
    digest = hashlib.sha256()
    for statement in statements:
        encoded = statement.encode()
        # The length keeps two lists that only split their text differently apart.
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def statements_to_run(library_migrations: Sequence[str]) -> list[str]:
    """The statements this revision sends to the database, or an error when they are not
    the ones it was written for."""
    if len(library_migrations) <= LAST_LIBRARY_MIGRATION:
        raise RuntimeError(
            "the installed langgraph-checkpoint-postgres has "
            f"{len(library_migrations)} schema migrations; this revision needs numbers 0 to "
            f"{LAST_LIBRARY_MIGRATION}"
        )
    statements = [
        statement.replace(" CONCURRENTLY ", " ")
        for statement in library_migrations[: LAST_LIBRARY_MIGRATION + 1]
    ]
    found = statements_digest(statements)
    if found != STATEMENTS_SHA256:
        raise RuntimeError(
            "the installed langgraph-checkpoint-postgres words its schema migrations 0 to "
            f"{LAST_LIBRARY_MIGRATION} differently from the statements this revision was "
            f"written for (SHA-256 {found}, expected {STATEMENTS_SHA256}), so they are not "
            "run. Install a version within the bounds in packages/core/pyproject.toml. If "
            "the library did change these statements, read the new ones and add a revision "
            "for the difference; do not replace the hash without reading what it stands for."
        )
    return statements


def upgrade() -> None:
    connection = op.get_bind()
    # Exactly the list that was hashed is what is executed.
    for number, statement in enumerate(statements_to_run(MIGRATIONS)):
        connection.exec_driver_sql(statement)
        connection.exec_driver_sql(
            "INSERT INTO checkpoint_migrations (v) VALUES (%(number)s)", {"number": number}
        )


def downgrade() -> None:
    op.drop_table("checkpoint_writes")
    op.drop_table("checkpoint_blobs")
    op.drop_table("checkpoints")
    op.drop_table("checkpoint_migrations")
