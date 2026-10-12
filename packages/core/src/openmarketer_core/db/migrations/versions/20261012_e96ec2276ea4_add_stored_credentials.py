"""add stored credentials

Adds ``credential``: the secrets of a workspace that were set from the
dashboard, one row per name, each encrypted by the application (the database
holds a nonce and a ciphertext, never a key). The name has no CHECK: the list
of credentials is code. Autogenerate wrote the table as it is; only its
markers are removed. The downgrade drops the stored values with the table.

Revision ID: e96ec2276ea4
Revises: 70a50205df52
Create Date: 2026-10-12 18:04:21.651202
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e96ec2276ea4"
down_revision: str | None = "70a50205df52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "credential",
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("set_by", sa.Uuid(), nullable=False),
        sa.Column(
            "set_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspace.id"], name=op.f("fk_credential_workspace_id_workspace")
        ),
        sa.PrimaryKeyConstraint("workspace_id", "name", name=op.f("pk_credential")),
    )


def downgrade() -> None:
    op.drop_table("credential")
