"""make approved profile versions immutable

Adds ``edited_from_id``, requires an approver as well as an approval time on an
approved row, and installs the trigger that keeps versions from being rewritten.
Autogenerate wrote only the column and its foreign key; the CHECK constraint and
the trigger are written by hand.

The upgrade is refused, on purpose, by a database that holds an approved row
without ``approved_by`` (the first revision allowed one, though no code wrote
it): nobody can say who approved it, so the migration does not guess. Give the
row its approver, or return it to a draft (``status = 'draft'``,
``approved_at = NULL``), and upgrade again.

The downgrade removes the trigger and ``edited_from_id``. While a database is
downgraded nothing protects its versions, and a later upgrade cannot tell
whether approved content was rewritten in between.

Revision ID: ebeeaa3e3f08
Revises: 9a5347e23b5c
Create Date: 2026-10-08 12:58:05.779517
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ebeeaa3e3f08"
down_revision: str | None = "9a5347e23b5c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Raised as an integrity error so callers see the same class of failure as for a
# CHECK constraint. TRUNCATE and DROP TABLE do not fire row triggers.
#
# The update rule compares whole rows with the approval columns removed, so a
# column added to the table later is frozen unless it is added to that list.
GUARD_FUNCTION = """
CREATE FUNCTION product_profile_guard_version() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    approval_columns CONSTANT text[] := ARRAY['status', 'approved_by', 'approved_at'];
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' THEN
            RAISE EXCEPTION 'a product profile version is stored as a draft and approved afterwards'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'product profile version % cannot be deleted', OLD.version
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status = 'approved' THEN
        RAISE EXCEPTION 'approved product profile version % cannot be changed', OLD.version
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (to_jsonb(NEW) - approval_columns) IS DISTINCT FROM (to_jsonb(OLD) - approval_columns) THEN
        RAISE EXCEPTION 'product profile version % can only be approved, not changed', OLD.version
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$
"""

GUARD_TRIGGER = """
CREATE TRIGGER product_profile_guard_version
BEFORE INSERT OR UPDATE OR DELETE ON product_profile
FOR EACH ROW EXECUTE FUNCTION product_profile_guard_version()
"""


def upgrade() -> None:
    op.add_column("product_profile", sa.Column("edited_from_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_product_profile_edited_from_id_product_profile"),
        "product_profile",
        "product_profile",
        ["edited_from_id"],
        ["id"],
    )
    op.drop_constraint(op.f("ck_product_profile_approved_at"), "product_profile", type_="check")
    op.create_check_constraint(
        op.f("ck_product_profile_approval"),
        "product_profile",
        "(status = 'approved') = (approved_by IS NOT NULL)"
        " AND (status = 'approved') = (approved_at IS NOT NULL)",
    )
    op.execute(GUARD_FUNCTION)
    op.execute(GUARD_TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER product_profile_guard_version ON product_profile")
    op.execute("DROP FUNCTION product_profile_guard_version()")
    op.drop_constraint(op.f("ck_product_profile_approval"), "product_profile", type_="check")
    op.create_check_constraint(
        op.f("ck_product_profile_approved_at"),
        "product_profile",
        "(status = 'approved') = (approved_at IS NOT NULL)",
    )
    op.drop_constraint(
        op.f("fk_product_profile_edited_from_id_product_profile"),
        "product_profile",
        type_="foreignkey",
    )
    op.drop_column("product_profile", "edited_from_id")
