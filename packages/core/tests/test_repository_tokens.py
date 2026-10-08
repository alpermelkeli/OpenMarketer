"""Tests for the rule that an access token goes only to the host it belongs to.

How git is then run so that the rule holds against a hostile host is tested
in ``test_clone_lockdown.py``.
"""

import pytest

from openmarketer_core.intake import IntakeError, RepositoryTokens

GITHUB = "github-token-value"
GITLAB = "gitlab-token-value"
TOKENS = RepositoryTokens({"github.com": GITHUB, "gitlab.com": GITLAB})


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
