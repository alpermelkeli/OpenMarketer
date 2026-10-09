"""Settings of the worker process, read from the environment at the edge.

Only what the process itself needs is here: its database, its workflow server
and how much it runs at once. Each agent reads its own settings in its folder,
with ``positive_number`` and ``SettingsError`` from here; this module imports
none of them. Model configuration is read by the model router and repository
tokens by ``RepositoryTokens``, from the same environment mapping, in the
wiring of the agent that uses them; activities read none of it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

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

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Settings:
        database_url = environ.get("DATABASE_URL")
        if not database_url:
            raise SettingsError("the worker needs DATABASE_URL (see .env.example)")
        workflow_server = WorkflowServer.from_environment(environ)
        return cls(
            database_url=database_url,
            temporal_address=workflow_server.address,
            temporal_namespace=workflow_server.namespace,
            max_concurrent_activities=int(
                positive_number(
                    environ, "WORKER_MAX_CONCURRENT_ACTIVITIES", DEFAULT_CONCURRENT_ACTIVITIES
                )
            ),
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
