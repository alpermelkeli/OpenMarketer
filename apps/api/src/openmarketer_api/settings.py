"""Settings of the API process, read from the environment at the edge.

Only what the API itself needs is here. Model configuration is read by the
model router; nothing else in this package reads the environment.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


class SettingsError(Exception):
    """The environment does not hold what the API needs to start."""


@dataclass(frozen=True)
class Settings:
    database_url: str

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Settings:
        database_url = environ.get("DATABASE_URL")
        if not database_url:
            raise SettingsError("the API needs DATABASE_URL (see .env.example)")
        return cls(database_url=database_url)
