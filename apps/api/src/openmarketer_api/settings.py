"""Settings of the API process, read from the environment at the edge.

Only what the API itself needs is here: the database and the workflow server.
The API reads no model configuration and no repository token; analyses run in
the worker.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from openmarketer_core.workflow_server import WorkflowServer


class SettingsError(Exception):
    """The environment does not hold what the API needs to start."""


@dataclass(frozen=True)
class Settings:
    database_url: str
    workflow_server: WorkflowServer

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Settings:
        database_url = environ.get("DATABASE_URL")
        if not database_url:
            raise SettingsError("the API needs DATABASE_URL (see .env.example)")
        return cls(
            database_url=database_url,
            workflow_server=WorkflowServer.from_environment(environ),
        )
