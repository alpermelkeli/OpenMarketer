"""Read-only shallow clone of the repository to analyse.

By default the clone is of the default branch. A caller that needs one known
state of the repository (the evaluation pins its cases) names a commit, and
that commit alone is fetched; everything below holds for both.

The repository and the host it is on are untrusted, so the clone is locked
down:

- only HTTPS URLs and local git folders are accepted, and git may use no other
  transport at any point of the clone;
- git gets an environment built here, not the one of this process: none of
  its variables can change how git behaves, and none of the secrets in it
  (model keys, the database URL, tokens) reach git or a program git starts;
- git asks nobody for a password: no askpass program, no credential helper,
  no prompt, no ``.netrc``. The only credential it can send is a token
  configured for the host of the URL (``credentials.py`` decides);
- no redirect is followed. After a redirect git repeats its extra headers,
  the token among them, to the new host; and without a token a redirect could
  still send the clone to an address the caller did not name;
- submodules and LFS objects are not fetched, and symbolic links are checked
  out as plain text so that no path in the working tree can point outside it.

What git prints when a clone fails is partly written by the remote host. It
goes to the log; the error raised is one of a few fixed sentences.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import shutil
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from openmarketer_core.repository_analysis.intake.credentials import (
    NO_TOKENS,
    RepositoryTokens,
    is_unambiguous,
)
from openmarketer_core.repository_analysis.intake.errors import IntakeError

logger = logging.getLogger(__name__)

CLONE_TIMEOUT_S = 300

# Why a clone failed, as told to whoever asked for it. Fixed text: nothing a
# repository host wrote is repeated.
NOT_FOUND = (
    "the repository was not found, or it needs an access token that is not configured "
    "for its host or does not give access to it"
)
REDIRECTED = (
    "the repository URL redirects to another address, and redirects are not followed: "
    "use the URL the repository has now (a renamed or moved repository redirects, "
    "and some hosts redirect a URL that does not end in .git)"
)
CLONE_TIMED_OUT = f"cloning the repository did not finish within {CLONE_TIMEOUT_S} seconds"
HOST_UNREACHABLE = "the repository host could not be reached"
NOT_SECURE = "the connection to the repository host could not be secured (TLS)"
NOT_CLONED = "the repository could not be cloned"
COMMIT_NOT_FETCHED = (
    "the requested commit could not be fetched from the repository: it may not have "
    "that commit, or its host may not serve single commits"
)
NOT_A_COMMIT_ID = "commit must be a full commit id: 40 lowercase hexadecimal characters"

# A full SHA-1 object name and nothing else, so that what is handed to git as the commit
# can be neither an option nor a ref that moves. A 64-character (SHA-256) id is not accepted:
# the repository made for the fetch is a SHA-1 one, where git would read it as a ref name.
_COMMIT_ID = re.compile(r"[0-9a-f]{40}")

# For every git command that writes the working tree: links stay text, no hook runs.
_NO_LINKS_NO_HOOKS = ["-c", "core.symlinks=false", "-c", f"core.hooksPath={os.devnull}"]

# What git's own messages (in the C locale) say for each cause, tried in this order.
_CAUSES: tuple[tuple[str, str], ...] = (
    (r"returned error: 30\d|redirect", REDIRECTED),
    (r"ssl|tls|certificate", NOT_SECURE),
    (r"timed out|timeout", CLONE_TIMED_OUT),
    (
        r"could not resolve|failed to connect|couldn't connect|connection refused"
        r"|connection reset|network is unreachable|empty reply",
        HOST_UNREACHABLE,
    ),
    (
        r"not found|returned error: 40[134]|authentication failed"
        r"|could not read username|could not read password",
        NOT_FOUND,
    ),
)

# The variables of this process that git may see. Everything else is withheld.
INHERITED_VARIABLES = (
    "PATH",  # to find git and the programs it is made of
    "TMPDIR",
    # An installation behind a proxy, or with its own certificate authority, cannot
    # clone without these. They add a route or a trusted issuer; none switches
    # verification off (GIT_SSL_NO_VERIFY is not here).
    "HTTPS_PROXY",
    "https_proxy",
    "NO_PROXY",
    "no_proxy",
    "GIT_SSL_CAINFO",
    "GIT_SSL_CAPATH",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "CURL_CA_BUNDLE",
)


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
    """Environment for git, built from nothing: see the module docstring for what it rules out.

    The token travels as an HTTP header set through the environment, so it is
    neither visible in the process list nor written to ``.git/config``, and the
    header is configured for the origin of ``url`` only.
    """
    env = {name: os.environ[name] for name in INHERITED_VARIABLES if name in os.environ}
    env.update(
        # No home folder: no user configuration, no ~/.netrc for curl to read.
        HOME=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        # Messages in one language, because the cause of a failure is read from them.
        LC_ALL="C",
        GIT_TERMINAL_PROMPT="0",
        # Set and empty: git then asks no program for a password, whatever else is configured.
        GIT_ASKPASS="",
        SSH_ASKPASS="",
        GIT_LFS_SKIP_SMUDGE="1",
    )
    config = {
        "core.askPass": "",
        "credential.helper": "",
        "http.followRedirects": "false",
        "protocol.allow": "never",
        f"protocol.{urlsplit(url).scheme}.allow": "always",
    }
    if token:
        config[f"http.{_origin(url)}.extraHeader"] = f"Authorization: Basic {_authorization(token)}"
    env["GIT_CONFIG_COUNT"] = str(len(config))
    for index, (key, value) in enumerate(config.items()):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


class _GitFailed(Exception):
    """git exited with an error. ``output`` is what it printed, without secrets."""

    def __init__(self, output: str) -> None:
        super().__init__("git failed")
        self.output = output


class _GitTimedOut(Exception):
    """git was stopped after ``CLONE_TIMEOUT_S``."""


def _without(secrets: Sequence[str], text: str) -> str:
    for secret in secrets:
        text = text.replace(secret, "[REDACTED]")
    return text


def _git(
    args: list[str],
    *,
    env: dict[str, str],
    cwd: Path | None = None,
    secrets: Sequence[str] = (),
    timeout_s: float | None = None,
) -> str:
    """Run git and return what it printed. ``secrets`` are removed from what a failure carries.

    git is stopped after ``timeout_s`` seconds, or after ``CLONE_TIMEOUT_S`` when none is given.
    """
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            # A remote host chooses some of these bytes; they need not be valid text.
            encoding="utf-8",
            errors="replace",
            timeout=CLONE_TIMEOUT_S if timeout_s is None else timeout_s,
            check=False,
        )
    except FileNotFoundError as e:
        raise IntakeError("git is not installed") from e
    except subprocess.TimeoutExpired:
        raise _GitTimedOut from None
    if done.returncode != 0:
        raise _GitFailed(_without(secrets, done.stderr).strip())
    return done.stdout.strip()


def _cause(git_output: str) -> str:
    """The fixed sentence for what git reported.

    Only git's own lines are read. Lines it relays from the host start with
    ``remote:``, and the URL it quotes was written by the caller; neither
    decides anything.
    """
    own_lines = [
        re.sub(r"'[^']*'", "", line).lower()
        for line in git_output.splitlines()
        if line.startswith(("fatal:", "error:"))
    ]
    for pattern, cause in _CAUSES:
        if any(re.search(pattern, line) for line in own_lines):
            return cause
    return NOT_CLONED


def _discard_partial_fetch(dest: Path, commit: str | None) -> None:
    """Remove what a failed fetch of a commit left in ``dest``, as ``git clone`` does itself."""
    if commit is not None:
        shutil.rmtree(dest, ignore_errors=True)


def _clone_default_branch(
    url: str, dest: Path, *, env: dict[str, str], secrets: Sequence[str]
) -> None:
    _git(
        [
            *_NO_LINKS_NO_HOOKS,
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


def _fetch_commit(
    url: str, dest: Path, commit: str, *, env: dict[str, str], secrets: Sequence[str]
) -> None:
    """Fetch ``commit`` alone from ``url`` into a new repository at ``dest`` and check it out.

    The URL is given to ``fetch`` directly, so no remote is written to the new
    repository's configuration. The three commands share one ``CLONE_TIMEOUT_S``,
    so a pinned clone is stopped as soon as a clone of the default branch is.
    """
    deadline = time.monotonic() + CLONE_TIMEOUT_S
    steps: tuple[tuple[list[str], Path | None], ...] = (
        (["init", "--quiet", "--", str(dest)], None),
        (
            ["fetch", "--depth", "1", "--no-tags", "--no-recurse-submodules", "--", url, commit],
            dest,
        ),
        (["checkout", "--quiet", "--detach", "FETCH_HEAD"], dest),
    )
    for args, cwd in steps:
        left = deadline - time.monotonic()
        if left <= 0:
            raise _GitTimedOut
        _git([*_NO_LINKS_NO_HOOKS, *args], env=env, cwd=cwd, secrets=secrets, timeout_s=left)


def clone(
    source: str, dest: Path, *, tokens: RepositoryTokens = NO_TOKENS, commit: str | None = None
) -> Snapshot:
    """Shallow-clone the default branch of ``source``, or ``commit`` alone, into ``dest``.

    A token from ``tokens`` is used only if it is configured for the host of ``source``.
    ``commit`` is a full commit id; the snapshot is then of exactly that commit
    and has no ``ref``.
    """
    url = _clone_url(source)
    if commit is not None and not _COMMIT_ID.fullmatch(commit):
        raise IntakeError(NOT_A_COMMIT_ID)
    if dest.exists() and any(dest.iterdir()):
        raise IntakeError(f"destination is not empty: {dest}")

    token = tokens.token_for(url)
    env = _git_env(token, url)
    secrets = (token, _authorization(token)) if token else ()
    try:
        if commit is None:
            _clone_default_branch(url, dest, env=env, secrets=secrets)
        else:
            _fetch_commit(url, dest, commit, env=env, secrets=secrets)
    except _GitTimedOut:
        logger.warning("git clone of %s was stopped after %ss", url, CLONE_TIMEOUT_S)
        _discard_partial_fetch(dest, commit)
        raise IntakeError(CLONE_TIMED_OUT) from None
    except _GitFailed as e:
        # For the operator. The error itself carries none of it: see _cause.
        logger.warning("git clone of %s failed: %r", url, e.output[-2000:])
        _discard_partial_fetch(dest, commit)
        cause = _cause(e.output)
        if commit is not None and cause == NOT_CLONED:
            cause = COMMIT_NOT_FETCHED
        raise IntakeError(cause) from None

    try:
        commit_sha = _git(["rev-parse", "HEAD"], env=env, cwd=dest)
    except (_GitFailed, _GitTimedOut):
        raise IntakeError("repository has no commits") from None
    if commit is not None and commit_sha != commit:
        # What is checked out is a commit nobody asked for; none of it is left behind.
        _discard_partial_fetch(dest, commit)
        raise IntakeError(COMMIT_NOT_FETCHED)
    try:
        ref = _git(["symbolic-ref", "--short", "-q", "HEAD"], env=env, cwd=dest) or None
    except (_GitFailed, _GitTimedOut):
        ref = None
    return Snapshot(root=dest.resolve(), source_url=url, commit_sha=commit_sha, ref=ref)
