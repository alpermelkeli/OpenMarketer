"""let a deleted project take its profile versions with it

Rewrites ``product_profile_guard_version`` so that a version can be deleted in
one case only: the transaction is deleting the version's project and said so
by setting ``openmarketer.deleted_project`` to the project's id, for that
transaction only (``db/project_removal.py``). Every other deletion is refused
as before, and inserts and updates are guarded exactly as before. Written by
hand: autogenerate does not see functions.

The setting is a deliberate step, not a secret: any session can set it, as
any session that can change this function can drop it. What it prevents is
what the trigger always prevented, a version deleted by mistake, on its own,
while its project stays.

The downgrade restores the function as the previous revision wrote it, so
versions cannot be deleted at all again.

Revision ID: 3c8d1e5f7a92
Revises: bbcf991fe9b4
Create Date: 2026-10-14 15:12:40.118204
"""

from collections.abc import Sequence

from alembic import op

revision: str = "3c8d1e5f7a92"
down_revision: str | None = "bbcf991fe9b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ``current_setting(..., true)`` is NULL when the setting was never made in the session, and
# an empty string after the transaction that made it ended; neither equals a project id.
GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION product_profile_guard_version() RETURNS trigger
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
        IF current_setting('openmarketer.deleted_project', true) = OLD.project_id::text THEN
            RETURN OLD;
        END IF;
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

PREVIOUS_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION product_profile_guard_version() RETURNS trigger
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


def upgrade() -> None:
    op.execute(GUARD_FUNCTION)


def downgrade() -> None:
    op.execute(PREVIOUS_GUARD_FUNCTION)
