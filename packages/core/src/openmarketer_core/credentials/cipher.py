"""Sealing a credential under the installation's ``ENCRYPTION_KEY``, and opening it again.

AES-256-GCM with a fresh random nonce for every value. The workspace and the
credential's name are the associated data, so a sealed value opens only as the
credential of the workspace it was sealed for: a row copied to another
workspace or under another name does not open. The key encrypts the values
directly; the design report names envelope encryption, a data key per row,
which this is not.

Neither the key nor a value appears in an error or in ``repr``. A value that
does not open is not told apart: a wrong key, a changed row and a row moved
elsewhere all read the same.

This is the only module that imports ``cryptography``. It does not read the
environment, store anything or decide which value is used; it does not rotate
a key either: values sealed under another key have to be set again.
"""

from __future__ import annotations

import base64
import binascii
import os
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from openmarketer_core.credentials.catalog import SealedValue

KEY_BYTES = 32
# 96 bits, the nonce size GCM is specified for.
NONCE_BYTES = 12
_HOW_TO_SET_THE_KEY = (
    f"set ENCRYPTION_KEY to {KEY_BYTES} random bytes in base64"
    f" (generate one with: openssl rand -base64 {KEY_BYTES})"
)


class EncryptionKeyInvalid(Exception):
    """``ENCRYPTION_KEY`` is missing or is not 32 bytes in base64; the message says what to set."""


class SealedValueDoesNotOpen(Exception):
    """The value was not sealed with this key for this credential of this workspace."""

    def __init__(self) -> None:
        super().__init__("the sealed value does not open with this key for this credential")


class CredentialCipher:
    """Seals and opens credential values with one installation's key."""

    def __init__(self, encryption_key: str) -> None:
        """``encryption_key`` is the text of ``ENCRYPTION_KEY``; ``EncryptionKeyInvalid`` if bad."""
        self._aead = AESGCM(_key_from(encryption_key))

    def seal(self, value: str, *, workspace_id: uuid.UUID, credential_name: str) -> SealedValue:
        """``value`` encrypted as that credential of that workspace, under a new nonce."""
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = self._aead.encrypt(
            nonce, value.encode("utf-8"), _binding(workspace_id, credential_name)
        )
        return SealedValue(nonce=nonce, ciphertext=ciphertext)

    def open(self, sealed: SealedValue, *, workspace_id: uuid.UUID, credential_name: str) -> str:
        """The value that was sealed; ``SealedValueDoesNotOpen`` for anything else."""
        try:
            value = self._aead.decrypt(
                sealed.nonce, sealed.ciphertext, _binding(workspace_id, credential_name)
            )
            return value.decode("utf-8")
        # A nonce of a length the cipher does not take is a ValueError, as is text that is
        # not UTF-8. The causes are dropped: none of them may carry a part of the row.
        except (InvalidTag, ValueError):
            raise SealedValueDoesNotOpen from None

    def __repr__(self) -> str:
        # Never the key: a repr ends up in logs and tracebacks.
        return "CredentialCipher(key=<hidden>)"


def _key_from(encryption_key: str) -> bytes:
    text = encryption_key.strip()
    if not text:
        raise EncryptionKeyInvalid(f"ENCRYPTION_KEY is not set: {_HOW_TO_SET_THE_KEY}")
    try:
        key = base64.b64decode(text, validate=True)
    # The cause is dropped so that no traceback shows the text of the key.
    except (binascii.Error, ValueError):
        raise EncryptionKeyInvalid(f"ENCRYPTION_KEY is not base64: {_HOW_TO_SET_THE_KEY}") from None
    if len(key) != KEY_BYTES:
        raise EncryptionKeyInvalid(
            f"ENCRYPTION_KEY is {len(key)} bytes, not {KEY_BYTES}: {_HOW_TO_SET_THE_KEY}"
        )
    return key


def _binding(workspace_id: uuid.UUID, credential_name: str) -> bytes:
    """What a sealed value is tied to. The identifier has a fixed length, so no two differ
    in where the name starts."""
    return b"openmarketer:credential:" + workspace_id.bytes + credential_name.encode("utf-8")
