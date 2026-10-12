"""Settings of the worker process, read from the environment at the edge.

Only what the process itself needs is here: its database, its workflow server,
how much it runs at once, and the installation's ``ENCRYPTION_KEY``, the key
the credentials stored from the dashboard are sealed with, which every agent's
runs open theirs with. An installation without the key gets no cipher, and a
stored credential is then unreadable, which the run that needs it is told. A
key that is set and is not one stops the worker. Each agent reads its own settings in its folder,
with ``positive_number`` and ``SettingsError`` from here; this module imports
none of them. Model configuration is read by the model router, from the same
environment mapping, in the wiring of the agent that uses it. Credentials (the
model provider's key, the repository tokens) are not settings and are not read
at start-up at all: they are resolved for each attempt, from that mapping and
from what the run's workspace stored (``credentials.py``). No activity reads
the process's environment itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from openmarketer_core.credentials.cipher import CredentialCipher
from openmarketer_core.workflow_server import (
    DEFAULT_NAMESPACE,
    DEV_STACK_WORKFLOW_SERVER,
    WorkflowServer,
)

DEFAULT_CONCURRENT_ACTIVITIES = 4


class SettingsError(Exception):
    """The environment does not hold what the worker needs to start."""


@dataclass(frozen=True)
class Settings:
    database_url: str
    temporal_address: str = DEV_STACK_WORKFLOW_SERVER
    temporal_namespace: str = DEFAULT_NAMESPACE
    max_concurrent_activities: int = DEFAULT_CONCURRENT_ACTIVITIES
    # Opens the stored credentials; ``None`` when the installation has no ``ENCRYPTION_KEY``.
    credential_cipher: CredentialCipher | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Settings:
        """Raises ``SettingsError``, naming the variable, for what is missing or not a number.

        Raises ``EncryptionKeyInvalid`` for an ``ENCRYPTION_KEY`` that is set and is not 32
        bytes in base64; the message says what to set and does not repeat the key.
        """
        database_url = environ.get("DATABASE_URL")
        if not database_url:
            raise SettingsError("the worker needs DATABASE_URL (see .env.example)")
        workflow_server = WorkflowServer.from_environment(environ)
        encryption_key = environ.get("ENCRYPTION_KEY", "").strip()
        return cls(
            database_url=database_url,
            temporal_address=workflow_server.address,
            temporal_namespace=workflow_server.namespace,
            max_concurrent_activities=int(
                positive_number(
                    environ, "WORKER_MAX_CONCURRENT_ACTIVITIES", DEFAULT_CONCURRENT_ACTIVITIES
                )
            ),
            credential_cipher=CredentialCipher(encryption_key) if encryption_key else None,
        )


def positive_number(environ: Mapping[str, str], name: str, default: float) -> float:
    """The number in the variable ``name``, at least 1, or ``default`` when it is not set."""
    text = environ.get(name)
    if not text:
        return default
    try:
        number = float(text)
    except ValueError:
        number = 0
    if number < 1:
        raise SettingsError(f"{name} must be a number of at least 1, not {text!r}")
    return number
