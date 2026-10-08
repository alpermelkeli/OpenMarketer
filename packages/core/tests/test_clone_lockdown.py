"""Tests for how git is run on an untrusted repository host.

Most of them run real git, through ``clone``, against HTTPS servers on the
loopback interface with a certificate made for the test; nothing here uses the
network. They need ``openssl`` to make that certificate.
"""

import base64
import http.server
import logging
import os
import shutil
import ssl
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from openmarketer_core.intake import IntakeError, RepositoryTokens, run_intake
from openmarketer_core.intake import git as intake_git
from openmarketer_core.intake.git import (
    CLONE_TIMED_OUT,
    HOST_UNREACHABLE,
    INHERITED_VARIABLES,
    NOT_CLONED,
    NOT_FOUND,
    NOT_SECURE,
    REDIRECTED,
    _cause,
    _git,
    _git_env,
    _GitFailed,
    clone,
)

GITHUB = "github-token-value"
GITLAB = "gitlab-token-value"
TOKENS = RepositoryTokens({"github.com": GITHUB, "gitlab.com": GITLAB})
REPOSITORY = "https://github.com/acme/app.git"
HOST_TEXT = "TEXT WRITTEN BY THE HOST"


def basic(token: str) -> str:
    return base64.b64encode(f"x-access-token:{token}".encode()).decode()


def configuration(env: dict[str, str]) -> dict[str, str]:
    """The git settings an environment carries."""
    count = int(env["GIT_CONFIG_COUNT"])
    return {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(count)}


# ------------------------------------------------------------ git's environment
SECRETS_OF_THE_PROCESS = {
    "OPENROUTER_API_KEY": "model-provider-key",
    "DATABASE_URL": "postgresql+psycopg://user:database-password@db/openmarketer",
    "GITHUB_TOKEN": "token-of-the-process",
    "GITLAB_TOKEN": "another-token-of-the-process",
    "SECRET_KEY": "application-secret",
}
SETTINGS_THAT_CHANGE_GIT = {
    "GIT_SSL_NO_VERIFY": "1",
    "GIT_ASKPASS": "/usr/local/bin/askpass",
    "SSH_ASKPASS": "/usr/local/bin/askpass",
    "GIT_CONFIG_PARAMETERS": "'http.sslVerify=false'",
    "GIT_TRACE": "1",
    "GIT_TRACE_CURL": "1",
    "GIT_CURL_VERBOSE": "1",
    "GIT_SSH_COMMAND": "/usr/local/bin/ssh-wrapper",
    "GIT_PROXY_COMMAND": "/usr/local/bin/proxy",
    "GIT_EXEC_PATH": "/tmp/other-git",
    "GIT_SSL_CERT": "/etc/client.pem",
    "HTTP_PROXY": "http://proxy.example:3128",
    "ALL_PROXY": "socks5://proxy.example:1080",
    "XDG_CONFIG_HOME": "/home/someone/.config",
}


def test_secrets_of_the_process_do_not_reach_git(monkeypatch):
    for name, value in SECRETS_OF_THE_PROCESS.items():
        monkeypatch.setenv(name, value)
    env = _git_env(None, REPOSITORY)
    assert not set(SECRETS_OF_THE_PROCESS) & set(env)
    assert not any(
        secret in value for secret in SECRETS_OF_THE_PROCESS.values() for value in env.values()
    )


@pytest.mark.parametrize("name", SETTINGS_THAT_CHANGE_GIT)
def test_setting_of_the_process_does_not_change_git(monkeypatch, name):
    monkeypatch.setenv(name, SETTINGS_THAT_CHANGE_GIT[name])
    assert _git_env(None, REPOSITORY).get(name) in (None, "")


def test_git_sees_only_listed_variables_of_the_process(monkeypatch):
    monkeypatch.setenv("SOMETHING_NEW_TOMORROW", "x")
    set_here = {
        "HOME",
        "LC_ALL",
        "GIT_TERMINAL_PROMPT",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_GLOBAL",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "GIT_LFS_SKIP_SMUDGE",
    }
    names = {name for name in _git_env(None, REPOSITORY) if not name.startswith("GIT_CONFIG_")}
    assert names <= set(INHERITED_VARIABLES) | set_here | {
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_GLOBAL",
    }


