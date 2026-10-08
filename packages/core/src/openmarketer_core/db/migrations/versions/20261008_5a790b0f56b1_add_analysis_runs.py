"""add analysis runs

Adds ``analysis_run`` and, on ``product_profile``, the unique constraint its
composite foreign key needs: a run's profile version must belong to the run's
project. Autogenerate wrote the status CHECK twice and put the unique
constraint after the table that references it (and dropped it first on the way
down); both are corrected by hand.

Revision ID: 5a790b0f56b1
Revises: ebeeaa3e3f08
Create Date: 2026-10-08 13:18:47.305319
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5a790b0f56b1"
down_revision: str | None = "ebeeaa3e3f08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        op.f("uq_product_profile_id_project_id"), "product_profile", ["id", "project_id"]
    )
    op.create_table(
        "analysis_run",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "succeeded",
                "failed",
                name="status",
                native_enum=False,
                length=32,
            ),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("profile_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'failed') = (error IS NOT NULL)", name=op.f("ck_analysis_run_error")
        ),
        sa.CheckConstraint(
            "(status = 'succeeded') = (profile_id IS NOT NULL)",
            name=op.f("ck_analysis_run_profile_id"),
        ),
        sa.CheckConstraint(
            "(status IN ('succeeded', 'failed')) = (finished_at IS NOT NULL)",
            name=op.f("ck_analysis_run_finished_at"),
        ),
        sa.CheckConstraint(
            "status = 'failed' OR (status = 'queued') = (started_at IS NULL)",
            name=op.f("ck_analysis_run_started_at"),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name=op.f("ck_analysis_run_status"),
        ),
        sa.CheckConstraint("char_length(error) <= 2000", name=op.f("ck_analysis_run_error_length")),
        sa.ForeignKeyConstraint(
            ["profile_id", "project_id"],
            ["product_profile.id", "product_profile.project_id"],
            name=op.f("fk_analysis_run_profile_id_product_profile"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["project.id"], name=op.f("fk_analysis_run_project_id_project")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analysis_run")),
    )
    op.create_index(
        op.f("ix_analysis_run_project_id"), "analysis_run", ["project_id"], unique=False
    )
    op.create_index(
        "uq_analysis_run_project_id_unfinished",
        "analysis_run",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_analysis_run_project_id_unfinished",
        table_name="analysis_run",
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.drop_index(op.f("ix_analysis_run_project_id"), table_name="analysis_run")
    op.drop_table("analysis_run")
    op.drop_constraint(op.f("uq_product_profile_id_project_id"), "product_profile", type_="unique")
