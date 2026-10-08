"""Tests for the rule that an access token goes only to the host it belongs to.

The redirect test runs real git against two HTTP servers on the loopback
interface; nothing here uses the network.
"""

import base64
import http.server
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from openmarketer_core.intake import IntakeError, RepositoryTokens, run_intake
from openmarketer_core.intake import git as intake_git
from openmarketer_core.intake.git import _git, _git_env, clone

GITHUB = "github-token-value"
GITLAB = "gitlab-token-value"
TOKENS = RepositoryTokens({"github.com": GITHUB, "gitlab.com": GITLAB})


def basic(token: str) -> str:
    return base64.b64encode(f"x-access-token:{token}".encode()).decode()


# ------------------------------------------------------------- which host gets a token
@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme/app.git",
        "https://GitHub.COM/acme/app.git",
        "https://github.com./acme/app.git",
        "https://github.com:443/acme/app.git",
    ],
)
def test_token_goes_to_the_host_it_is_configured_for(url):
    assert TOKENS.token_for(url) == GITHUB


def test_each_host_gets_its_own_token():
    assert TOKENS.token_for("https://gitlab.com/acme/app.git") == GITLAB


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/acme/app.git",
        "https://github.com.evil.example/acme/app.git",
        "https://evilgithub.com/acme/app.git",
        "https://api.github.com/acme/app.git",
        "https://evil.example/github.com/app.git",
        "https://evil.example/?github.com",
        "https://evil.example#@github.com/acme/app.git",
        "https://github.com@evil.example/acme/app.git",
        "https://github.com:x@evil.example/acme/app.git",
        "https://evil.example\\@github.com/acme/app.git",
        "https://user@github.com/acme/app.git",
        "https://github.com:8443/acme/app.git",
        "https://github.com:0/acme/app.git",
        "https://github.com:port/acme/app.git",
        "https://github.com /acme/app.git",
        "https://github.com\t.evil.example/acme/app.git",
        "http://github.com/acme/app.git",
        "ssh://github.com/acme/app.git",
        "file:///github.com/acme/app",
        "/srv/github.com/app",
        "github.com/acme/app.git",
    ],
)
def test_no_token_for_any_other_host_port_or_scheme(url):
    assert TOKENS.token_for(url) is None


def test_token_can_be_configured_for_a_port():
    tokens = RepositoryTokens({"Git.Example.com:8443": "t"})
    assert tokens.token_for("https://git.example.com:8443/acme/app.git") == "t"
    assert tokens.token_for("https://git.example.com/acme/app.git") is None


def test_empty_token_is_no_token():
    assert RepositoryTokens({"github.com": ""}).token_for("https://github.com/a/b") is None


@pytest.mark.parametrize(
    "host",
    ["https://gitlab.example.com", "gitlab.example.com/group", "user@gitlab.example.com", ""],
)
def test_configured_host_must_be_a_host_name(host):
    with pytest.raises(IntakeError):
        RepositoryTokens({host: "t"})


def test_tokens_do_not_show_in_a_repr():
    assert GITHUB not in repr(TOKENS)
    assert GITHUB not in str(TOKENS)
    assert "github.com" in repr(TOKENS)


# ------------------------------------------------------------ what the environment configures
def test_github_token_is_for_github_only():
    tokens = RepositoryTokens.from_environment({"GITHUB_TOKEN": GITHUB})
    assert tokens.token_for("https://github.com/acme/app.git") == GITHUB
    assert tokens.token_for("https://gitlab.com/acme/app.git") is None


def test_gitlab_token_is_for_gitlab_com_unless_a_host_is_named():
    tokens = RepositoryTokens.from_environment({"GITLAB_TOKEN": GITLAB})
    assert tokens.token_for("https://gitlab.com/acme/app.git") == GITLAB
    assert tokens.token_for("https://github.com/acme/app.git") is None


def test_gitlab_token_follows_the_configured_host():
    tokens = RepositoryTokens.from_environment(
        {"GITLAB_TOKEN": GITLAB, "GITLAB_HOST": "gitlab.example.com", "GITHUB_TOKEN": GITHUB}
    )
    assert tokens.token_for("https://gitlab.example.com/acme/app.git") == GITLAB
    assert tokens.token_for("https://gitlab.com/acme/app.git") is None
    assert tokens.token_for("https://github.com/acme/app.git") == GITHUB


def test_environment_without_tokens_configures_none():
    tokens = RepositoryTokens.from_environment({})
    assert tokens.token_for("https://github.com/acme/app.git") is None


def test_gitlab_host_that_is_not_a_host_name_is_refused_by_name():
    with pytest.raises(IntakeError, match="GITLAB_HOST"):
        RepositoryTokens.from_environment({"GITLAB_HOST": "https://gitlab.example.com"})


# ----------------------------------------------------------------- how git is told
def test_header_is_configured_for_the_repository_origin_only():
    env = _git_env(GITHUB, "https://github.com/acme/app.git")
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraHeader"
    assert env["GIT_CONFIG_VALUE_0"] == f"Authorization: Basic {basic(GITHUB)}"


def test_git_follows_no_redirect_while_it_carries_a_token():
    env = _git_env(GITHUB, "https://github.com/acme/app.git")
    assert (env["GIT_CONFIG_KEY_1"], env["GIT_CONFIG_VALUE_1"]) == ("http.followRedirects", "false")
    assert env["GIT_CONFIG_COUNT"] == "2"


def test_token_is_not_in_the_environment_as_plain_text():
    env = _git_env(GITHUB, "https://github.com/acme/app.git")
    assert not any(GITHUB in value for value in env.values())