@pytest.mark.parametrize("name", ["HTTPS_PROXY", "NO_PROXY", "GIT_SSL_CAINFO", "SSL_CERT_FILE"])
def test_proxy_and_certificate_authority_of_the_installation_reach_git(monkeypatch, name):
    monkeypatch.setenv(name, "configured-by-the-operator")
    assert _git_env(None, REPOSITORY)[name] == "configured-by-the-operator"


def test_git_has_no_home_folder_to_read_credentials_from():
    env = _git_env(None, REPOSITORY)
    assert (env["HOME"], env["GIT_CONFIG_GLOBAL"], env["GIT_CONFIG_NOSYSTEM"]) == (
        "/dev/null",
        "/dev/null",
        "1",
    )


def test_git_asks_no_program_and_no_person_for_a_password():
    env = _git_env(None, REPOSITORY)
    assert (env["GIT_ASKPASS"], env["SSH_ASKPASS"], env["GIT_TERMINAL_PROMPT"]) == ("", "", "0")
    settings = configuration(env)
    assert (settings["core.askPass"], settings["credential.helper"]) == ("", "")


@pytest.mark.parametrize("token", [None, GITHUB])
def test_git_follows_no_redirect_with_or_without_a_token(token):
    assert configuration(_git_env(token, REPOSITORY))["http.followRedirects"] == "false"


def test_git_may_use_https_and_nothing_else_for_a_remote_repository():
    settings = configuration(_git_env(None, REPOSITORY))
    protocols = {key: value for key, value in settings.items() if key.startswith("protocol.")}
    assert protocols == {"protocol.allow": "never", "protocol.https.allow": "always"}


def test_header_is_configured_for_the_repository_origin_only():
    settings = configuration(_git_env(GITHUB, REPOSITORY))
    assert settings["http.https://github.com/.extraHeader"] == (
        f"Authorization: Basic {basic(GITHUB)}"
    )


def test_token_is_not_in_the_environment_as_plain_text():
    assert not any(GITHUB in value for value in _git_env(GITHUB, REPOSITORY).values())


def test_without_a_token_git_gets_no_header():
    assert not any("extraHeader" in key for key in configuration(_git_env(None, REPOSITORY)))


# ------------------------------------------------------- which token clone chooses
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
        clone(REPOSITORY, tmp_path / "clone", tokens=TOKENS)
    assert f"Authorization: Basic {basic(GITHUB)}" in configuration(git_environments[0]).values()


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
        clone(REPOSITORY, tmp_path / "clone")
    assert "extraHeader" not in "\n".join(git_environments[0].values())


def test_failure_of_git_does_not_carry_a_secret():
    script = f"!echo 'fatal: sent {basic(GITHUB)} for {GITHUB}' >&2; exit 1"
    with pytest.raises(_GitFailed) as failure:
        _git(
            ["-c", f"alias.fail={script}", "fail"],
            env=_git_env(None, REPOSITORY),
            secrets=(GITHUB, basic(GITHUB)),
        )
    assert failure.value.output == "fatal: sent [REDACTED] for [REDACTED]"
    assert GITHUB not in str(failure.value)


