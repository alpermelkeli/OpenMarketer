"""add social accounts and connection attempts

Adds ``social_account``: a project's account on a social platform, at most one per platform
(the unique constraint on project and platform is also the project's index), with the access
token and its secret encrypted by the application as stored credentials are. Adds
``social_connection_attempt``: a connection a person started that the platform has not
answered yet, named by the state the platform answers with, with the secret its answer needs,
encrypted too, and the time it expires; the index on workspace and expiry serves the removal
of a workspace's expired attempts. ``platform`` is text with a CHECK, the catalog's slugs.

Autogenerate wrote both tables as they are; only its markers are removed. The downgrade drops
the connected accounts and their tokens with the tables.

Revision ID: 3524068b40ad
Revises: a48df96b2004
Create Date: 2026-11-11 20:06:18.316642
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3524068b40ad"
down_revision: str | None = "a48df96b2004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "social_account",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("picture_url", sa.Text(), nullable=True),
        sa.Column("access_token_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("access_token_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("access_secret_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("access_secret_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("connected_by", sa.Uuid(), nullable=False),
        sa.Column(
            "connected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("platform IN ('x')", name=op.f("ck_social_account_platform")),
        sa.CheckConstraint(
            "char_length(account_id) BETWEEN 1 AND 64", name=op.f("ck_social_account_account_id")
        ),
        sa.CheckConstraint(
            "char_length(display_name) <= 200", name=op.f("ck_social_account_display_name")
        ),
        sa.CheckConstraint(
            "char_length(picture_url) <= 2048", name=op.f("ck_social_account_picture_url")
        ),
        sa.CheckConstraint(
            "char_length(username) BETWEEN 1 AND 100", name=op.f("ck_social_account_username")
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["project.id"], name=op.f("fk_social_account_project_id_project")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_social_account_workspace_id_workspace"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_social_account")),
        sa.UniqueConstraint(
            "project_id", "platform", name=op.f("uq_social_account_project_id_platform")
        ),
    )
    op.create_index(
        op.f("ix_social_account_workspace_id"), "social_account", ["workspace_id"], unique=False
    )
    op.create_table(
        "social_connection_attempt",
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("secret_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("secret_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("platform IN ('x')", name=op.f("ck_social_connection_attempt_platform")),
        sa.CheckConstraint(
            "char_length(state) BETWEEN 1 AND 512", name=op.f("ck_social_connection_attempt_state")
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_social_connection_attempt_project_id_project"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_social_connection_attempt_workspace_id_workspace"),
        ),
        sa.PrimaryKeyConstraint("state", name=op.f("pk_social_connection_attempt")),
    )
    op.create_index(
        op.f("ix_social_connection_attempt_project_id"),
        "social_connection_attempt",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        "ix_social_connection_attempt_workspace_id_expires_at",
        "social_connection_attempt",
        ["workspace_id", "expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_social_connection_attempt_workspace_id_expires_at",
        table_name="social_connection_attempt",
    )
    op.drop_index(
        op.f("ix_social_connection_attempt_project_id"), table_name="social_connection_attempt"
    )
    op.drop_table("social_connection_attempt")
    op.drop_index(op.f("ix_social_account_workspace_id"), table_name="social_account")
    op.drop_table("social_account")
