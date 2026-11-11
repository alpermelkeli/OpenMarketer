"""publish a post from a connected account

An approved post that names an account is published from it, so a content item gains two
statuses, ``publishing`` (its send started and its outcome is not known yet; not settled) and
``published``, and the columns of its publishing: when the send started, the account it was
sent from (the platform's identifier and the username), the SHA-256 of its text as posts are
compared, the platform's identifier and address of the post, when it was published, what that
was reckoned to cost, and why the platform refused it. CHECKs hold which are set in which
status and their bounds; a platform's post is recorded for one item (a partial unique index),
and the items of a workspace that sent a text are found by its SHA-256 (a partial index).

Autogenerate wrote the columns and the two indexes; the CHECKs are written here, since it does
not compare them. The names are the naming convention's (``ck_content_item_<name>``). A
downgrade drops the columns and puts the old CHECKs back, and fails while an item is
publishing or published: a post a platform has is not deleted here behind a person's back.

Revision ID: a09e0cc156a8
Revises: 3524068b40ad
Create Date: 2026-11-11 20:59:47.264357
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a09e0cc156a8"
down_revision: str | None = "3524068b40ad"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_STATUSES = "'proposed', 'dry_run', 'rejected', 'expired', 'failed'"
NEW_STATUSES = "'proposed', 'dry_run', 'publishing', 'published', 'rejected', 'expired', 'failed'"
FAILURES = (
    "'access_revoked', 'duplicate', 'too_long', 'media_refused', 'not_permitted', "
    "'out_of_credits', 'refused'"
)
NEW_CHECKS = {
    "publishing": (
        "status NOT IN ('publishing', 'published') OR num_nulls(publishing_started_at,"
        " published_account_id, published_username, published_text_sha256) = 0"
    ),
    "published": (
        "CASE WHEN status = 'published'"
        " THEN num_nulls(external_id, external_url, published_at, publishing_cost_usd) = 0"
        " ELSE num_nonnulls(external_id, external_url, published_at, publishing_cost_usd) = 0"
        " END"
    ),
    "publishing_failure": (
        "publishing_failure IS NULL"
        " OR (status = 'failed' AND publishing_started_at IS NOT NULL"
        f" AND publishing_failure IN ({FAILURES}))"
    ),
    "published_account_id": "char_length(published_account_id) BETWEEN 1 AND 64",
    "published_username": "char_length(published_username) BETWEEN 1 AND 100",
    "published_text_sha256": "published_text_sha256 ~ '^[0-9a-f]{64}$'",
    "external_id": "char_length(external_id) BETWEEN 1 AND 64",
    "external_url": "char_length(external_url) BETWEEN 1 AND 2048",
    "publishing_cost_usd": "publishing_cost_usd >= 0",
}


def upgrade() -> None:
    op.add_column(
        "content_item",
        sa.Column("publishing_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("content_item", sa.Column("published_account_id", sa.Text(), nullable=True))
    op.add_column("content_item", sa.Column("published_username", sa.Text(), nullable=True))
    op.add_column("content_item", sa.Column("published_text_sha256", sa.Text(), nullable=True))
    op.add_column("content_item", sa.Column("external_id", sa.Text(), nullable=True))
    op.add_column("content_item", sa.Column("external_url", sa.Text(), nullable=True))
    op.add_column(
        "content_item", sa.Column("published_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "content_item",
        sa.Column("publishing_cost_usd", sa.Numeric(precision=14, scale=6), nullable=True),
    )
    op.add_column("content_item", sa.Column("publishing_failure", sa.Text(), nullable=True))
    op.create_index(
        "ix_content_item_published_text_sha256",
        "content_item",
        ["published_text_sha256"],
        unique=False,
        postgresql_where=sa.text("published_text_sha256 IS NOT NULL"),
    )
    op.create_index(
        "ux_content_item_platform_external_id",
        "content_item",
        ["platform", "external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )
    op.drop_constraint("status", "content_item", type_="check")
    op.create_check_constraint("status", "content_item", f"status IN ({NEW_STATUSES})")
    op.drop_constraint("settled_at", "content_item", type_="check")
    op.create_check_constraint(
        "settled_at",
        "content_item",
        "(status IN ('proposed', 'publishing')) = (settled_at IS NULL)",
    )
    for name, condition in NEW_CHECKS.items():
        op.create_check_constraint(name, "content_item", condition)


def downgrade() -> None:
    for name in NEW_CHECKS:
        op.drop_constraint(name, "content_item", type_="check")
    op.drop_constraint("settled_at", "content_item", type_="check")
    op.create_check_constraint(
        "settled_at", "content_item", "(status = 'proposed') = (settled_at IS NULL)"
    )
    op.drop_constraint("status", "content_item", type_="check")
    op.create_check_constraint("status", "content_item", f"status IN ({OLD_STATUSES})")
    op.drop_index(
        "ux_content_item_platform_external_id",
        table_name="content_item",
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )
    op.drop_index(
        "ix_content_item_published_text_sha256",
        table_name="content_item",
        postgresql_where=sa.text("published_text_sha256 IS NOT NULL"),
    )
    op.drop_column("content_item", "publishing_failure")
    op.drop_column("content_item", "publishing_cost_usd")
    op.drop_column("content_item", "published_at")
    op.drop_column("content_item", "external_url")
    op.drop_column("content_item", "external_id")
    op.drop_column("content_item", "published_text_sha256")
    op.drop_column("content_item", "published_username")
    op.drop_column("content_item", "published_account_id")
    op.drop_column("content_item", "publishing_started_at")