# ------------------------------------------------------- the cause of a failed clone
@pytest.mark.parametrize(
    ("git_output", "cause"),
    [
        ("fatal: repository 'https://h/a/b.git/' not found", NOT_FOUND),
        (
            "fatal: unable to access 'https://h/a/': The requested URL returned error: 404",
            NOT_FOUND,
        ),
        (
            "fatal: unable to access 'https://h/a/': The requested URL returned error: 403",
            NOT_FOUND,
        ),
        ("fatal: Authentication failed for 'https://h/a/b.git/'", NOT_FOUND),
        ("fatal: could not read Username for 'https://h': terminal prompts disabled", NOT_FOUND),
        (
            "fatal: unable to access 'https://h/a/': The requested URL returned error: 301",
            REDIRECTED,
        ),
        (
            "fatal: unable to access 'https://h/a/': The requested URL returned error: 302",
            REDIRECTED,
        ),
        (
            "fatal: unable to access 'https://h/a/': Protocol \"http\" disabled (in redirect)",
            REDIRECTED,
        ),
        (
            "fatal: unable to access 'https://h/a/': SSL certificate problem: self signed",
            NOT_SECURE,
        ),
        ("fatal: unable to access 'https://h/a/': Could not resolve host: h", HOST_UNREACHABLE),
        (
            "fatal: unable to access 'https://h/a/': Failed to connect to h port 443 after 0 ms: "
            "Couldn't connect to server",
            HOST_UNREACHABLE,
        ),
        (
            "fatal: unable to access 'https://h/a/': Operation timed out after 300 ms",
            CLONE_TIMED_OUT,
        ),
        (
            "fatal: unable to access 'https://h/a/': The requested URL returned error: 500",
            NOT_CLONED,
        ),
        ("fatal: transport 'file' not allowed", NOT_CLONED),
        ("", NOT_CLONED),
    ],
)
def test_cause_is_read_from_what_git_says(git_output, cause):
    assert _cause(f"Cloning into '/tmp/x/repo'...\n{git_output}") == cause


def test_host_cannot_choose_the_cause():
    relayed = "remote: fatal: SSL certificate problem\nremote: returned error: 301\n"
    assert _cause(relayed + "fatal: repository 'https://h/a/b.git/' not found") == NOT_FOUND


def test_url_cannot_choose_the_cause():
    output = "fatal: unable to access 'https://h/certificate/redirect/': returned error: 500"
    assert _cause(output) == NOT_CLONED


# ----------------------------------------------------------- hosts on the loopback
@dataclass
class Host:
    """An HTTPS server of a test and the Authorization header of every request it received."""

    port: int
    authorizations: list[str | None] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"https://localhost:{self.port}/acme/app.git"

    @property
    def contacted(self) -> bool:
        return bool(self.authorizations)


Respond = Callable[[http.server.BaseHTTPRequestHandler], None]


def answering(
    status: int, body: bytes = b"", content_type: str = "text/plain", **headers: str
) -> Respond:
    def respond(request: http.server.BaseHTTPRequestHandler) -> None:
        request.send_response(status)
        for name, value in {"Content-Type": content_type, **headers}.items():
            request.send_header(name.replace("_", "-"), value)
        request.send_header("Content-Length", str(len(body)))
        request.end_headers()
        request.wfile.write(body)

    return respond


@pytest.fixture(scope="module")
def certificate(tmp_path_factory) -> Path:
    """A self-signed certificate for localhost, with its key in the same file."""
    if shutil.which("openssl") is None:
        pytest.skip("needs openssl to make a certificate for the test servers")
    pem = tmp_path_factory.mktemp("tls") / "localhost.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
         "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
         "-keyout", str(pem), "-out", str(pem)],
        check=True, capture_output=True,
    )  # fmt: skip
    return pem


@pytest.fixture
def serve(certificate, monkeypatch) -> Iterator[Callable[..., Host]]:
    """Start HTTPS servers whose certificate git trusts, configured the way an operator would."""
    monkeypatch.setenv("GIT_SSL_CAINFO", str(certificate))
    servers: list[http.server.HTTPServer] = []

    def start(get: Respond, post: Respond | None = None, *, tls: bool = True) -> Host:
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                host.authorizations.append(self.headers.get("Authorization"))
                get(self)

            def do_POST(self) -> None:
                host.authorizations.append(self.headers.get("Authorization"))
                (post or get)(self)

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certificate)
            server.socket = context.wrap_socket(server.socket, server_side=True)
        host = Host(port=server.server_address[1])
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return host

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def advertisement(tmp_path) -> bytes:
    """What a real repository answers to git's first request, so that git makes a second one."""
    repo = tmp_path / "served"
    repo.mkdir()
    env = _git_env(None, repo.as_uri())
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "first"]):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
            cwd=repo, check=True, capture_output=True, env=env,
        )  # fmt: skip
    refs = subprocess.run(
        ["git", "upload-pack", "--stateless-rpc", "--advertise-refs", str(repo)],
        check=True, capture_output=True, env=env,
    ).stdout  # fmt: skip
    service = b"# service=git-upload-pack\n"
    return b"%04x" % (len(service) + 4) + service + b"0000" + refs


