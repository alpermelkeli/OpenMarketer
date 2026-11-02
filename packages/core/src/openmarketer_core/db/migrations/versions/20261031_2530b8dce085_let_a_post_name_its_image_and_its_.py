"""let a post name its image and its research

``content_item`` gains ``asset_id`` and ``asset_version_id``, the image of the project's
library a post goes with, and ``research_run_id``, the research run whose findings it drew on.
All three are nullable. Each is named together with something that makes it the project's: the
asset with the item's project, the version with the item's asset, the research run with the
item's project; ``media_asset`` and ``research_run`` gain a unique ``(id, project_id)`` for it.
A CHECK holds the asset and its version to both or neither. Autogenerate created the foreign
keys before the unique constraints they rest on, and wrote no CHECK; both are put right here.

The downgrade forgets which image and research each post used.

Revision ID: 2530b8dce085
Revises: 6afb465902a5
Create Date: 2026-10-31 17:20:41.716041
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2530b8dce085"
down_revision: str | None = "6afb465902a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        op.f("uq_media_asset_id_project_id"), "media_asset", ["id", "project_id"]
    )
    op.create_unique_constraint(
        op.f("uq_research_run_id_project_id"), "research_run", ["id", "project_id"]
    )
    op.add_column("content_item", sa.Column("asset_id", sa.Uuid(), nullable=True))
    op.add_column("content_item", sa.Column("asset_version_id", sa.Uuid(), nullable=True))
    op.add_column("content_item", sa.Column("research_run_id", sa.Uuid(), nullable=True))
    op.create_index(op.f("ix_content_item_asset_id"), "content_item", ["asset_id"], unique=False)
    op.create_index(
        op.f("ix_content_item_research_run_id"), "content_item", ["research_run_id"], unique=False
    )
    op.create_foreign_key(
        op.f("fk_content_item_asset_id_media_asset"),
        "content_item",
        "media_asset",
        ["asset_id", "project_id"],
        ["id", "project_id"],
    )
    op.create_foreign_key(
        op.f("fk_content_item_asset_version_id_media_asset_version"),
        "content_item",
        "media_asset_version",
        ["asset_version_id", "asset_id"],
        ["id", "asset_id"],
    )
    op.create_foreign_key(
        op.f("fk_content_item_research_run_id_research_run"),
        "content_item",
        "research_run",
        ["research_run_id", "project_id"],
        ["id", "project_id"],
    )
    op.create_check_constraint(
        op.f("ck_content_item_asset_version_id"),
        "content_item",
        "(asset_id IS NULL) = (asset_version_id IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_content_item_asset_version_id"), "content_item", type_="check")
    op.drop_constraint(
        op.f("fk_content_item_research_run_id_research_run"), "content_item", type_="foreignkey"
    )
    op.drop_constraint(
        op.f("fk_content_item_asset_version_id_media_asset_version"),
        "content_item",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_content_item_asset_id_media_asset"), "content_item", type_="foreignkey"
    )
    op.drop_index(op.f("ix_content_item_research_run_id"), table_name="content_item")
    op.drop_index(op.f("ix_content_item_asset_id"), table_name="content_item")
    op.drop_column("content_item", "research_run_id")
    op.drop_column("content_item", "asset_version_id")
    op.drop_column("content_item", "asset_id")
    op.drop_constraint(op.f("uq_research_run_id_project_id"), "research_run", type_="unique")
    op.drop_constraint(op.f("uq_media_asset_id_project_id"), "media_asset", type_="unique")
