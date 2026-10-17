"""The installation's credentials as an attempt of any agent gets them, for its workspace.

A run needs the model provider's key, and some agents need more (the tokens of the
repository hosts, for an analysis). Each can be set in the environment or stored for a
workspace from the dashboard, after the worker started too. So none is read when the worker
starts: ``WorkspaceCredentials`` resolves them when an attempt asks, inside the activity that
uses them, and keeps none.

Which value a credential has is core's rule (``credentials/resolution.py``): the
environment's, otherwise the one stored for the workspace, opened with the installation's
``ENCRYPTION_KEY``. The model provider's key is the catalog's credential
(``OPENROUTER_API_KEY``, then the stored value) when the variable the model configuration
names for its key (``provider.api_key_env``) is the catalog's. A configuration that names
another variable is another provider's: its key comes from that variable alone, so the key of
one provider is never sent to another.

What cannot be resolved is said in an error whose message may be shown and never holds a
value: core's ``CredentialUnreadable`` for a stored value that does not open, and core's
``ModelKeyNotSet`` when the provider's key is set nowhere. Neither passes by itself, so
another attempt is not worth its cost. A failure of the database is not caught. This module
words no agent's failure and imports no agent: each agent turns these errors into the
failure of its own runs.

A value is returned to the caller and to nobody else: it is not logged, not put in an
error, and never part of what an activity returns. Nothing here reads the process's
environment; the mapping is handed in when the worker starts. It is for activities: the
workflow sandbox does not load it.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence

from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.credentials.catalog import Credential
from openmarketer_core.credentials.cipher import CredentialCipher
from openmarketer_core.credentials.resolution import credential_value
from openmarketer_core.credentials.resolution import model_key as resolve_model_key
from openmarketer_core.db.session import transaction
from openmarketer_core.llm_config import ModelRouter


class WorkspaceCredentials:
    """The installation's credentials, resolved for a workspace each time they are asked for."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        environ: Mapping[str, str],
        cipher: CredentialCipher | None,
    ) -> None:
        """``cipher`` is ``None`` for an installation without ``ENCRYPTION_KEY``."""
        self._sessions = sessions
        self._environ = environ
        self._cipher = cipher

    def values(
        self, workspace_id: uuid.UUID, credentials: Sequence[Credential]
    ) -> dict[Credential, str | None]:
        """The value of each credential in the workspace, ``None`` for one that has none.

        Read in one transaction. Raises ``CredentialUnreadable`` for a stored value that
        cannot be opened, and ``DatabaseError``.
        """
        with transaction(self._sessions) as session:
            return {
                credential: credential_value(
                    session,
                    self._environ,
                    workspace_id=workspace_id,
                    credential=credential,
                    cipher=self._cipher,
                )
                for credential in credentials
            }

    def model_key(self, workspace_id: uuid.UUID, router: ModelRouter) -> str:
        """The key for the calls ``router`` builds for a run of the workspace.

        Core's rule (``credentials/resolution.py``), read in one transaction. Raises
        ``ModelKeyNotSet`` when there is none, ``CredentialUnreadable`` and ``DatabaseError``.
        """
        with transaction(self._sessions) as session:
            return resolve_model_key(
                session,
                self._environ,
                workspace_id=workspace_id,
                router=router,
                cipher=self._cipher,
            )

    def __repr__(self) -> str:
        # Never the environment: a repr ends up in logs and tracebacks.
        return "WorkspaceCredentials(<hidden>)"
