"""Tests for the repository tokens a clone of an analysis may carry, for its run's workspace.

They need PostgreSQL (see the root ``conftest.py``). Tokens are made up. Which host a token
is offered to is core's rule, tested there; here, that the resolved values reach it.
"""

import uuid

import pytest

from openmarketer_core.credentials.catalog import GITHUB_TOKEN, GITLAB_TOKEN
from openmarketer_core.credentials.resolution import CredentialUnreadable
from openmarketer_core.repository_analysis.intake import IntakeError
from openmarketer_worker.credentials import WorkspaceCredentials
from openmarketer_worker.repository_analyzer.repository_tokens import WorkspaceRepositoryTokens

STORED_GITHUB = "ghp_stored0123456789"
STORED_GITLAB = "glpat-stored0123456789"
ON_GITHUB = "https://github.com/acme/app.git"
ON_GITLAB = "https://gitlab.com/acme/app.git"
ELSEWHERE = "https://example.com/acme/app.git"


@pytest.fixture
def workspace_id(database) -> uuid.UUID:
    return database.new_workspace()


@pytest.fixture
def tokens_with(sessions, cipher):
    """The tokens as a worker with that environment resolves them."""

    def tokens(environ: dict[str, str], cipher=cipher) -> WorkspaceRepositoryTokens:
        return WorkspaceRepositoryTokens(WorkspaceCredentials(sessions, environ, cipher), environ)

    return tokens


def test_stored_github_token_is_offered_to_github_and_to_no_other_host(
    tokens_with, database, workspace_id, cipher
):
    database.store_credential(workspace_id, GITHUB_TOKEN, STORED_GITHUB, cipher)
    tokens = tokens_with({}).of(workspace_id)
    assert tokens.token_for(ON_GITHUB) == STORED_GITHUB
    assert (tokens.token_for(ON_GITLAB), tokens.token_for(ELSEWHERE)) == (None, None)


def test_stored_gitlab_token_is_offered_to_gitlab(tokens_with, database, workspace_id, cipher):
    database.store_credential(workspace_id, GITLAB_TOKEN, STORED_GITLAB, cipher)
    tokens = tokens_with({}).of(workspace_id)
    assert (tokens.token_for(ON_GITLAB), tokens.token_for(ON_GITHUB)) == (STORED_GITLAB, None)


def test_stored_gitlab_token_goes_to_the_host_the_environment_names(
    tokens_with, database, workspace_id, cipher
):
    database.store_credential(workspace_id, GITLAB_TOKEN, STORED_GITLAB, cipher)
    tokens = tokens_with({"GITLAB_HOST": "gitlab.example.com"}).of(workspace_id)
    assert tokens.token_for("https://gitlab.example.com/acme/app.git") == STORED_GITLAB
    assert tokens.token_for(ON_GITLAB) is None


def test_token_of_the_environment_wins_over_the_stored_one(
    tokens_with, database, workspace_id, cipher
):
    database.store_credential(workspace_id, GITHUB_TOKEN, STORED_GITHUB, cipher)
    tokens = tokens_with({"GITHUB_TOKEN": "ghp_environment0123456789"}).of(workspace_id)
    assert tokens.token_for(ON_GITHUB) == "ghp_environment0123456789"


def test_token_stored_after_the_worker_started_is_offered_by_the_next_clone(
    tokens_with, database, workspace_id, cipher
):
    tokens = tokens_with({})
    assert tokens.of(workspace_id).token_for(ON_GITHUB) is None
    database.store_credential(workspace_id, GITHUB_TOKEN, STORED_GITHUB, cipher)
    assert tokens.of(workspace_id).token_for(ON_GITHUB) == STORED_GITHUB


def test_token_of_another_workspace_is_not_offered(tokens_with, database, workspace_id, cipher):
    database.store_credential(workspace_id, GITHUB_TOKEN, STORED_GITHUB, cipher)
    assert tokens_with({}).of(database.new_workspace()).token_for(ON_GITHUB) is None


def test_workspace_without_tokens_clones_without_credentials(tokens_with, workspace_id):
    tokens = tokens_with({}).of(workspace_id)
    assert (tokens.token_for(ON_GITHUB), tokens.token_for(ON_GITLAB)) == (None, None)


def test_stored_token_that_cannot_be_read_is_refused_without_its_value(
    tokens_with, database, workspace_id, cipher, another_cipher
):
    database.store_credential(workspace_id, GITHUB_TOKEN, STORED_GITHUB, cipher)
    with pytest.raises(CredentialUnreadable, match="github_token cannot be read") as refused:
        tokens_with({}, cipher=another_cipher).of(workspace_id)
    assert STORED_GITHUB not in str(refused.value)


def test_token_host_that_is_not_a_host_is_refused_when_the_tokens_are_built(tokens_with):
    with pytest.raises(IntakeError, match="GITLAB_HOST"):
        tokens_with({"GITLAB_HOST": "https://gitlab.example.com"})


def test_no_token_is_in_the_text_of_the_tokens(tokens_with, database, workspace_id, cipher):
    database.store_credential(workspace_id, GITHUB_TOKEN, STORED_GITHUB, cipher)
    tokens = tokens_with({"GITLAB_TOKEN": "glpat-environment"})
    shown = repr(tokens) + repr(tokens.of(workspace_id))
    assert STORED_GITHUB not in shown and "glpat-environment" not in shown
