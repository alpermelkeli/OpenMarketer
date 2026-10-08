"""Read-only shallow clone of the repository to analyse.

The repository is untrusted input, so the clone is locked down: only HTTPS
URLs and local git folders are accepted, the user's git configuration and
credential helpers are ignored, submodules and LFS objects are not fetched,
and symbolic links are checked out as plain text so that no path in the
working tree can point outside it.

An access token is sent only to the host it is configured for
(``credentials.py`` decides), and git is told not to follow redirects while it
carries one: after a redirect git sends its extra headers to the new host,
whatever URL the header was configured for.
"""

from __future__ import annotations

import base64
import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from openmarketer_core.intake.credentials import NO_TOKENS, RepositoryTokens, is_unambiguous
from openmarketer_core.intake.errors import IntakeError

CLONE_TIMEOUT_S = 300


@dataclass(frozen=True)
class Snapshot:
    """A checked-out commit of the analysed repository."""

    root: Path
    source_url: str  # what git cloned from; a local folder appears as a file:// URL
    commit_sha: str
    ref: str | None


def remote_repository_url(source: str) -> str:
    """Validate ``source`` as the URL of a remote repository and return it.

    This is the only form a caller that is not the machine's own user may
    submit: a local folder or a ``file://`` URL would let a request make the
    service read the disk it runs on.
    """
    try:
        parts = urlsplit(source)
        host, _ = parts.hostname, parts.port
    except ValueError as e:
        raise IntakeError("repository URL is not a valid URL") from e
    if parts.scheme != "https":
        raise IntakeError("repository URL must start with https://")
    if not host:
        raise IntakeError("repository URL has no host")
    if parts.username is not None or parts.password is not None:
        raise IntakeError("credentials in the URL are not accepted; configure a token instead")
    if not is_unambiguous(source):
        raise IntakeError("repository URL contains characters that are not allowed in a URL")
    return source


def _clone_url(source: str) -> str:
    """Validate ``source`` and return the URL handed to git."""
    if source.startswith("-"):
        raise IntakeError("repository source must be an https:// URL or a local folder")

    parts = urlsplit(source)
    if parts.scheme == "https":
        return remote_repository_url(source)

    path = Path(source).expanduser()
    if parts.scheme in ("", "file") or path.exists():
        if parts.scheme == "file":
            path = Path(parts.path)
        if not path.is_dir():
            raise IntakeError(f"local repository not found: {source}")
        if not (path / ".git").exists():
            raise IntakeError(f"not a git repository: {source}")
        return path.resolve().as_uri()

    raise IntakeError(
        f"unsupported repository source (only https:// URLs and local folders): {source}"
    )


def _origin(url: str) -> str:
    """Scheme, host and port of ``url``, the form git matches ``http.<url>.*`` settings by."""
    parts = urlsplit(url)
    port = "" if parts.port is None else f":{parts.port}"
    return f"{parts.scheme}://{parts.hostname}{port}/"


def _authorization(token: str) -> str:
    return base64.b64encode(f"x-access-token:{token}".encode()).decode()


def _git_env(token: str | None, url: str) -> dict[str, str]:
    """Environment for git: isolated from user configuration, never prompts.

    The token travels as an HTTP header set through the environment, so it is
    neither visible in the process list nor written to ``.git/config``. The
    header is configured for the origin of ``url`` only, and redirects are
    refused: git would repeat the header to the host a redirect names.
    """
    env = dict(os.environ)
    env.update(
        GIT_TERMINAL_PROMPT="0",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_LFS_SKIP_SMUDGE="1",
    )
    if token:
        env.update(
            GIT_CONFIG_COUNT="2",
            GIT_CONFIG_KEY_0=f"http.{_origin(url)}.extraHeader",
            GIT_CONFIG_VALUE_0=f"Authorization: Basic {_authorization(token)}",
            GIT_CONFIG_KEY_1="http.followRedirects",
            GIT_CONFIG_VALUE_1="false",
        )
    return env


def _without(secrets: Sequence[str], text: str) -> str:
    for secret in secrets:
        text = text.replace(secret, "[REDACTED]")
    return text


def _git(
    args: list[str], *, env: dict[str, str], cwd: Path | None = None, secrets: Sequence[str] = ()
) -> str:
    """Run git and return what it printed. ``secrets`` never appear in the error it raises."""
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=CLONE_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError as e:
        raise IntakeError("git is not installed") from e
    except subprocess.TimeoutExpired as e:
        raise IntakeError(f"git {args[0]} timed out after {CLONE_TIMEOUT_S}s") from e
    if done.returncode != 0:
        raise IntakeError(f"git {args[0]} failed: {_without(secrets, done.stderr).strip()[-500:]}")
    return done.stdout.strip()


def clone(source: str, dest: Path, *, tokens: RepositoryTokens = NO_TOKENS) -> Snapshot:
    """Shallow-clone the default branch of ``source`` into ``dest``.

    A token from ``tokens`` is used only if it is configured for the host of ``source``.
    """
    url = _clone_url(source)
    if dest.exists() and any(dest.iterdir()):
        raise IntakeError(f"destination is not empty: {dest}")

    token = tokens.token_for(url)
    env = _git_env(token, url)
    secrets = (token, _authorization(token)) if token else ()
    _git(
        [
            "-c", "core.symlinks=false",
            "-c", f"core.hooksPath={os.devnull}",
            "-c", "protocol.ext.allow=never",
            "clone",
            "--depth", "1",
            "--single-branch",
            "--no-tags",
            "--no-recurse-submodules",
            "--",
            url,
            str(dest),
        ],
        env=env,
        secrets=secrets,
    )  # fmt: skip

    try:
        commit_sha = _git(["rev-parse", "HEAD"], env=env, cwd=dest)
    except IntakeError as e:
        raise IntakeError("repository has no commits") from e
    try:
        ref = _git(["symbolic-ref", "--short", "-q", "HEAD"], env=env, cwd=dest) or None
    except IntakeError:
        ref = None
    return Snapshot(root=dest.resolve(), source_url=url, commit_sha=commit_sha, ref=ref)
