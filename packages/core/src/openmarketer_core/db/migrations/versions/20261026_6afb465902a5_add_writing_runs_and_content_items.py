"""add writing runs and content items

Adds the writer's tables. ``writing_run`` is a requested post, built like ``studio_run``: it
moves forwards only, which its CHECKs hold, it keeps the brief it answers, the approved profile
version its ``prepare`` chose and what its model calls cost, and a run that succeeded names
its content item. ``content_item`` is the post a run proposed, one per run, with the record of
how it was written (plan, alternatives, claims, the code rules' and the critic's reports, the
numbers the profile does not hold) and how it ended; it names its run's profile version and
the proposal the gate held it as. Both name the profile version together with their project,
so it is a version of the same project.

The two tables name each other: a run its item, and an item its run. Autogenerate put the
run's foreign key in the ``CREATE TABLE`` with ``use_alter``, where it is left out without a
word; it is added by hand after both tables exist, and dropped first on the way down. It is
composite, so a run's item is its own. Autogenerate also wrote the CHECKs of the enum columns
twice; the copies made by the enum types are removed, so the columns' enums carry no
constraint of their own, as in the earlier migrations.

The downgrade drops the writer's rows with their tables, and with them the proposals and the
events of writing runs, which name their run without a foreign key.

Revision ID: 6afb465902a5
Revises: 75fd91d8fe65
Create Date: 2026-10-26 21:30:01.377171
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "6afb465902a5"
down_revision: str | None = "75fd91d8fe65"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "writing_run",
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
        sa.Column("requested_by", sa.Text(), server_default="person", nullable=False),
        sa.Column("brief", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("profile_version_id", sa.Uuid(), nullable=True),
        sa.Column("content_item_id", sa.Uuid(), nullable=True),
        sa.Column(
            "cost_usd",
            sa.Numeric(precision=14, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("chat_thread_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'failed') = (error IS NOT NULL)", name=op.f("ck_writing_run_error")
        ),
        sa.CheckConstraint(
            "(status IN ('succeeded', 'failed')) = (finished_at IS NOT NULL)",
            name=op.f("ck_writing_run_finished_at"),
        ),
        sa.CheckConstraint("platform IN ('x')", name=op.f("ck_writing_run_platform")),
        sa.CheckConstraint(
            "requested_by IN ('person', 'orchestrator', 'run')",
            name=op.f("ck_writing_run_requested_by"),
        ),
        sa.CheckConstraint(
            "status <> 'succeeded' OR content_item_id IS NOT NULL",
            name=op.f("ck_writing_run_content_item_id"),
        ),
        sa.CheckConstraint(
            "status = 'failed' OR (status = 'queued') = (started_at IS NULL)",
            name=op.f("ck_writing_run_started_at"),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name=op.f("ck_writing_run_status"),
        ),
        sa.CheckConstraint("char_length(error) <= 2000", name=op.f("ck_writing_run_error_length")),
        sa.CheckConstraint(
            "content_item_id IS NULL OR profile_version_id IS NOT NULL",
            name=op.f("ck_writing_run_profile_version_id"),
        ),
        sa.CheckConstraint("cost_usd >= 0", name=op.f("ck_writing_run_cost_usd")),
        sa.ForeignKeyConstraint(
            ["chat_thread_id"],
            ["chat_thread.id"],
            name=op.f("fk_writing_run_chat_thread_id_chat_thread"),
        ),
        sa.ForeignKeyConstraint(
            ["profile_version_id", "project_id"],
            ["product_profile.id", "product_profile.project_id"],
            name=op.f("fk_writing_run_profile_version_id_product_profile"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["project.id"], name=op.f("fk_writing_run_project_id_project")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_writing_run")),
    )
    op.create_index(
        op.f("ix_writing_run_chat_thread_id"), "writing_run", ["chat_thread_id"], unique=False
    )
    op.create_index(op.f("ix_writing_run_project_id"), "writing_run", ["project_id"], unique=False)
    op.create_table(
        "content_item",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "proposed",
                "dry_run",
                "rejected",
                "expired",
                "failed",
                name="status",
                native_enum=False,
                length=32,
            ),
            server_default="proposed",
            nullable=False,
        ),
        sa.Column("plan", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("plan_changes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("alternatives", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("claims", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("check_report", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("critic_report", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("flagged_numbers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("profile_version_id", sa.Uuid(), nullable=False),
        sa.Column("proposal_id", sa.UUID(), nullable=True),
        sa.Column(
            "cost_usd", sa.Numeric(precision=14, scale=6), server_default="0", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'proposed') = (settled_at IS NULL)", name=op.f("ck_content_item_settled_at")
        ),
        sa.CheckConstraint("platform IN ('x')", name=op.f("ck_content_item_platform")),
        sa.CheckConstraint(
            "status IN ('proposed', 'dry_run', 'rejected', 'expired', 'failed')",
            name=op.f("ck_content_item_status"),
        ),
        sa.CheckConstraint("char_length(text) > 0", name=op.f("ck_content_item_text")),
        sa.CheckConstraint("cost_usd >= 0", name=op.f("ck_content_item_cost_usd")),
        sa.ForeignKeyConstraint(
            ["profile_version_id", "project_id"],
            ["product_profile.id", "product_profile.project_id"],
            name=op.f("fk_content_item_profile_version_id_product_profile"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["project.id"], name=op.f("fk_content_item_project_id_project")
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["proposal.id"], name=op.f("fk_content_item_proposal_id_proposal")
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["writing_run.id"], name=op.f("fk_content_item_run_id_writing_run")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_content_item")),
        sa.UniqueConstraint("id", "run_id", name=op.f("uq_content_item_id_run_id")),
        sa.UniqueConstraint("run_id", name=op.f("uq_content_item_run_id")),
    )
    op.create_index(
        op.f("ix_content_item_project_id"), "content_item", ["project_id"], unique=False
    )
    op.create_index(
        "ix_content_item_project_id_created_at",
        "content_item",
        ["project_id", "created_at", "id"],
        unique=False,
    )
    op.create_foreign_key(
        op.f("fk_writing_run_content_item_id_content_item"),
        "writing_run",
        "content_item",
        ["content_item_id", "id"],
        ["id", "run_id"],
    )


def downgrade() -> None:
    op.execute("DELETE FROM run_event WHERE run_kind = 'writing-run'")
    op.drop_constraint(
        op.f("fk_writing_run_content_item_id_content_item"), "writing_run", type_="foreignkey"
    )
    op.drop_index("ix_content_item_project_id_created_at", table_name="content_item")
    op.drop_index(op.f("ix_content_item_project_id"), table_name="content_item")
    op.drop_table("content_item")
    op.drop_index(op.f("ix_writing_run_project_id"), table_name="writing_run")
    op.drop_index(op.f("ix_writing_run_chat_thread_id"), table_name="writing_run")
    op.drop_table("writing_run")
    op.execute("DELETE FROM proposal WHERE run_kind = 'writing-run'")
