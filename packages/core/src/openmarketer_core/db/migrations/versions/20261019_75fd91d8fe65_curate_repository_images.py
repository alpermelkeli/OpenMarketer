"""curate repository images

A person curates a project's repository images: uploads their own, removes one and
chooses an image's kind. ``repository_image`` gains ``source`` (``analysis`` for
every existing row, ``person`` for an upload, which has no run), who chose the
kind (``kind_chosen_by``) and a soft removal (``removed_at``, ``removed_by``) that
a later analysis carries over by hash. The list now reads a project's rows newest
first, so the index by run is replaced by one by project; a project keeps
identical uploads once. Autogenerate left out the CHECKs and gave the enum a
constraint of its own; both are written here, as in the earlier migrations.

The downgrade deletes the uploads' rows (their objects are left in object storage)
and forgets every removal and chosen kind.

Revision ID: 75fd91d8fe65
Revises: a9f4233f3134
Create Date: 2026-10-19 16:55:06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "75fd91d8fe65"
down_revision: str | None = "a9f4233f3134"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "repository_image"
# The naming convention prefixes each: ck_repository_image_<name>.
CHECKS = {
    "source": "source IN ('analysis', 'person')",
    "analysis_run_id": "(source = 'analysis') = (analysis_run_id IS NOT NULL)",
    "kind_chosen_by": "source = 'analysis' OR kind_chosen_by IS NOT NULL",
    "removed_by": "(removed_at IS NULL) = (removed_by IS NULL)",
    "removed_at": "source = 'analysis' OR removed_at IS NULL",
}


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column(
            "source",
            sa.Enum("analysis", "person", name="source", native_enum=False, length=32),
            server_default="analysis",
            nullable=False,
        ),
    )
    op.add_column(TABLE, sa.Column("kind_chosen_by", sa.Uuid(), nullable=True))
    op.add_column(TABLE, sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(TABLE, sa.Column("removed_by", sa.Uuid(), nullable=True))
    op.alter_column(TABLE, "analysis_run_id", existing_type=sa.UUID(), nullable=True)
    for name, condition in CHECKS.items():
        op.create_check_constraint(name, TABLE, condition)

    op.drop_index("ix_repository_image_analysis_run_id_created_at", table_name=TABLE)
    op.create_index(
        "ix_repository_image_project_id_created_at", TABLE, ["project_id", "created_at", "id"]
    )
    op.create_index(
        "uq_repository_image_project_id_sha256_person",
        TABLE,
        ["project_id", "sha256"],
        unique=True,
        postgresql_where=sa.text("source = 'person'"),
    )


def downgrade() -> None:
    op.execute("DELETE FROM repository_image WHERE source = 'person'")
    op.drop_index("uq_repository_image_project_id_sha256_person", table_name=TABLE)
    op.drop_index("ix_repository_image_project_id_created_at", table_name=TABLE)
    op.create_index(
        "ix_repository_image_analysis_run_id_created_at",
        TABLE,
        ["analysis_run_id", "created_at", "id"],
    )
    for name in CHECKS:
        op.drop_constraint(name, TABLE, type_="check")
    op.alter_column(TABLE, "analysis_run_id", existing_type=sa.UUID(), nullable=False)
    op.drop_column(TABLE, "removed_by")
    op.drop_column(TABLE, "removed_at")
    op.drop_column(TABLE, "kind_chosen_by")
    op.drop_column(TABLE, "source")
