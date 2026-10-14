"""add workspace settings

Adds ``workspace_setting``: the choices a workspace made in the dashboard's
settings (the model of a tier or a role, the limits of an analysis), one row
per name, each a JSON object. The name has no CHECK and the value no schema:
both are code, and a value is checked again when it is read. Autogenerate
wrote the table as it is; only its markers are removed. The downgrade drops the
choices with the table, and the configuration file and the environment apply
again.

Revision ID: bbcf991fe9b4
Revises: e96ec2276ea4
Create Date: 2026-10-14 09:35:23.729074
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "bbcf991fe9b4"
down_revision: str | None = "e96ec2276ea4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace_setting",
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("set_by", sa.Uuid(), nullable=False),
        sa.Column(
            "set_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_workspace_setting_workspace_id_workspace"),
        ),
        sa.PrimaryKeyConstraint("workspace_id", "name", name=op.f("pk_workspace_setting")),
    )


def downgrade() -> None:
    op.drop_table("workspace_setting")
