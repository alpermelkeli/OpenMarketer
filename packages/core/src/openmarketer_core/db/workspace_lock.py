"""The workspace lock: how writes in one workspace are made to run one after another.

Creating a project, storing an analysis, saving an edit and approving a version
all take it first. It is a row lock on the workspace, released when the
transaction ends; reads never take it.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from openmarketer_core.db.models import Workspace


def wait_for_other_writes(session: Session, workspace_id: uuid.UUID) -> None:
    """Hold the workspace row until the transaction ends.

    Writes in one workspace then run one after another, so two of them cannot
    create the same project, take the same version number or approve the same
    version. This relies on READ COMMITTED, PostgreSQL's default: under a
    stricter isolation level the writer that waited still sees the old rows and
    fails on the unique constraint instead of taking the next number.
    """
    session.execute(select(Workspace.id).where(Workspace.id == workspace_id).with_for_update())
