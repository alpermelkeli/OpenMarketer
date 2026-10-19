"""allow the html_reference technique

A studio version can be made from a layout rendered from HTML and given to the
image model as a reference, so the technique's CHECK takes one more value.

Revision ID: 7b1e2c4d9a10
Revises: f69548c2512f
Create Date: 2026-10-18 00:20:00

"""

from collections.abc import Sequence

from alembic import op

revision: str = "7b1e2c4d9a10"
down_revision: str | None = "f69548c2512f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The naming convention prefixes it: ck_media_asset_version_technique.
CHECK = "technique"


def upgrade() -> None:
    op.drop_constraint(CHECK, "media_asset_version", type_="check")
    op.create_check_constraint(
        CHECK,
        "media_asset_version",
        "technique IN ('generated', 'html_reference', 'background_overlay', 'composed')",
    )


def downgrade() -> None:
    op.execute("DELETE FROM media_asset_version WHERE technique = 'html_reference'")
    op.drop_constraint(CHECK, "media_asset_version", type_="check")
    op.create_check_constraint(
        CHECK,
        "media_asset_version",
        "technique IN ('generated', 'background_overlay', 'composed')",
    )