def test_without_a_token_git_gets_no_header():
    env = _git_env(None, "https://github.com/acme/app.git")
    assert not any(key.startswith("GIT_CONFIG_KEY") for key in env)
    assert "GIT_CONFIG_COUNT" not in env


class GitWasCalled(Exception):
    pass


@pytest.fixture
def git_environments(monkeypatch) -> list[dict[str, str]]:
    """Stop ``clone`` at its first git call and collect the environment it would have used."""
    environments: list[dict[str, str]] = []

    def record(args, *, env, cwd=None, secrets=()):
        environments.append(env)
        raise GitWasCalled

    monkeypatch.setattr(intake_git, "_git", record)
    return environments


def test_clone_sends_the_token_of_the_repository_host(git_environments, tmp_path):
    with pytest.raises(GitWasCalled):
        clone("https://github.com/acme/app.git", tmp_path / "clone", tokens=TOKENS)
    assert basic(GITHUB) in git_environments[0]["GIT_CONFIG_VALUE_0"]


@pytest.mark.parametrize(
    "url", ["https://example.com/acme/app.git", "https://github.com.evil.example/acme/app.git"]
)
def test_clone_from_another_host_carries_no_token(git_environments, tmp_path, url):
    with pytest.raises(GitWasCalled):
        run_intake(url, tmp_path / "clone", tokens=TOKENS)
    environment = "\n".join(f"{k}={v}" for k, v in git_environments[0].items())
    assert "extraHeader" not in environment
    for token in (GITHUB, GITLAB):
        assert token not in environment
        assert basic(token) not in environment


def test_clone_without_tokens_carries_none(git_environments, tmp_path):
    with pytest.raises(GitWasCalled):
        clone("https://github.com/acme/app.git", tmp_path / "clone")
    assert "GIT_CONFIG_COUNT" not in git_environments[0]


def test_git_error_does_not_repeat_a_secret():
    env = _git_env(None, "https://example.com/acme/app.git")
    with pytest.raises(IntakeError) as failure:
        _git(
            [
                "-c",
                f"alias.fail=!echo 'fatal: sent {basic(GITHUB)} for {GITHUB}' >&2; exit 1",
                "fail",
            ],
            env=env,
            secrets=(GITHUB, basic(GITHUB)),
        )
    assert "fatal: sent [REDACTED] for [REDACTED]" in str(failure.value)
    assert GITHUB not in str(failure.value)
    assert basic(GITHUB) not in str(failure.value)


# ------------------------------------------------------------------- redirects
@dataclass
class Requests:
    """The Authorization header of every request a test server received."""

    authorizations: list[str | None] = field(default_factory=list)


def serve(handler: type[http.server.BaseHTTPRequestHandler]) -> http.server.HTTPServer:
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@dataclass
class RedirectingHost:
    url: str
    received: Requests
    target_received: Requests


@pytest.fixture
def redirecting_host(tmp_path: Path) -> Iterator[RedirectingHost]:
    """A host that redirects every request to a second host serving a real repository.

    The second host answers git's first request properly, so that git goes on
    to its next request: that one is where it would repeat the header.
    """
    repo = tmp_path / "served"
    repo.mkdir()
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "first"]):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            env=_git_env(None, "https://example.com/"),
        )
    advertised = subprocess.run(
        ["git", "upload-pack", "--stateless-rpc", "--advertise-refs", str(repo)],
        check=True,
        capture_output=True,
    ).stdout
    service = b"# service=git-upload-pack\n"
    advertisement = b"%04x" % (len(service) + 4) + service + b"0000" + advertised
    at_origin, at_target = Requests(), Requests()

    class Target(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            at_target.authorizations.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-git-upload-pack-advertisement")
            self.send_header("Content-Length", str(len(advertisement)))
            self.end_headers()
            self.wfile.write(advertisement)

        def do_POST(self) -> None:
            at_target.authorizations.append(self.headers.get("Authorization"))
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    target = serve(Target)

    class Origin(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            at_origin.authorizations.append(self.headers.get("Authorization"))
            self.send_response(301)
            self.send_header("Location", f"http://127.0.0.1:{target.server_address[1]}{self.path}")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    origin = serve(Origin)
    try:
        yield RedirectingHost(
            url=f"http://127.0.0.1:{origin.server_address[1]}/acme/app.git",
            received=at_origin,
            target_received=at_target,
        )
    finally:
        for server in (origin, target):
            server.shutdown()
            server.server_close()


def git_clone(url: str, dest: Path, env: dict[str, str]) -> None:
    subprocess.run(
        ["git", "clone", "--depth", "1", "--", url, str(dest)],
        env=env,
        capture_output=True,
        check=False,
        timeout=60,
    )


def test_token_does_not_follow_a_redirect_to_another_host(redirecting_host, tmp_path):
    git_clone(redirecting_host.url, tmp_path / "clone", _git_env(GITHUB, redirecting_host.url))
    assert redirecting_host.received.authorizations == [f"Basic {basic(GITHUB)}"]
    assert redirecting_host.target_received.authorizations == []


def test_scoping_the_header_alone_would_not_stop_it(redirecting_host, tmp_path):
    # Why redirects are refused: with the header configured for the first host only,
    # git still repeats it to the second host once it has been redirected there.
    env = _git_env(GITHUB, redirecting_host.url)
    env["GIT_CONFIG_COUNT"] = "1"
    git_clone(redirecting_host.url, tmp_path / "clone", env)
    assert f"Basic {basic(GITHUB)}" in redirecting_host.target_received.authorizations


def test_without_a_token_a_redirect_is_followed(redirecting_host, tmp_path):
    git_clone(redirecting_host.url, tmp_path / "clone", _git_env(None, redirecting_host.url))
    assert redirecting_host.target_received.authorizations == [None, None]
