"""create project, evidence and product profile tables

Revision ID: 9a5347e23b5c
Revises:
Create Date: 2026-10-08 02:06:22.495523
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9a5347e23b5c"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspace")),
    )
    op.create_table(
        "project",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("source_repo_url", sa.Text(), nullable=False),
        sa.Column("product_url", sa.Text(), nullable=True),
        sa.Column("playbook_key", sa.Text(), nullable=True),
        sa.Column("autonomy_level", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "onboarding",
                "active",
                "paused",
                "archived",
                name="status",
                native_enum=False,
                length=32,
            ),
            server_default="onboarding",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('onboarding', 'active', 'paused', 'archived')",
            name=op.f("ck_project_status"),
        ),
        sa.CheckConstraint(
            "autonomy_level BETWEEN 0 AND 3", name=op.f("ck_project_autonomy_level")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspace.id"], name=op.f("fk_project_workspace_id_workspace")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project")),
    )
    op.create_index(op.f("ix_project_workspace_id"), "project", ["workspace_id"], unique=False)
    op.create_table(
        "repo_snapshot",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("commit_sha", sa.Text(), nullable=False),
        sa.Column("ref", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["project.id"], name=op.f("fk_repo_snapshot_project_id_project")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_repo_snapshot")),
    )
    op.create_index(
        op.f("ix_repo_snapshot_project_id"), "repo_snapshot", ["project_id"], unique=False
    )
    op.create_table(
        "evidence",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("snapshot_id", sa.UUID(), nullable=False),
        sa.Column("extractor", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("file", sa.Text(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=True),
        sa.Column("end_line", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "end_line IS NULL OR (start_line IS NOT NULL AND end_line >= start_line)",
            name=op.f("ck_evidence_end_line"),
        ),
        sa.CheckConstraint(
            "start_line IS NULL OR start_line >= 1", name=op.f("ck_evidence_start_line")
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["project.id"], name=op.f("fk_evidence_project_id_project")
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["repo_snapshot.id"],
            name=op.f("fk_evidence_snapshot_id_repo_snapshot"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence")),
    )
    op.create_index(op.f("ix_evidence_project_id"), "evidence", ["project_id"], unique=False)
    op.create_index(
        "ix_evidence_snapshot_id_kind", "evidence", ["snapshot_id", "kind"], unique=False
    )
    op.create_table(
        "product_profile",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("snapshot_id", sa.UUID(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "draft",
                "approved",
                name="status",
                native_enum=False,
                length=32,
            ),
            server_default="draft",
            nullable=False,
        ),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("approved_by", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'approved') = (approved_at IS NOT NULL)",
            name=op.f("ck_product_profile_approved_at"),
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'approved')", name=op.f("ck_product_profile_status")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_product_profile_version")),
        sa.ForeignKeyConstraint(
            ["project_id"], ["project.id"], name=op.f("fk_product_profile_project_id_project")
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["repo_snapshot.id"],
            name=op.f("fk_product_profile_snapshot_id_repo_snapshot"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_product_profile")),
        sa.UniqueConstraint(
            "project_id", "version", name=op.f("uq_product_profile_project_id_version")
        ),
    )
    op.create_index(
        op.f("ix_product_profile_project_id"), "product_profile", ["project_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_product_profile_project_id"), table_name="product_profile")
    op.drop_table("product_profile")
    op.drop_index("ix_evidence_snapshot_id_kind", table_name="evidence")
    op.drop_index(op.f("ix_evidence_project_id"), table_name="evidence")
    op.drop_table("evidence")
    op.drop_index(op.f("ix_repo_snapshot_project_id"), table_name="repo_snapshot")
    op.drop_table("repo_snapshot")
    op.drop_index(op.f("ix_project_workspace_id"), table_name="project")
    op.drop_table("project")
    op.drop_table("workspace")
