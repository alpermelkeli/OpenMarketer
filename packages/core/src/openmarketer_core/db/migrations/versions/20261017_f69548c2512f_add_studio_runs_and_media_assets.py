"""add studio runs and media assets

Adds the creative studio's tables. ``studio_run`` is a requested run, built like
``research_run``: it moves forwards only, which its CHECKs hold, and a run that
succeeded or needs review names its final asset. ``media_asset`` is an image a run
made, a draft until a person accepts or rejects it, and ``media_asset_version``
each candidate of it with its provenance and its reviews; the images themselves
are in object storage, under ``storage_key``.

The three tables name each other in two cycles: a run its final asset, and an
asset its current version. Autogenerate put those two foreign keys in the
``CREATE TABLE`` with ``use_alter``, where they are left out without a word; they
are added by hand after the three tables exist, and dropped first on the way
down. Each is composite, so a run's final asset is one of its own assets and an
asset's current version one of its own versions.

``chat_turn`` gains ``studio_run_id`` and the trigger ``studio_finished``: a chat
follows up on a studio run it started as it does on a research run. Autogenerate
saw the column only; the CHECK of the trigger's values is replaced by hand, and
the CHECK that ties the column to its trigger added. It also wrote the CHECKs of
the new enum columns twice; the copies made by the enum types are removed, so the
columns' enums carry no constraint of their own, as in the earlier migrations.

The downgrade drops the studio's rows with their tables: runs, assets and
versions are forgotten, and their images are left in object storage. The chat's
follow-up turns on studio runs go with their events; the messages they wrote stay
in their chats, no longer naming a turn, as messages stored before turns were.
The events studio runs recorded go too.

Revision ID: f69548c2512f
Revises: a3d61f0c9b27
Create Date: 2026-10-17 19:42:23.562207
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# The platforms of ``studio/platforms.py`` as they were when this revision was written.
PLATFORMS = (
    "platform IN ('instagram_post', 'instagram_portrait', 'instagram_story', 'x_post',"
    " 'linkedin_post', 'og_image', 'app_store_screenshot')"
)

revision: str = "f69548c2512f"
down_revision: str | None = "a3d61f0c9b27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "studio_run",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "succeeded",
                "needs_review",
                "failed",
                name="status",
                native_enum=False,
                length=32,
            ),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("final_asset_id", sa.Uuid(), nullable=True),
        sa.Column("requested_by", sa.Text(), server_default="person", nullable=False),
        sa.Column("origin_action_id", sa.Text(), nullable=True),
        sa.Column("request", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
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
            "(status = 'failed') = (error IS NOT NULL)", name=op.f("ck_studio_run_error")
        ),
        sa.CheckConstraint(
            "(status IN ('succeeded', 'needs_review')) = (final_asset_id IS NOT NULL)",
            name=op.f("ck_studio_run_final_asset_id"),
        ),
        sa.CheckConstraint(
            "(status IN ('succeeded', 'needs_review', 'failed')) = (finished_at IS NOT NULL)",
            name=op.f("ck_studio_run_finished_at"),
        ),
        sa.CheckConstraint(
            PLATFORMS,
            name=op.f("ck_studio_run_platform"),
        ),
        sa.CheckConstraint(
            "requested_by IN ('person', 'orchestrator', 'run')",
            name=op.f("ck_studio_run_requested_by"),
        ),
        sa.CheckConstraint(
            "status = 'failed' OR (status = 'queued') = (started_at IS NULL)",
            name=op.f("ck_studio_run_started_at"),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'needs_review', 'failed')",
            name=op.f("ck_studio_run_status"),
        ),
        sa.CheckConstraint("char_length(error) <= 2000", name=op.f("ck_studio_run_error_length")),
        sa.CheckConstraint("cost_usd >= 0", name=op.f("ck_studio_run_cost_usd")),
        sa.ForeignKeyConstraint(
            ["chat_thread_id"],
            ["chat_thread.id"],
            name=op.f("fk_studio_run_chat_thread_id_chat_thread"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["project.id"], name=op.f("fk_studio_run_project_id_project")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_studio_run")),
        sa.UniqueConstraint("origin_action_id", name=op.f("uq_studio_run_origin_action_id")),
    )
    op.create_index(
        op.f("ix_studio_run_chat_thread_id"), "studio_run", ["chat_thread_id"], unique=False
    )
    op.create_index(op.f("ix_studio_run_project_id"), "studio_run", ["project_id"], unique=False)
    op.create_table(
        "media_asset",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "draft",
                "accepted",
                "rejected",
                name="status",
                native_enum=False,
                length=32,
            ),
            server_default="draft",
            nullable=False,
        ),
        sa.Column("current_version_id", sa.Uuid(), nullable=True),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'draft') = (decided_at IS NULL)", name=op.f("ck_media_asset_decided_at")
        ),
        sa.CheckConstraint(
            "(status = 'draft') = (decided_by IS NULL)", name=op.f("ck_media_asset_decided_by")
        ),
        sa.CheckConstraint(
            PLATFORMS,
            name=op.f("ck_media_asset_platform"),
        ),
        sa.CheckConstraint(
            "status = 'draft' OR current_version_id IS NOT NULL",
            name=op.f("ck_media_asset_current_version_id"),
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'accepted', 'rejected')", name=op.f("ck_media_asset_status")
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["project.id"], name=op.f("fk_media_asset_project_id_project")
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["studio_run.id"], name=op.f("fk_media_asset_run_id_studio_run")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_asset")),
        sa.UniqueConstraint("id", "run_id", name=op.f("uq_media_asset_id_run_id")),
    )
    op.create_index(op.f("ix_media_asset_project_id"), "media_asset", ["project_id"], unique=False)
    op.create_index(
        "ix_media_asset_project_id_created_at",
        "media_asset",
        ["project_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(op.f("ix_media_asset_run_id"), "media_asset", ["run_id"], unique=False)
    op.create_table(
        "media_asset_version",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("asset_id", sa.UUID(), nullable=False),
        sa.Column("action_id", sa.Text(), nullable=False),
        sa.Column("round", sa.SmallInteger(), nullable=False),
        sa.Column(
            "technique",
            sa.Enum(
                "generated",
                "background_overlay",
                "composed",
                name="technique",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("mime", sa.Text(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("code", sa.Text(), nullable=True),
        sa.Column("check_report", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("verifier_report", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(precision=14, scale=6), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "mime IN ('image/png', 'image/jpeg', 'image/webp')",
            name=op.f("ck_media_asset_version_mime"),
        ),
        sa.CheckConstraint(
            "technique IN ('generated', 'background_overlay', 'composed')",
            name=op.f("ck_media_asset_version_technique"),
        ),
        sa.CheckConstraint(
            "(passed IS NULL) = (check_report IS NULL)",
            name=op.f("ck_media_asset_version_check_report"),
        ),
        sa.CheckConstraint("cost_usd >= 0", name=op.f("ck_media_asset_version_cost_usd")),
        sa.CheckConstraint(
            "passed IS NOT NULL OR verifier_report IS NULL",
            name=op.f("ck_media_asset_version_verifier_report"),
        ),
        sa.CheckConstraint("round >= 1", name=op.f("ck_media_asset_version_round")),
        sa.CheckConstraint("size_bytes > 0", name=op.f("ck_media_asset_version_size_bytes")),
        sa.CheckConstraint("width > 0 AND height > 0", name=op.f("ck_media_asset_version_size")),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["media_asset.id"],
            name=op.f("fk_media_asset_version_asset_id_media_asset"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["project.id"], name=op.f("fk_media_asset_version_project_id_project")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_asset_version")),
        sa.UniqueConstraint("action_id", name=op.f("uq_media_asset_version_action_id")),
        sa.UniqueConstraint("id", "asset_id", name=op.f("uq_media_asset_version_id_asset_id")),
    )
    op.create_index(
        "ix_media_asset_version_asset_id_created_at",
        "media_asset_version",
        ["asset_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_media_asset_version_project_id"),
        "media_asset_version",
        ["project_id"],
        unique=False,
    )
    op.create_foreign_key(
        op.f("fk_studio_run_final_asset_id_media_asset"),
        "studio_run",
        "media_asset",
        ["final_asset_id", "id"],
        ["id", "run_id"],
    )
    op.create_foreign_key(
        op.f("fk_media_asset_current_version_id_media_asset_version"),
        "media_asset",
        "media_asset_version",
        ["current_version_id", "id"],
        ["id", "asset_id"],
    )
    op.add_column("chat_turn", sa.Column("studio_run_id", sa.Uuid(), nullable=True))
    op.drop_constraint(op.f("ck_chat_turn_trigger"), "chat_turn", type_="check")
    op.create_check_constraint(
        op.f("ck_chat_turn_trigger"),
        "chat_turn",
        "trigger IN ('message', 'research_finished', 'studio_finished')",
    )
    op.create_check_constraint(
        op.f("ck_chat_turn_studio_run_id"),
        "chat_turn",
        "(trigger = 'studio_finished') = (studio_run_id IS NOT NULL)",
    )


def downgrade() -> None:
    studio_turns = "SELECT id FROM chat_turn WHERE trigger = 'studio_finished'"
    op.execute(f"UPDATE chat_message SET turn_id = NULL WHERE turn_id IN ({studio_turns})")
    op.execute(
        f"DELETE FROM run_event WHERE run_kind = 'orchestrator-chat' AND run_id IN ({studio_turns})"
    )
    op.execute("DELETE FROM run_event WHERE run_kind = 'studio-run'")
    op.execute("DELETE FROM chat_turn WHERE trigger = 'studio_finished'")
    op.drop_constraint(op.f("ck_chat_turn_studio_run_id"), "chat_turn", type_="check")
    op.drop_constraint(op.f("ck_chat_turn_trigger"), "chat_turn", type_="check")
    op.create_check_constraint(
        op.f("ck_chat_turn_trigger"), "chat_turn", "trigger IN ('message', 'research_finished')"
    )
    op.drop_column("chat_turn", "studio_run_id")
    op.drop_constraint(
        op.f("fk_media_asset_current_version_id_media_asset_version"),
        "media_asset",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_studio_run_final_asset_id_media_asset"), "studio_run", type_="foreignkey"
    )
    op.drop_index(op.f("ix_media_asset_version_project_id"), table_name="media_asset_version")
    op.drop_index("ix_media_asset_version_asset_id_created_at", table_name="media_asset_version")
    op.drop_table("media_asset_version")
    op.drop_index(op.f("ix_media_asset_run_id"), table_name="media_asset")
    op.drop_index("ix_media_asset_project_id_created_at", table_name="media_asset")
    op.drop_index(op.f("ix_media_asset_project_id"), table_name="media_asset")
    op.drop_table("media_asset")
    op.drop_index(op.f("ix_studio_run_project_id"), table_name="studio_run")
    op.drop_index(op.f("ix_studio_run_chat_thread_id"), table_name="studio_run")
    op.drop_table("studio_run")
