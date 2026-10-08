"""Settings of the worker process, read from the environment at the edge.

Only what the worker itself needs is here. Model configuration is read by the
model router and repository tokens by ``RepositoryTokens``, both from the same
environment mapping in ``main.py``; activities read none of it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from openmarketer_worker.policy import AnalysisPolicy

# The dev stack's Temporal as seen from the host. ``.env.example`` names the
# compose-network host instead, which only resolves inside that network.
DEV_STACK_TEMPORAL_ADDRESS = "localhost:7233"
DEFAULT_NAMESPACE = "default"
DEFAULT_CONCURRENT_ACTIVITIES = 4


class SettingsError(Exception):
    """The environment does not hold what the worker needs to start."""


@dataclass(frozen=True)
class Settings:
    database_url: str
    temporal_address: str = DEV_STACK_TEMPORAL_ADDRESS
    temporal_namespace: str = DEFAULT_NAMESPACE
    analysis_policy: AnalysisPolicy = field(default_factory=AnalysisPolicy)
    max_concurrent_activities: int = DEFAULT_CONCURRENT_ACTIVITIES

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Settings:
        database_url = environ.get("DATABASE_URL")
        if not database_url:
            raise SettingsError("the worker needs DATABASE_URL (see .env.example)")
        defaults = AnalysisPolicy()
        return cls(
            database_url=database_url,
            temporal_address=environ.get("TEMPORAL_ADDRESS") or DEV_STACK_TEMPORAL_ADDRESS,
            temporal_namespace=environ.get("TEMPORAL_NAMESPACE") or DEFAULT_NAMESPACE,
            analysis_policy=AnalysisPolicy(
                attempt_timeout_seconds=60
                * _positive(
                    environ, "ANALYSIS_TIMEOUT_MINUTES", defaults.attempt_timeout_seconds / 60
                ),
                heartbeat_timeout_seconds=defaults.heartbeat_timeout_seconds,
                max_attempts=int(
                    _positive(environ, "ANALYSIS_MAX_ATTEMPTS", defaults.max_attempts)
                ),
                retry_after_seconds=defaults.retry_after_seconds,
            ),
            max_concurrent_activities=int(
                _positive(
                    environ, "WORKER_MAX_CONCURRENT_ACTIVITIES", DEFAULT_CONCURRENT_ACTIVITIES
                )
            ),
        )


def _positive(environ: Mapping[str, str], name: str, default: float) -> float:
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
