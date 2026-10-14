"""add proposals

Adds ``proposal``: the actions an agent asked for that the policy gate held for a
person's decision, one row per action, keyed by the action's idempotency key.
Autogenerate wrote the effect and status CHECKs twice; the copies made by the
enum types are removed by hand, and so are its markers.

Revision ID: ae28cb0a146e
Revises: bbcf991fe9b4
Create Date: 2026-10-14 09:38:36.918867
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "ae28cb0a146e"
down_revision: str | None = "bbcf991fe9b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "proposal",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("run_kind", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_id", sa.Text(), nullable=False),
        sa.Column("action_id", sa.Text(), nullable=False),
        sa.Column("action_name", sa.Text(), nullable=False),
        sa.Column(
            "effect",
            sa.Enum(
                "internal",
                "publishes",
                "spends",
                name="effect",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "approved",
                "rejected",
                "expired",
                name="status",
                native_enum=False,
                length=32,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'pending') = (decided_at IS NULL)", name=op.f("ck_proposal_decided_at")
        ),
        sa.CheckConstraint(
            "(status IN ('approved', 'rejected')) = (decided_by IS NOT NULL)",
            name=op.f("ck_proposal_decided_by"),
        ),
        sa.CheckConstraint("effect <> 'internal'", name=op.f("ck_proposal_effect_held")),
        sa.CheckConstraint(
            "effect IN ('internal', 'publishes', 'spends')", name=op.f("ck_proposal_effect")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired')",
            name=op.f("ck_proposal_status"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["project.id"], name=op.f("fk_proposal_project_id_project")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_proposal")),
        sa.UniqueConstraint("action_id", name=op.f("uq_proposal_action_id")),
    )
    op.create_index(op.f("ix_proposal_project_id"), "proposal", ["project_id"], unique=False)
    op.create_index(
        "ix_proposal_project_id_status", "proposal", ["project_id", "status"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_proposal_project_id_status", table_name="proposal")
    op.drop_index(op.f("ix_proposal_project_id"), table_name="proposal")
    op.drop_table("proposal")
