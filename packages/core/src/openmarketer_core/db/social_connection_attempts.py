"""Connection attempt store: the connections a person started that a platform has not answered.

An attempt is stored when a person is sent to the platform, and taken, which deletes it,
when the platform's answer comes back: it is used once. It lasts until ``expires_at``, a
time of the database's clock; storing an attempt removes the workspace's expired ones,
which a person started and never finished. Every function is scoped to a workspace, takes a
session and never commits: the caller owns the transaction. Callers get a frozen
``TakenAttempt``, never the ORM row.

A taken attempt is gone only if the caller's transaction commits: a caller that raises after
taking one, and so rolls back, leaves it to be answered again until it expires.

This module keeps bytes. It does not encrypt or decrypt, does not talk to a platform and does
not check that the project belongs to the workspace before storing: the use case reads the
project first (``social_accounts/connection.py``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, func, insert
from sqlalchemy.orm import Session

from openmarketer_core.credentials.catalog import SealedValue
from openmarketer_core.db.models import SocialConnectionAttemptRecord
from openmarketer_core.db.workspace_lock import wait_for_other_writes
from openmarketer_core.social_accounts.catalog import SocialPlatform


@dataclass(frozen=True)
class TakenAttempt:
    """An attempt the platform answered: its project, its platform, the secret kept for the
    answer, still encrypted, and whether it had expired when it was taken."""

    project_id: uuid.UUID
    platform: SocialPlatform
    secret: SealedValue
    expired: bool


def store_connection_attempt(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    platform: SocialPlatform,
    state: str,
    secret: SealedValue,
    lifetime: timedelta,
) -> None:
    """Keep an attempt named ``state`` until ``lifetime`` from now, and drop the expired ones.

    A ``state`` that is already stored is an ``IntegrityError``: a platform names each
    attempt anew.
    """
    wait_for_other_writes(session, workspace_id)
    session.execute(
        delete(SocialConnectionAttemptRecord).where(
            SocialConnectionAttemptRecord.workspace_id == workspace_id,
            SocialConnectionAttemptRecord.expires_at <= func.now(),
        )
    )
    session.execute(
        insert(SocialConnectionAttemptRecord).values(
            state=state,
            workspace_id=workspace_id,
            project_id=project_id,
            platform=platform.value,
            secret_nonce=secret.nonce,
            secret_ciphertext=secret.ciphertext,
            expires_at=func.now() + lifetime,
        )
    )


def take_connection_attempt(
    session: Session, *, workspace_id: uuid.UUID, platform: SocialPlatform, state: str
) -> TakenAttempt | None:
    """Delete the workspace's attempt on ``platform`` named ``state`` and return it, or ``None``
    if there is none.

    An attempt started on another platform is not taken: an answer is finished by the platform
    it was started on, or not at all. An expired attempt is taken as well, and says so. Of two
    callers that take the same attempt, the second waits for the first and finds nothing.
    """
    wait_for_other_writes(session, workspace_id)
    row = session.execute(
        delete(SocialConnectionAttemptRecord)
        .where(
            SocialConnectionAttemptRecord.workspace_id == workspace_id,
            SocialConnectionAttemptRecord.platform == platform.value,
            SocialConnectionAttemptRecord.state == state,
        )
        .returning(
            SocialConnectionAttemptRecord.project_id,
            SocialConnectionAttemptRecord.platform,
            SocialConnectionAttemptRecord.secret_nonce,
            SocialConnectionAttemptRecord.secret_ciphertext,
            (SocialConnectionAttemptRecord.expires_at <= func.now()).label("expired"),
        )
    ).one_or_none()
    if row is None:
        return None
    return TakenAttempt(
        project_id=row.project_id,
        platform=SocialPlatform(row.platform),
        secret=SealedValue(nonce=row.secret_nonce, ciphertext=row.secret_ciphertext),
        expired=row.expired,
    )
