"""The repository tokens a clone of an analysis may carry, for the workspace of its run.

The one credential that is the analyzer's alone. The values are resolved by the worker's
``WorkspaceCredentials`` (the environment's, otherwise the workspace's stored one) each
time a clone is made, and handed to core's ``RepositoryTokens.from_environment`` under the
names the environment has them, so which host a token goes to stays core's rule
(``repository_analysis/intake/credentials.py``) and is not repeated here. ``GITLAB_HOST``
is not a credential: it is the environment's only, read when the worker starts.

This module resolves no other credential and words no failure: what cannot be resolved is
raised as ``WorkspaceCredentials`` raises it, and ``analysis.py`` ends the analysis with it.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from openmarketer_core.credentials.catalog import GITHUB_TOKEN, GITLAB_TOKEN
from openmarketer_core.repository_analysis.intake import RepositoryTokens
from openmarketer_worker.credentials import WorkspaceCredentials

GITLAB_HOST_VARIABLE = "GITLAB_HOST"


class WorkspaceRepositoryTokens:
    """The tokens of the repository hosts, resolved for a workspace each time they are asked for."""

    def __init__(self, credentials: WorkspaceCredentials, environ: Mapping[str, str]) -> None:
        """Raises ``IntakeError`` for a ``GITLAB_HOST`` that is not a host name, so the mistake
        stops the worker and not every clone."""
        self._credentials = credentials
        self._gitlab_host = environ.get(GITLAB_HOST_VARIABLE, "")
        RepositoryTokens.from_environment({GITLAB_HOST_VARIABLE: self._gitlab_host})

    def of(self, workspace_id: uuid.UUID) -> RepositoryTokens:
        """The tokens a clone for a run of the workspace may carry, each bound to its host.

        Raises ``CredentialUnreadable`` when a stored token cannot be read, whichever host
        the repository is on: a clone without the token its owner set would fail in a way
        that says less. Raises ``DatabaseError``.
        """
        resolved = self._credentials.values(workspace_id, [GITHUB_TOKEN, GITLAB_TOKEN])
        return RepositoryTokens.from_environment(
            {
                **{
                    credential.environment_variable: value or ""
                    for credential, value in resolved.items()
                },
                GITLAB_HOST_VARIABLE: self._gitlab_host,
            }
        )
