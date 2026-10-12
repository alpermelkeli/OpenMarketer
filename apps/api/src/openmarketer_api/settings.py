"""Settings of the API process, read from the environment at the edge.

Only what the API itself needs is here: the database, the workflow server and
the key that credentials are stored under. The API reads no model configuration
and never the value of a credential, from the environment or from the database:
analyses run in the worker. ``ENCRYPTION_KEY`` is read to encrypt a credential
the user sets; without it nothing can be stored, and a key that is not one
stops the start. Whether the environment sets a credential is not a setting:
core's ``credential_statuses`` is handed the environment (``main.py``) and
looks only at whether each credential's variable is empty.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from openmarketer_core.credentials.cipher import CredentialCipher, EncryptionKeyInvalid
from openmarketer_core.workflow_server import WorkflowServer


class SettingsError(Exception):
    """The environment does not hold what the API needs to start."""


@dataclass(frozen=True)
class Settings:
    database_url: str
    workflow_server: WorkflowServer
    # None when the installation has no ENCRYPTION_KEY: credentials cannot be stored.
    credential_cipher: CredentialCipher | None

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Settings:
        database_url = environ.get("DATABASE_URL")
        if not database_url:
            raise SettingsError("the API needs DATABASE_URL (see .env.example)")
        return cls(
            database_url=database_url,
            workflow_server=WorkflowServer.from_environment(environ),
            credential_cipher=_credential_cipher(environ),
        )


def _credential_cipher(environ: Mapping[str, str]) -> CredentialCipher | None:
    encryption_key = environ.get("ENCRYPTION_KEY", "")
    if not encryption_key.strip():
        return None
    try:
        return CredentialCipher(encryption_key)
    except EncryptionKeyInvalid as invalid:
        # The message says what to set and holds nothing of the key.
        raise SettingsError(str(invalid)) from None
