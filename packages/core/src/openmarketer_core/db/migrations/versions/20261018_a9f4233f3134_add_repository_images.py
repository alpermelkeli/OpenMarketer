"""add repository images

Adds ``repository_image``: the product's own images an analysis run found in the
repository (icon, logo, screenshots, marketing images), one row each, with the
image itself in object storage under ``storage_key``. A run keeps a path once and
identical bytes once. Autogenerate wrote the CHECK of ``kind`` twice; the copy
made by the enum type is removed, so the column's enum carries no constraint of
its own, as in the earlier migrations.

The downgrade drops the rows with the table; their images are left in object
storage.

Revision ID: a9f4233f3134
Revises: 7b1e2c4d9a10
Create Date: 2026-10-18 12:09:30

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a9f4233f3134"
down_revision: str | None = "7b1e2c4d9a10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "repository_image",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("analysis_run_id", sa.UUID(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "icon",
                "logo",
                "screenshot",
                "marketing",
                "other",
                name="kind",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("mime", sa.Text(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('icon', 'logo', 'screenshot', 'marketing', 'other')",
            name=op.f("ck_repository_image_kind"),
        ),
        sa.CheckConstraint(
            "mime IN ('image/png', 'image/webp')", name=op.f("ck_repository_image_mime")
        ),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_repository_image_sha256")),
        sa.CheckConstraint(
            "char_length(path) BETWEEN 1 AND 1000", name=op.f("ck_repository_image_path")
        ),
        sa.CheckConstraint("size_bytes > 0", name=op.f("ck_repository_image_size_bytes")),
        sa.CheckConstraint("width > 0 AND height > 0", name=op.f("ck_repository_image_size")),
        sa.ForeignKeyConstraint(
            ["analysis_run_id"],
            ["analysis_run.id"],
            name=op.f("fk_repository_image_analysis_run_id_analysis_run"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["project.id"], name=op.f("fk_repository_image_project_id_project")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_repository_image")),
        sa.UniqueConstraint(
            "analysis_run_id", "path", name=op.f("uq_repository_image_analysis_run_id_path")
        ),
        sa.UniqueConstraint(
            "analysis_run_id", "sha256", name=op.f("uq_repository_image_analysis_run_id_sha256")
        ),
    )
    op.create_index(
        "ix_repository_image_analysis_run_id_created_at",
        "repository_image",
        ["analysis_run_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_repository_image_project_id"), "repository_image", ["project_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_repository_image_project_id"), table_name="repository_image")
    op.drop_index("ix_repository_image_analysis_run_id_created_at", table_name="repository_image")
    op.drop_table("repository_image")
