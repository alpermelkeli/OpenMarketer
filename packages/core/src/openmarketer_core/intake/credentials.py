"""Which access token, if any, may be sent to the host of a repository URL.

A repository URL can come from anyone who can create a project, so it must
never decide where a credential goes. Each token is configured for one host,
and it is offered only to an ``https://`` URL whose host is exactly that one:
compared in lower case and without a trailing dot, on the same port (443
unless the configuration names another), with no suffix or prefix matching. A
URL on any other host is cloned without credentials, which is how public
repositories work.

This module decides; it does not talk to git. How the token is then kept from
following a redirect to another host is in ``git.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit

from openmarketer_core.intake.errors import IntakeError

GITHUB_HOST = "github.com"
GITLAB_HOST = "gitlab.com"
HTTPS_PORT = 443

HostAndPort = tuple[str, int]


class RepositoryTokens:
    """Access tokens, each for the one host it was issued by."""

    def __init__(self, token_by_host: Mapping[str, str]) -> None:
        """``token_by_host`` maps ``host`` or ``host:port`` to a token; empty tokens are ignored."""
        # Every host is read, with or without a token, so a mistake in one shows at start-up.
        configured = {_configured_host(host): token for host, token in token_by_host.items()}
        self._tokens = {host: token for host, token in configured.items() if token}

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> RepositoryTokens:
        """The tokens an installation configures: one for GitHub, one for one GitLab host.

        ``GITHUB_TOKEN`` belongs to github.com. ``GITLAB_TOKEN`` belongs to the
        host named by ``GITLAB_HOST`` (a self-hosted GitLab), or to gitlab.com
        when that is not set.
        """
        gitlab_host = environ.get("GITLAB_HOST") or GITLAB_HOST
        try:
            return cls(
                {
                    GITHUB_HOST: environ.get("GITHUB_TOKEN", ""),
                    gitlab_host: environ.get("GITLAB_TOKEN", ""),
                }
            )
        except IntakeError as e:
            raise IntakeError(f"GITLAB_HOST: {e}") from e

    def token_for(self, repository_url: str) -> str | None:
        """The token to send when cloning ``repository_url``, or ``None`` for no credentials."""
        if not is_unambiguous(repository_url):
            return None
        try:
            parts = urlsplit(repository_url)
            host, port = parts.hostname, parts.port
        except ValueError:
            return None
        if parts.scheme != "https" or not host or parts.username is not None:
            return None
        return self._tokens.get((_normalised(host), HTTPS_PORT if port is None else port))

    def __repr__(self) -> str:
        # Never the tokens: a repr ends up in logs and tracebacks.
        hosts = sorted(f"{host}:{port}" for host, port in self._tokens)
        return f"RepositoryTokens(hosts={hosts})"


def is_unambiguous(url: str) -> bool:
    """Whether every URL parser reads the same host out of ``url``.

    A backslash, a space or a control character is read differently by
    different parsers; the host this code compares must be the host git contacts.
    """
    return "\\" not in url and all(c.isprintable() and not c.isspace() for c in url)


def _normalised(host: str) -> str:
    return host.lower().rstrip(".")


def _configured_host(host: str) -> HostAndPort:
    """Read a configured ``host`` or ``host:port``; anything more than that is a mistake."""
    try:
        parts = urlsplit(f"//{host}")
        name, port = parts.hostname, parts.port
    except ValueError as e:
        raise IntakeError(f"not a host name: {host!r}") from e
    if not name or parts.netloc != host or parts.username is not None or parts.path:
        raise IntakeError(
            f"a token is configured for a host name such as gitlab.example.com, not {host!r}"
        )
    return _normalised(name), HTTPS_PORT if port is None else port


NO_TOKENS = RepositoryTokens({})
