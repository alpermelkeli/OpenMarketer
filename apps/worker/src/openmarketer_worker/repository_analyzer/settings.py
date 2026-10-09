"""Settings of the repository analyzer's workflow, read from the environment at the edge.

What an operator can change about an analysis: how long one attempt may take
and how many attempts a run gets. The other limits and every default, with
their reasons, are in ``policy.py``; this module only fills that policy. It
takes the environment as an argument and is called once, by the worker's
``main.py``; the workflow and the activities read none of it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from openmarketer_worker.repository_analyzer.policy import AnalysisPolicy
from openmarketer_worker.settings import positive_number


@dataclass(frozen=True)
class RepositoryAnalyzerSettings:
    policy: AnalysisPolicy = field(default_factory=AnalysisPolicy)

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> RepositoryAnalyzerSettings:
        """Raises ``SettingsError``, naming the variable, for a value that is not a number >= 1."""
        defaults = AnalysisPolicy()
        return cls(
            policy=AnalysisPolicy(
                attempt_timeout_seconds=60
                * positive_number(
                    environ, "ANALYSIS_TIMEOUT_MINUTES", defaults.attempt_timeout_seconds / 60
                ),
                heartbeat_timeout_seconds=defaults.heartbeat_timeout_seconds,
                max_attempts=int(
                    positive_number(environ, "ANALYSIS_MAX_ATTEMPTS", defaults.max_attempts)
                ),
                retry_after_seconds=defaults.retry_after_seconds,
            )
        )