def refused(url: str, dest: Path, tokens: RepositoryTokens | None = None) -> str:
    with pytest.raises(IntakeError) as failure:
        clone(url, dest / "clone", tokens=tokens or RepositoryTokens({}))
    return str(failure.value)


# --------------------------------------------------------- no password from anywhere
@pytest.fixture
def askpass(tmp_path, monkeypatch) -> Path:
    """A password program in the environment of this process; it leaves a mark when it is run."""
    mark = tmp_path / "askpass-was-run"
    program = tmp_path / "askpass.sh"
    program.write_text(f"#!/bin/sh\necho run >> '{mark}'\necho credential-of-the-machine\n")
    program.chmod(0o755)
    monkeypatch.setenv("GIT_ASKPASS", str(program))
    monkeypatch.setenv("SSH_ASKPASS", str(program))
    return mark


def test_host_that_demands_a_password_gets_none(serve, askpass, tmp_path):
    host = serve(answering(401, WWW_Authenticate='Basic realm="repository"'))
    assert refused(host.url, tmp_path) == NOT_FOUND
    assert not askpass.exists()
    assert host.contacted
    assert set(host.authorizations) == {None}


def test_password_program_would_answer_the_host_if_git_inherited_the_environment(
    serve, askpass, tmp_path
):
    # Why git's environment is built from nothing: this is git with the environment of
    # the process and prompts switched off, as it was run before.
    host = serve(answering(401, WWW_Authenticate='Basic realm="repository"'))
    inherited = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}
    inherited["GIT_CONFIG_GLOBAL"] = "/dev/null"
    subprocess.run(
        ["git", "clone", "--", host.url, str(tmp_path / "clone")],
        env=inherited, capture_output=True, check=False, timeout=60,
    )  # fmt: skip
    assert askpass.exists()
    assert any(header is not None for header in host.authorizations)


def test_certificate_check_cannot_be_switched_off_from_the_environment(
    serve, monkeypatch, tmp_path
):
    host = serve(answering(404))
    monkeypatch.delenv("GIT_SSL_CAINFO")
    monkeypatch.setenv("GIT_SSL_NO_VERIFY", "1")
    assert refused(host.url, tmp_path) == NOT_SECURE
    assert not host.contacted


# ------------------------------------------------------------------- redirects
def redirecting_to(target: str) -> Respond:
    def respond(request: http.server.BaseHTTPRequestHandler) -> None:
        answering(302, Location=f"{target}{request.path}")(request)

    return respond


def test_redirect_is_not_followed_without_a_token(serve, tmp_path):
    target = serve(answering(404, HOST_TEXT.encode()))
    origin = serve(redirecting_to(f"https://localhost:{target.port}"))
    assert refused(origin.url, tmp_path) == REDIRECTED
    assert origin.contacted
    assert not target.contacted


def test_redirect_to_plain_http_is_not_followed(serve, tmp_path):
    target = serve(answering(404, HOST_TEXT.encode()), tls=False)
    origin = serve(redirecting_to(f"http://127.0.0.1:{target.port}"))
    assert refused(origin.url, tmp_path) == REDIRECTED
    assert not target.contacted


def test_plain_http_is_not_contacted_even_if_redirects_were_followed(serve, tmp_path):
    # The second lock: with redirects allowed again, the transport limit still holds.
    target = serve(answering(404, HOST_TEXT.encode()), tls=False)
    origin = serve(redirecting_to(f"http://127.0.0.1:{target.port}"))
    env = following_redirects(_git_env(None, origin.url))
    git_clone(origin.url, tmp_path / "clone", env)
    assert origin.contacted
    assert not target.contacted


def test_token_does_not_follow_a_redirect_to_another_host(serve, tmp_path):
    target = serve(answering(404))
    origin = serve(redirecting_to(f"https://localhost:{target.port}"))
    tokens = RepositoryTokens({f"localhost:{origin.port}": GITHUB})
    assert refused(origin.url, tmp_path, tokens) == REDIRECTED
    assert origin.authorizations == [f"Basic {basic(GITHUB)}"]
    assert not target.contacted


