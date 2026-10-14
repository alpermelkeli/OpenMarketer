"""Workspace settings store: the choices a workspace made in the dashboard's settings.

A workspace has at most one value per name; storing again replaces it. A value
is a JSON object. Every function is scoped to a workspace, takes a session and
never commits: the caller owns the transaction. Callers get a frozen
``StoredSetting``, never the ORM row.

This module keeps JSON. It does not know which names exist or what a value
must hold, and it does not decide whether the environment or a stored choice
wins: that is ``workspace_settings/``.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from openmarketer_core.db.models import WorkspaceSettingRecord
from openmarketer_core.db.workspace_lock import wait_for_other_writes


@dataclass(frozen=True)
class StoredSetting:
    """A choice the workspace stored under ``name``: its value, who stored it and when."""

    name: str
    value: Mapping[str, Any]
    set_at: datetime
    set_by: uuid.UUID


def store_setting(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    name: str,
    value: Mapping[str, Any],
    set_by: uuid.UUID,
) -> StoredSetting:
    """Keep ``value`` as the workspace's choice for ``name``, in place of the one there.

    ``set_at`` is the database's time, also when a value is replaced.
    """
    wait_for_other_writes(session, workspace_id)
    stored = {"value": dict(value), "set_by": set_by}
    row = session.execute(
        insert(WorkspaceSettingRecord)
        .values(workspace_id=workspace_id, name=name, **stored)
        .on_conflict_do_update(
            index_elements=[WorkspaceSettingRecord.workspace_id, WorkspaceSettingRecord.name],
            set_={**stored, "set_at": func.now()},
        )
        .returning(
            WorkspaceSettingRecord.name,
            WorkspaceSettingRecord.value,
            WorkspaceSettingRecord.set_at,
            WorkspaceSettingRecord.set_by,
        )
    ).one()
    return StoredSetting(name=row.name, value=row.value, set_at=row.set_at, set_by=row.set_by)


def remove_setting(session: Session, *, workspace_id: uuid.UUID, name: str) -> bool:
    """Delete the workspace's choice for ``name``; whether there was one."""
    wait_for_other_writes(session, workspace_id)
    removed = session.execute(
        delete(WorkspaceSettingRecord)
        .where(
            WorkspaceSettingRecord.workspace_id == workspace_id,
            WorkspaceSettingRecord.name == name,
        )
        .returning(WorkspaceSettingRecord.name)
    ).all()
    return bool(removed)


def stored_settings(
    session: Session, *, workspace_id: uuid.UUID, prefix: str
) -> list[StoredSetting]:
    """The workspace's choices whose name starts with ``prefix``, by name, in one query."""
    rows = session.execute(
        select(
            WorkspaceSettingRecord.name,
            WorkspaceSettingRecord.value,
            WorkspaceSettingRecord.set_at,
            WorkspaceSettingRecord.set_by,
        )
        .where(
            WorkspaceSettingRecord.workspace_id == workspace_id,
            WorkspaceSettingRecord.name.startswith(prefix, autoescape=True),
        )
        .order_by(WorkspaceSettingRecord.name)
    )
    return [
        StoredSetting(name=row.name, value=row.value, set_at=row.set_at, set_by=row.set_by)
        for row in rows
    ]


def stored_setting(session: Session, *, workspace_id: uuid.UUID, name: str) -> StoredSetting | None:
    """The workspace's choice for exactly ``name``, or ``None``."""
    row = session.execute(
        select(
            WorkspaceSettingRecord.name,
            WorkspaceSettingRecord.value,
            WorkspaceSettingRecord.set_at,
            WorkspaceSettingRecord.set_by,
        ).where(
            WorkspaceSettingRecord.workspace_id == workspace_id,
            WorkspaceSettingRecord.name == name,
        )
    ).one_or_none()
    if row is None:
        return None
    return StoredSetting(name=row.name, value=row.value, set_at=row.set_at, set_by=row.set_by)
