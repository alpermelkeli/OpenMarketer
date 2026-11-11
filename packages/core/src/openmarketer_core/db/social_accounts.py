"""Social account store: a project's connected accounts and their sealed tokens.

A project has at most one account per platform; storing an account replaces the one the
project had on that platform, whether it is the same account authorized again or another.
Every function is scoped to a workspace, takes a session and never commits: the caller owns
the transaction. Callers get a frozen ``ConnectedAccount`` or ``SealedAccountTokens``, never
the ORM row.

What a caller can learn without asking for the tokens is which account is connected, by
whom and when: ``connected_accounts`` and ``connected_account`` never select a nonce or a
ciphertext. Only ``sealed_account_tokens`` reads them, and what it returns is still
encrypted.

This module keeps bytes. It does not encrypt or decrypt, does not talk to a platform and
does not check that the project belongs to the workspace before storing: the use case reads
the project first (``social_accounts/connection.py``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import case, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from openmarketer_core.credentials.catalog import SealedValue
from openmarketer_core.db.models import SocialAccountRecord
from openmarketer_core.db.workspace_lock import wait_for_other_writes
from openmarketer_core.social_accounts.catalog import AccountDescription, SocialPlatform

# Every column a caller may see; the sealed tokens are not among them.
_SHOWN = (
    SocialAccountRecord.id,
    SocialAccountRecord.project_id,
    SocialAccountRecord.platform,
    SocialAccountRecord.account_id,
    SocialAccountRecord.username,
    SocialAccountRecord.display_name,
    SocialAccountRecord.picture_url,
    SocialAccountRecord.connected_by,
    SocialAccountRecord.connected_at,
    SocialAccountRecord.updated_at,
)


@dataclass(frozen=True)
class ConnectedAccount:
    """A project's account on a platform, as the platform described it; never its tokens.

    ``account_id`` is the platform's identifier of the account. ``connected_at`` is when
    this account was connected to the project, ``updated_at`` when it was last authorized.
    """

    id: uuid.UUID
    project_id: uuid.UUID
    platform: SocialPlatform
    account_id: str
    username: str
    display_name: str
    picture_url: str | None
    connected_by: uuid.UUID
    connected_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class SealedAccountTokens:
    """An account's access token and its secret, both still encrypted."""

    access_token: SealedValue
    access_secret: SealedValue


def store_connected_account(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    platform: SocialPlatform,
    account: AccountDescription,
    tokens: SealedAccountTokens,
    connected_by: uuid.UUID,
) -> ConnectedAccount:
    """Keep ``account`` as the project's account on ``platform``, in place of the one there.

    The same account authorized again keeps its ``connected_at``; another account starts a
    new one. Both times are the database's.
    """
    wait_for_other_writes(session, workspace_id)
    values = {
        "account_id": account.account_id,
        "username": account.username,
        "display_name": account.display_name,
        "picture_url": account.picture_url,
        "access_token_nonce": tokens.access_token.nonce,
        "access_token_ciphertext": tokens.access_token.ciphertext,
        "access_secret_nonce": tokens.access_secret.nonce,
        "access_secret_ciphertext": tokens.access_secret.ciphertext,
        "connected_by": connected_by,
    }
    statement = insert(SocialAccountRecord).values(
        workspace_id=workspace_id, project_id=project_id, platform=platform.value, **values
    )
    same_account = SocialAccountRecord.account_id == statement.excluded.account_id
    row = session.execute(
        statement.on_conflict_do_update(
            index_elements=[SocialAccountRecord.project_id, SocialAccountRecord.platform],
            set_={
                **values,
                "connected_at": case(
                    (same_account, SocialAccountRecord.connected_at), else_=func.now()
                ),
                "updated_at": func.now(),
            },
            # A project of another workspace is never written over.
            where=SocialAccountRecord.workspace_id == workspace_id,
        ).returning(*_SHOWN)
    ).one()
    return _connected(row)


def connected_accounts(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> list[ConnectedAccount]:
    """The project's connected accounts, by platform, in one query.

    The list needs no limit: a project has at most one account per platform. Neither the
    nonces nor the ciphertexts are selected.
    """
    rows = session.execute(
        select(*_SHOWN)
        .where(
            SocialAccountRecord.workspace_id == workspace_id,
            SocialAccountRecord.project_id == project_id,
        )
        .order_by(SocialAccountRecord.platform)
    )
    return [_connected(row) for row in rows]


def connected_account(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, platform: SocialPlatform
) -> ConnectedAccount | None:
    """The project's account on ``platform``, or ``None``; its tokens are not selected."""
    row = session.execute(
        select(*_SHOWN).where(
            SocialAccountRecord.workspace_id == workspace_id,
            SocialAccountRecord.project_id == project_id,
            SocialAccountRecord.platform == platform.value,
        )
    ).one_or_none()
    return None if row is None else _connected(row)


def sealed_account_tokens(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, platform: SocialPlatform
) -> SealedAccountTokens | None:
    """The tokens of the project's account on ``platform``, still encrypted, or ``None``."""
    row = session.execute(
        select(
            SocialAccountRecord.access_token_nonce,
            SocialAccountRecord.access_token_ciphertext,
            SocialAccountRecord.access_secret_nonce,
            SocialAccountRecord.access_secret_ciphertext,
        ).where(
            SocialAccountRecord.workspace_id == workspace_id,
            SocialAccountRecord.project_id == project_id,
            SocialAccountRecord.platform == platform.value,
        )
    ).one_or_none()
    if row is None:
        return None
    return SealedAccountTokens(
        access_token=SealedValue(
            nonce=row.access_token_nonce, ciphertext=row.access_token_ciphertext
        ),
        access_secret=SealedValue(
            nonce=row.access_secret_nonce, ciphertext=row.access_secret_ciphertext
        ),
    )


def remove_connected_account(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, platform: SocialPlatform
) -> bool:
    """Delete the project's account on ``platform`` with its tokens; whether there was one."""
    wait_for_other_writes(session, workspace_id)
    removed = session.execute(
        delete(SocialAccountRecord)
        .where(
            SocialAccountRecord.workspace_id == workspace_id,
            SocialAccountRecord.project_id == project_id,
            SocialAccountRecord.platform == platform.value,
        )
        .returning(SocialAccountRecord.id)
    ).all()
    return bool(removed)


def _connected(row: Any) -> ConnectedAccount:
    return ConnectedAccount(
        id=row.id,
        project_id=row.project_id,
        platform=SocialPlatform(row.platform),
        account_id=row.account_id,
        username=row.username,
        display_name=row.display_name,
        picture_url=row.picture_url,
        connected_by=row.connected_by,
        connected_at=row.connected_at,
        updated_at=row.updated_at,
    )
