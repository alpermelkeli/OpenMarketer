"""Read-only shallow clone of the repository to analyse.

The repository is untrusted input, so the clone is locked down: only HTTPS
URLs and local git folders are accepted, the user's git configuration and
credential helpers are ignored, submodules and LFS objects are not fetched,
and symbolic links are checked out as plain text so that no path in the
working tree can point outside it.
"""

from __future__ import annotations

import base64
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from openmarketer_core.intake.errors import IntakeError

CLONE_TIMEOUT_S = 300


@dataclass(frozen=True)
class Snapshot:
    """A checked-out commit of the analysed repository."""

    root: Path
    source_url: str  # what git cloned from; a local folder appears as a file:// URL
    commit_sha: str
    ref: str | None


def _clone_url(source: str) -> str:
    """Validate ``source`` and return the URL handed to git."""
    if source.startswith("-"):
        raise IntakeError("repository source must be an https:// URL or a local folder")

    parts = urlsplit(source)
    if parts.scheme == "https":
        if not parts.hostname:
            raise IntakeError("repository URL has no host")
        if parts.username or parts.password:
            raise IntakeError("credentials in the URL are not accepted; pass a token instead")
        return source

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


def _git_env(token: str | None) -> dict[str, str]:
    """Environment for git: isolated from user configuration, never prompts.

    The token travels as an HTTP header set through the environment, so it is
    neither visible in the process list nor written to ``.git/config``.
    """
    env = dict(os.environ)
    env.update(
        GIT_TERMINAL_PROMPT="0",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_LFS_SKIP_SMUDGE="1",
    )
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="http.extraHeader",
            GIT_CONFIG_VALUE_0=f"Authorization: Basic {basic}",
        )
    return env


def _git(args: list[str], *, env: dict[str, str], cwd: Path | None = None) -> str:
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
        raise IntakeError(f"git {args[0]} failed: {done.stderr.strip()[-500:]}")
    return done.stdout.strip()


def clone(source: str, dest: Path, *, token: str | None = None) -> Snapshot:
    """Shallow-clone the default branch of ``source`` into ``dest``."""
    url = _clone_url(source)
    if dest.exists() and any(dest.iterdir()):
        raise IntakeError(f"destination is not empty: {dest}")

    env = _git_env(token)
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
