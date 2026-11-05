"""let a post be written for instagram

The writer writes a post for Instagram too, so the platform CHECK of a writing
run and of a content item takes one more value. A downgrade puts the old CHECK
back and fails while a run or an item for Instagram is stored: a run and its
item reference each other, so they are not deleted here behind a person's back.

Revision ID: 86a789f035d3
Revises: 0a4fdc44993b
Create Date: 2026-11-02 22:00:53.136920
"""

from collections.abc import Sequence

from alembic import op

revision: str = "86a789f035d3"
down_revision: str | None = "0a4fdc44993b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The naming convention prefixes it: ck_writing_run_platform, ck_content_item_platform.
CHECK = "platform"
TABLES = ("writing_run", "content_item")


def upgrade() -> None:
    for table in TABLES:
        op.drop_constraint(CHECK, table, type_="check")
        op.create_check_constraint(CHECK, table, "platform IN ('x', 'instagram')")


def downgrade() -> None:
    for table in TABLES:
        op.drop_constraint(CHECK, table, type_="check")
        op.create_check_constraint(CHECK, table, "platform IN ('x')")
