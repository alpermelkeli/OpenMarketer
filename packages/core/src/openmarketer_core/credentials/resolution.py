"""Which value a credential has in a workspace, and setting or removing a stored one.

The environment wins. A credential whose environment variable is not empty has
that value, and a value stored for it is ignored; storing one is refused while
the variable is set, since it would have no effect. Removing a stored value is
always allowed. Without the variable, the stored value is the one in use, and
it needs the installation's ``ENCRYPTION_KEY`` to be read.

A status says where a credential's value comes from and never holds the value
or anything derived from it. ``source`` is the one in use; ``stored`` is filled
whenever a value is stored, also when the environment overrides it, so a caller
can say that a stored value is being ignored.

Every function receives what it needs: the session, the environment as a
mapping, the workspace and the cipher (``None`` when the installation has no
``ENCRYPTION_KEY``). None of them reads the process environment, builds a
cipher or commits. Whether a value is one its provider accepts is not checked.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.orm import Session

from openmarketer_core.credentials.catalog import (
    CREDENTIALS,
    Credential,
    acceptable_value,
    credential_named,
)
from openmarketer_core.credentials.cipher import CredentialCipher, SealedValueDoesNotOpen
from openmarketer_core.db.credentials import (
    StoredCredential,
    remove_stored_credential,
    sealed_credential,
    store_credential,
    stored_credentials,
)


class CredentialUnreadable(Exception):
    """A value is stored for the credential, but it cannot be decrypted here."""

    def __init__(self, credential: Credential) -> None:
        super().__init__(
            f"the stored value of {credential.name} cannot be read: set ENCRYPTION_KEY to the"
            " key it was stored with, or set the credential again"
        )
        self.credential = credential


class CredentialSetByEnvironment(Exception):
    """The environment sets the credential, so a stored value would never be used."""

    def __init__(self, credential: Credential) -> None:
        super().__init__(
            f"{credential.name} is set by the environment variable"
            f" {credential.environment_variable}; unset it to store a value instead"
        )
        self.credential = credential


class EncryptionKeyMissing(Exception):
    """The installation has no ``ENCRYPTION_KEY``, so no credential can be stored."""

    def __init__(self) -> None:
        super().__init__(
            "ENCRYPTION_KEY is needed to store credentials: set it to 32 random bytes in"
            " base64 (generate one with: openssl rand -base64 32)"
        )


class CredentialSource(StrEnum):
    """Where the value in use comes from."""

    ENVIRONMENT = "environment"
    STORED = "stored"
    NOT_SET = "not_set"


@dataclass(frozen=True)
class CredentialStatus:
    """Where one credential's value comes from in a workspace; never the value.

    ``stored`` is who stored a value and when. With ``source`` ``ENVIRONMENT``
    it is a value the environment overrides.
    """

    credential: Credential
    source: CredentialSource
    stored: StoredCredential | None


def credential_value(
    session: Session,
    environ: Mapping[str, str],
    *,
    workspace_id: uuid.UUID,
    credential: Credential,
    cipher: CredentialCipher | None,
) -> str | None:
    """The value to use for the credential in the workspace, or ``None`` when it has none.

    The environment's value if there is one, otherwise the stored one. Raises
    ``CredentialUnreadable`` when a value is stored and there is no cipher, or
    the cipher does not open it.
    """
    from_environment = _value_in_environment(credential, environ)
    if from_environment is not None:
        return from_environment
    sealed = sealed_credential(session, workspace_id=workspace_id, name=credential.name)
    if sealed is None:
        return None
    if cipher is None:
        raise CredentialUnreadable(credential)
    try:
        return cipher.open(sealed, workspace_id=workspace_id, credential_name=credential.name)
    except SealedValueDoesNotOpen:
        # Without the cause: the message already says all a caller may know.
        raise CredentialUnreadable(credential) from None


def credential_statuses(
    session: Session, environ: Mapping[str, str], *, workspace_id: uuid.UUID
) -> tuple[CredentialStatus, ...]:
    """The status of every credential of the catalog in the workspace, in the catalog's order.

    Nothing is decrypted, so this needs no cipher and does not tell whether a
    stored value can still be read.
    """
    names = [credential.name for credential in CREDENTIALS]
    stored = {
        row.name: row for row in stored_credentials(session, workspace_id=workspace_id, names=names)
    }
    return tuple(
        _status(credential, environ, stored.get(credential.name)) for credential in CREDENTIALS
    )


def set_credential(
    session: Session,
    environ: Mapping[str, str],
    *,
    workspace_id: uuid.UUID,
    name: str,
    value: str,
    set_by: uuid.UUID,
    cipher: CredentialCipher | None,
) -> CredentialStatus:
    """Store ``value``, encrypted, as the workspace's value of the credential named ``name``.

    It replaces a value stored before. Raises ``UnknownCredential``;
    ``CredentialSetByEnvironment``; ``CredentialValueRefused`` when the value
    is not one a credential can have; and ``EncryptionKeyMissing`` when there
    is no cipher.
    """
    credential = credential_named(name)
    if _value_in_environment(credential, environ) is not None:
        raise CredentialSetByEnvironment(credential)
    accepted = acceptable_value(value)
    if cipher is None:
        raise EncryptionKeyMissing
    sealed = cipher.seal(accepted, workspace_id=workspace_id, credential_name=credential.name)
    stored = store_credential(
        session, workspace_id=workspace_id, name=credential.name, sealed=sealed, set_by=set_by
    )
    return CredentialStatus(credential=credential, source=CredentialSource.STORED, stored=stored)


def remove_credential(session: Session, *, workspace_id: uuid.UUID, name: str) -> bool:
    """Remove the workspace's stored value of the credential; whether there was one.

    The environment's value, if any, is not touched. Raises ``UnknownCredential``.
    """
    credential = credential_named(name)
    return remove_stored_credential(session, workspace_id=workspace_id, name=credential.name)


def _value_in_environment(credential: Credential, environ: Mapping[str, str]) -> str | None:
    """The environment's value of the credential; an empty variable sets nothing."""
    return environ.get(credential.environment_variable) or None


def _status(
    credential: Credential, environ: Mapping[str, str], stored: StoredCredential | None
) -> CredentialStatus:
    if _value_in_environment(credential, environ) is not None:
        source = CredentialSource.ENVIRONMENT
    elif stored is not None:
        source = CredentialSource.STORED
    else:
        source = CredentialSource.NOT_SET
    return CredentialStatus(credential=credential, source=source, stored=stored)
