"""Test that the profile version migration refuses an approved row nobody is named for.

It needs PostgreSQL (see the root ``conftest.py``) and its own database, which
it leaves at the first revision.
"""

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

BEFORE = "9a5347e23b5c"
APPROVED_WITHOUT_APPROVER = """
WITH workspace AS (
    INSERT INTO workspace (name) VALUES ('Acme') RETURNING id
), project AS (
    INSERT INTO project (workspace_id, name, source_repo_url)
    SELECT id, 'Example App', 'https://example.com/r.git' FROM workspace RETURNING id
)
INSERT INTO product_profile (project_id, version, content, status, approved_at)
SELECT id, 1, '{"product": {"name": "X"}}'::jsonb, 'approved', now() FROM project
"""


def test_upgrade_is_refused_while_an_approved_profile_has_no_approver(alembic_config):
    command.upgrade(alembic_config, BEFORE)
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    with engine.begin() as conn:
        conn.execute(text(APPROVED_WITHOUT_APPROVER))

    with pytest.raises(IntegrityError, match="ck_product_profile_approval"):
        command.upgrade(alembic_config, "head")

    with engine.connect() as conn:
        revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        stored = conn.execute(
            text("SELECT version, status, approved_by FROM product_profile")
        ).all()
    engine.dispose()
    assert revision == BEFORE
    assert [tuple(row) for row in stored] == [(1, "approved", None)]