def test_scoping_the_header_alone_would_not_stop_it(serve, advertisement, tmp_path):
    # Why redirects are refused: with the header configured for the first host only,
    # git still repeats it to the second host once it has been redirected there.
    target = serve(
        answering(200, advertisement, "application/x-git-upload-pack-advertisement"),
        answering(500),
    )
    origin = serve(redirecting_to(f"https://localhost:{target.port}"))
    env = following_redirects(_git_env(GITHUB, origin.url))
    git_clone(origin.url, tmp_path / "clone", env)
    assert f"Basic {basic(GITHUB)}" in target.authorizations


def following_redirects(env: dict[str, str]) -> dict[str, str]:
    """The environment with one lock taken off, to show what the other one holds."""
    key = next(name for name, value in env.items() if value == "http.followRedirects")
    return {**env, key.replace("KEY", "VALUE"): "true"}


def git_clone(url: str, dest: Path, env: dict[str, str]) -> None:
    subprocess.run(
        ["git", "clone", "--depth", "1", "--", url, str(dest)],
        env=env, capture_output=True, check=False, timeout=60,
    )  # fmt: skip


def test_no_transport_but_https_is_allowed_for_a_remote_repository(tmp_path, advertisement):
    served = (tmp_path / "served").as_uri()
    for url in (served, f"ext::sh -c 'touch {tmp_path}/ran'", "ssh://localhost/acme/app.git"):
        done = subprocess.run(
            ["git", "clone", "--", url, str(tmp_path / "clone")],
            env=_git_env(None, REPOSITORY), capture_output=True, text=True, check=False, timeout=60,
        )  # fmt: skip
        assert "not allowed" in done.stderr
    assert not (tmp_path / "ran").exists()


# ----------------------------------------------- what the host writes stays in the log
def test_text_written_by_the_host_is_not_in_the_error(serve, tmp_path):
    host = serve(answering(404, HOST_TEXT.encode()))
    assert refused(host.url, tmp_path) == NOT_FOUND


def test_text_written_by_the_host_is_logged_for_the_operator(serve, tmp_path, caplog):
    host = serve(answering(403, HOST_TEXT.encode()))
    with caplog.at_level(logging.WARNING, logger=intake_git.__name__):
        refused(host.url, tmp_path)
    assert HOST_TEXT in caplog.text


def test_token_a_host_echoes_back_is_not_logged(serve, tmp_path, caplog):
    def echo(request: http.server.BaseHTTPRequestHandler) -> None:
        answering(403, f"you sent {request.headers.get('Authorization')}".encode())(request)

    host = serve(echo)
    tokens = RepositoryTokens({f"localhost:{host.port}": GITHUB})
    with caplog.at_level(logging.WARNING, logger=intake_git.__name__):
        assert refused(host.url, tmp_path, tokens) == NOT_FOUND
    assert "you sent Basic [REDACTED]" in caplog.text
    assert basic(GITHUB) not in caplog.text


def test_bytes_that_are_not_text_do_not_break_the_clone(serve, tmp_path):
    host = serve(answering(403, b"\xff\xfe\x80 not utf-8"))
    assert refused(host.url, tmp_path) == NOT_FOUND


def test_error_does_not_name_the_folder_or_the_git_command(serve, tmp_path):
    host = serve(answering(404))
    message = refused(host.url, tmp_path)
    assert str(tmp_path) not in message
    assert "git" not in message.split()


# --------------------------------------------------------------- other causes
def test_untrusted_certificate_is_reported_as_such(serve, monkeypatch, tmp_path):
    host = serve(answering(404))
    monkeypatch.delenv("GIT_SSL_CAINFO")
    assert refused(host.url, tmp_path) == NOT_SECURE


def test_host_that_does_not_answer_is_reported_as_unreachable(tmp_path):
    assert refused("https://localhost:1/acme/app.git", tmp_path) == HOST_UNREACHABLE


def test_clone_that_takes_too_long_is_stopped(serve, monkeypatch, tmp_path):
    def slow(request: http.server.BaseHTTPRequestHandler) -> None:
        time.sleep(3)
        answering(404)(request)

    host = serve(slow)
    monkeypatch.setattr(intake_git, "CLONE_TIMEOUT_S", 1)
    assert refused(host.url, tmp_path) == CLONE_TIMED_OUT
