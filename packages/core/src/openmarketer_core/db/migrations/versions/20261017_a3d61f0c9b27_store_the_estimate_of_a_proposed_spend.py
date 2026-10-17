"""store the estimate of a proposed spend

Adds ``proposal.estimate_usd``: what an action that spends was estimated to
cost, the amount the budget ledger holds while a person decides on it, so that
the person sees it. It is empty for every other action, which the CHECK
``estimate_of_a_spend`` holds, and for proposals stored before. The downgrade
drops the column and its CHECK: the estimates stay in the ledger.

Revision ID: a3d61f0c9b27
Revises: 5c77fe19d4bc
Create Date: 2026-10-17 21:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3d61f0c9b27"
down_revision: str | None = "5c77fe19d4bc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "proposal", sa.Column("estimate_usd", sa.Numeric(precision=14, scale=6), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_proposal_estimate_of_a_spend"),
        "proposal",
        "estimate_usd IS NULL OR (effect = 'spends' AND estimate_usd >= 0)",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_proposal_estimate_of_a_spend"), "proposal", type_="check")
    op.drop_column("proposal", "estimate_usd")
