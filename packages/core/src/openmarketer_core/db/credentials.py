"""Credential store: the encrypted secrets a workspace set from the dashboard.

A workspace has at most one stored value per credential name; storing again
replaces it. Every function is scoped to a workspace, takes a session and
never commits: the caller owns the transaction. Callers get a frozen
``StoredCredential`` or ``SealedValue``, never the ORM row.

What a caller can learn without asking for the secret is who set a value and
when: ``stored_credentials`` never selects the nonce or the ciphertext. Only
``sealed_credential`` reads them, and what it returns is still encrypted.

This module keeps bytes. It does not encrypt or decrypt, does not know which
names exist (the caller passes the ones it asks about, so a row under a name
the code no longer knows is never read), and does not decide whether the
environment or the stored value is the one in use: that is
``credentials/resolution.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from openmarketer_core.credentials.catalog import SealedValue
from openmarketer_core.db.models import CredentialRecord
from openmarketer_core.db.workspace_lock import wait_for_other_writes


@dataclass(frozen=True)
class StoredCredential:
    """That a workspace has a stored value under ``name``, set by whom and when; not the value."""

    name: str
    set_at: datetime
    set_by: uuid.UUID


def store_credential(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    name: str,
    sealed: SealedValue,
    set_by: uuid.UUID,
) -> StoredCredential:
    """Keep ``sealed`` as the workspace's value for ``name``, in place of the one there.

    ``set_at`` is the database's time, also when a value is replaced.
    """
    wait_for_other_writes(session, workspace_id)
    value = {"nonce": sealed.nonce, "ciphertext": sealed.ciphertext, "set_by": set_by}
    row = session.execute(
        insert(CredentialRecord)
        .values(workspace_id=workspace_id, name=name, **value)
        .on_conflict_do_update(
            index_elements=[CredentialRecord.workspace_id, CredentialRecord.name],
            set_={**value, "set_at": func.now()},
        )
        .returning(CredentialRecord.name, CredentialRecord.set_at, CredentialRecord.set_by)
    ).one()
    return StoredCredential(name=row.name, set_at=row.set_at, set_by=row.set_by)


def remove_stored_credential(session: Session, *, workspace_id: uuid.UUID, name: str) -> bool:
    """Delete the workspace's stored value for ``name``; whether there was one."""
    wait_for_other_writes(session, workspace_id)
    removed = session.execute(
        delete(CredentialRecord)
        .where(CredentialRecord.workspace_id == workspace_id, CredentialRecord.name == name)
        .returning(CredentialRecord.name)
    ).all()
    return bool(removed)


def stored_credentials(
    session: Session, *, workspace_id: uuid.UUID, names: Collection[str]
) -> list[StoredCredential]:
    """Which of ``names`` the workspace has a stored value for, by name, in one query.

    At most one row per name asked for. Neither the nonce nor the ciphertext is selected.
    """
    rows = session.execute(
        select(CredentialRecord.name, CredentialRecord.set_at, CredentialRecord.set_by)
        .where(CredentialRecord.workspace_id == workspace_id, CredentialRecord.name.in_(names))
        .order_by(CredentialRecord.name)
    )
    return [StoredCredential(name=row.name, set_at=row.set_at, set_by=row.set_by) for row in rows]


def sealed_credential(
    session: Session, *, workspace_id: uuid.UUID, name: str
) -> SealedValue | None:
    """The workspace's stored value for ``name``, still encrypted, or ``None``."""
    row = session.execute(
        select(CredentialRecord.nonce, CredentialRecord.ciphertext).where(
            CredentialRecord.workspace_id == workspace_id, CredentialRecord.name == name
        )
    ).one_or_none()
    if row is None:
        return None
    return SealedValue(nonce=row.nonce, ciphertext=row.ciphertext)
