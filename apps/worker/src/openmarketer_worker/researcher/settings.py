"""Settings of the researcher's workflow, read from the environment at the edge.

What an operator can change about a research run: how long one attempt may
take, how many attempts a run gets, where the installation's search and
rendering services are, and how long fetched pages are kept. The other limits
and every default, with their reasons, are in ``policy.py``; this module only
fills that policy and checks the addresses. It takes the environment as an
argument and is called when the worker starts; the workflow and the activities
read none of it.

The search and the rendering service are free and self-hosted (SearXNG and
Lightpanda in the dev stack); a missing or malformed address stops the worker
at start-up, not every fetch. The defaults are the dev stack's published
ports, for the worker run on the host.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlsplit

from openmarketer_worker.researcher.policy import ResearchPolicy
from openmarketer_worker.settings import SettingsError, positive_number

DEFAULT_SEARXNG_URL = "http://localhost:8888"
DEFAULT_LIGHTPANDA_URL = "http://localhost:9222"
DEFAULT_PAGE_RETENTION_DAYS = 30


@dataclass(frozen=True)
class ResearcherSettings:
    """What the researcher's worker side is configured with."""

    policy: ResearchPolicy = field(default_factory=ResearchPolicy)
    searxng_url: str = DEFAULT_SEARXNG_URL
    lightpanda_url: str = DEFAULT_LIGHTPANDA_URL
    page_retention: timedelta = timedelta(days=DEFAULT_PAGE_RETENTION_DAYS)

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> ResearcherSettings:
        """Raises ``SettingsError``, naming the variable, for a value that does not fit."""
        defaults = ResearchPolicy()
        return cls(
            policy=ResearchPolicy(
                attempt_timeout_seconds=60
                * positive_number(
                    environ,
                    "RESEARCH_TIMEOUT_MINUTES",
                    defaults.attempt_timeout_seconds / 60,
                ),
                heartbeat_timeout_seconds=defaults.heartbeat_timeout_seconds,
                max_attempts=int(
                    positive_number(environ, "RESEARCH_MAX_ATTEMPTS", defaults.max_attempts)
                ),
                retry_after_seconds=defaults.retry_after_seconds,
            ),
            searxng_url=_http_url(environ, "SEARXNG_URL", DEFAULT_SEARXNG_URL),
            lightpanda_url=_http_url(environ, "LIGHTPANDA_URL", DEFAULT_LIGHTPANDA_URL),
            page_retention=timedelta(
                days=positive_number(
                    environ, "RESEARCH_PAGE_RETENTION_DAYS", DEFAULT_PAGE_RETENTION_DAYS
                )
            ),
        )


def _http_url(environ: Mapping[str, str], name: str, default: str) -> str:
    value = (environ.get(name) or default).strip().rstrip("/")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise SettingsError(f"{name} must be an http(s) address like {default}, not {value!r}")
    return value
