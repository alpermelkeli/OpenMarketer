"""add budget ledger

Adds ``budget_ledger_entry``: the ledger of what a workspace's runs spend on
generated media (``openmarketer_core.budget``). An action's estimate is reserved
once and settled at most once, by a commit or a release; two partial unique
indexes on the run's kind and the action's key hold that, and balances are
derived by query. The project and the run are named without a foreign key, so
a deleted project's spending still counts against the workspace's cap.

The trigger ``budget_ledger_entry_append_only`` is written by hand, since the
model cannot show it: it refuses every update and delete, and a commit or a
release for an action that has no reservation in the same workspace.

Autogenerate wrote the CHECKs of ``entry_kind`` and ``decision`` twice; the
copies made by the enum types are removed by hand, so the columns' enums carry
no constraint of their own, as in the earlier migrations. The downgrade drops
the ledger with its table: what was spent and held is forgotten.

Revision ID: 5c77fe19d4bc
Revises: 4897ea1d143f
Create Date: 2026-10-17 19:20:55.587097
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5c77fe19d4bc"
down_revision: str | None = "4897ea1d143f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_FUNCTION = """
CREATE FUNCTION budget_ledger_entry_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'budget ledger entry % cannot be changed or deleted', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.entry_kind <> 'reserve' AND NOT EXISTS (
        SELECT 1 FROM budget_ledger_entry AS reserved
        WHERE reserved.entry_kind = 'reserve'
          AND reserved.run_kind = NEW.run_kind
          AND reserved.action_id = NEW.action_id
          AND reserved.workspace_id = NEW.workspace_id
    ) THEN
        RAISE EXCEPTION 'a budget ledger entry settles only a reservation of its own workspace'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$
"""

APPEND_ONLY_TRIGGER = """
CREATE TRIGGER budget_ledger_entry_append_only
BEFORE INSERT OR UPDATE OR DELETE ON budget_ledger_entry
FOR EACH ROW EXECUTE FUNCTION budget_ledger_entry_append_only()
"""


def upgrade() -> None:
    op.create_table(
        "budget_ledger_entry",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("run_kind", sa.Text(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("action_id", sa.Text(), nullable=False),
        sa.Column(
            "entry_kind",
            sa.Enum(
                "reserve", "commit", "release", name="entry_kind", native_enum=False, length=32
            ),
            nullable=False,
        ),
        sa.Column("amount_usd", sa.Numeric(precision=14, scale=6), nullable=False),
        sa.Column(
            "decision",
            sa.Enum(
                "run_automatically",
                "ask_a_person",
                "refuse",
                name="decision",
                native_enum=False,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("amount_usd >= 0", name=op.f("ck_budget_ledger_entry_amount_usd")),
        sa.CheckConstraint(
            "entry_kind IN ('reserve', 'commit', 'release')",
            name=op.f("ck_budget_ledger_entry_entry_kind"),
        ),
        sa.CheckConstraint(
            "decision IN ('run_automatically', 'ask_a_person', 'refuse')",
            name=op.f("ck_budget_ledger_entry_decision"),
        ),
        sa.CheckConstraint(
            "(entry_kind = 'reserve') = (decision IS NOT NULL)",
            name=op.f("ck_budget_ledger_entry_reserve_decision"),
        ),
        sa.CheckConstraint(
            "decision <> 'refuse'", name=op.f("ck_budget_ledger_entry_decision_held")
        ),
        sa.CheckConstraint(
            "entry_kind <> 'release' OR amount_usd = 0",
            name=op.f("ck_budget_ledger_entry_release_amount"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_budget_ledger_entry_workspace_id_workspace"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_budget_ledger_entry")),
    )
    op.create_index(
        "ix_budget_ledger_entry_workspace_id_created_at",
        "budget_ledger_entry",
        ["workspace_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "uq_budget_ledger_entry_reserve",
        "budget_ledger_entry",
        ["run_kind", "action_id"],
        unique=True,
        postgresql_where=sa.text("entry_kind = 'reserve'"),
    )
    op.create_index(
        "uq_budget_ledger_entry_settlement",
        "budget_ledger_entry",
        ["run_kind", "action_id"],
        unique=True,
        postgresql_where=sa.text("entry_kind IN ('commit', 'release')"),
    )
    op.execute(APPEND_ONLY_FUNCTION)
    op.execute(APPEND_ONLY_TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER budget_ledger_entry_append_only ON budget_ledger_entry")
    op.execute("DROP FUNCTION budget_ledger_entry_append_only()")
    op.drop_index("uq_budget_ledger_entry_settlement", table_name="budget_ledger_entry")
    op.drop_index("uq_budget_ledger_entry_reserve", table_name="budget_ledger_entry")
    op.drop_index(
        "ix_budget_ledger_entry_workspace_id_created_at", table_name="budget_ledger_entry"
    )
    op.drop_table("budget_ledger_entry")
