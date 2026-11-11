"""Tests for how an attempt gets the installation's credentials for the workspace of its run.

They need PostgreSQL (see the root ``conftest.py``). Keys and tokens are made up. The
model configuration is the repository's, read with an environment of the test's own.
"""

import uuid
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from openmarketer_core.credentials.catalog import GITHUB_TOKEN, GITLAB_TOKEN, OPENROUTER_API_KEY
from openmarketer_core.credentials.resolution import CredentialUnreadable, ModelKeyNotSet
from openmarketer_core.db.projects import create_project
from openmarketer_core.db.session import DatabaseError, session_factory, transaction
from openmarketer_core.llm_config import ModelRouter
from openmarketer_core.social_accounts.catalog import AccountDescription, SocialPlatform
from openmarketer_core.social_accounts.connection import (
    AuthorizationStart,
    AuthorizedAccount,
    ConnectionSecretUnreadable,
    PlatformApp,
    finish_connection,
    start_connection,
)
from openmarketer_worker.credentials import WorkspaceCredentials

MODELS = Path(__file__).resolve().parents[3] / "config" / "models.yaml"
STORED_KEY = "sk-stored-0123456789"
ENVIRONMENT_KEY = "sk-environment-0123456789"
NOWHERE = "postgresql+psycopg://u:p@127.0.0.1:1/db"


def router(environ: dict[str, str]) -> ModelRouter:
    return ModelRouter.from_file(MODELS, env=environ)


def router_naming(api_key_env: str, environ: dict[str, str]) -> ModelRouter:
    named = router(environ)
    named.config.provider.api_key_env = api_key_env
    return named


@pytest.fixture
def workspace_id(database) -> uuid.UUID:
    return database.new_workspace()


@pytest.fixture
def stored_key(database, workspace_id, cipher) -> str:
    database.store_credential(workspace_id, OPENROUTER_API_KEY, STORED_KEY, cipher)
    return STORED_KEY


# ------------------------------------------------------- the model provider's key
def test_key_of_the_environment_is_the_models_key(sessions, workspace_id, cipher):
    environ = {"OPENROUTER_API_KEY": ENVIRONMENT_KEY}
    credentials = WorkspaceCredentials(sessions, environ, cipher)
    assert credentials.model_key(workspace_id, router(environ)) == ENVIRONMENT_KEY


def test_key_stored_for_the_workspace_is_used_when_the_environment_has_none(
    sessions, workspace_id, cipher, stored_key
):
    credentials = WorkspaceCredentials(sessions, {}, cipher)
    assert credentials.model_key(workspace_id, router({})) == stored_key


def test_environment_wins_over_the_key_stored_for_the_workspace(
    sessions, workspace_id, cipher, stored_key
):
    environ = {"OPENROUTER_API_KEY": ENVIRONMENT_KEY}
    credentials = WorkspaceCredentials(sessions, environ, cipher)
    assert credentials.model_key(workspace_id, router(environ)) == ENVIRONMENT_KEY


def test_key_stored_after_the_credentials_were_built_is_found_the_next_time(
    sessions, database, workspace_id, cipher
):
    credentials = WorkspaceCredentials(sessions, {}, cipher)
    with pytest.raises(ModelKeyNotSet):
        credentials.model_key(workspace_id, router({}))
    database.store_credential(workspace_id, OPENROUTER_API_KEY, STORED_KEY, cipher)
    assert credentials.model_key(workspace_id, router({})) == STORED_KEY


def test_key_stored_for_another_workspace_is_not_this_ones(sessions, database, cipher, stored_key):
    credentials = WorkspaceCredentials(sessions, {}, cipher)
    with pytest.raises(ModelKeyNotSet):
        credentials.model_key(database.new_workspace(), router({}))


def test_no_key_anywhere_says_where_one_can_be_set(sessions, workspace_id, cipher):
    credentials = WorkspaceCredentials(sessions, {}, cipher)
    with pytest.raises(ModelKeyNotSet) as refused:
        credentials.model_key(workspace_id, router({}))
    assert str(refused.value) == (
        "the model provider's API key is not set: set it in the dashboard's Settings,"
        " or with the environment variable OPENROUTER_API_KEY"
    )


def test_key_padded_with_whitespace_in_the_environment_is_used_without_it(
    sessions, workspace_id, cipher
):
    environ = {"OPENROUTER_API_KEY": f" {ENVIRONMENT_KEY}\n"}
    credentials = WorkspaceCredentials(sessions, environ, cipher)
    assert credentials.model_key(workspace_id, router(environ)) == ENVIRONMENT_KEY


def test_whitespace_in_the_environment_is_no_key_so_the_stored_one_is_used(
    sessions, workspace_id, cipher, stored_key
):
    environ = {"OPENROUTER_API_KEY": " "}
    credentials = WorkspaceCredentials(sessions, environ, cipher)
    assert credentials.model_key(workspace_id, router(environ)) == stored_key


# ------------------------------------------- a configuration that names another variable
def test_another_variable_the_model_configuration_names_holds_the_key(
    sessions, workspace_id, cipher, stored_key
):
    environ = {"PROVIDER_KEY": "sk-named-by-the-configuration", "OPENROUTER_API_KEY": ""}
    credentials = WorkspaceCredentials(sessions, environ, cipher)
    key = credentials.model_key(workspace_id, router_naming("PROVIDER_KEY", environ))
    assert key == "sk-named-by-the-configuration"


def test_catalogs_variable_is_not_the_key_of_a_configuration_that_names_another(
    sessions, workspace_id, cipher
):
    environ = {"OPENROUTER_API_KEY": ENVIRONMENT_KEY}
    credentials = WorkspaceCredentials(sessions, environ, cipher)
    with pytest.raises(ModelKeyNotSet):
        credentials.model_key(workspace_id, router_naming("PROVIDER_KEY", environ))


def test_stored_key_is_not_the_key_of_a_configuration_that_names_another_variable(
    sessions, workspace_id, cipher, stored_key
):
    credentials = WorkspaceCredentials(sessions, {}, cipher)
    with pytest.raises(ModelKeyNotSet):
        credentials.model_key(workspace_id, router_naming("PROVIDER_KEY", {}))


def test_another_variable_that_is_not_set_is_named_without_pointing_at_settings(
    sessions, workspace_id, cipher, stored_key
):
    environ = {"OPENROUTER_API_KEY": ENVIRONMENT_KEY}
    credentials = WorkspaceCredentials(sessions, environ, cipher)
    with pytest.raises(ModelKeyNotSet) as refused:
        credentials.model_key(workspace_id, router_naming("PROVIDER_KEY", environ))
    assert str(refused.value) == (
        "the model provider's API key is not set: set the environment variable PROVIDER_KEY"
    )


# ------------------------------------------------------------ what cannot be read
def test_stored_key_is_unreadable_without_an_encryption_key(sessions, workspace_id, stored_key):
    credentials = WorkspaceCredentials(sessions, {}, None)
    with pytest.raises(CredentialUnreadable, match="openrouter_api_key cannot be read") as refused:
        credentials.model_key(workspace_id, router({}))
    assert stored_key not in str(refused.value)


def test_stored_key_is_unreadable_under_another_encryption_key(
    sessions, workspace_id, stored_key, another_cipher
):
    credentials = WorkspaceCredentials(sessions, {}, another_cipher)
    with pytest.raises(CredentialUnreadable) as refused:
        credentials.model_key(workspace_id, router({}))
    assert stored_key not in str(refused.value)
    assert refused.value.__cause__ is None


def test_stored_key_that_cannot_be_read_does_not_matter_while_the_environment_has_one(
    sessions, workspace_id, stored_key
):
    environ = {"OPENROUTER_API_KEY": ENVIRONMENT_KEY}
    credentials = WorkspaceCredentials(sessions, environ, None)
    assert credentials.model_key(workspace_id, router(environ)) == ENVIRONMENT_KEY


def test_database_that_cannot_be_reached_is_a_database_error():
    credentials = WorkspaceCredentials(session_factory(NOWHERE), {}, None)
    with pytest.raises(DatabaseError):
        credentials.model_key(uuid.uuid4(), router({}))


# ------------------------------------------------------------- any credential
def test_values_are_given_for_each_credential_asked_for(sessions, database, workspace_id, cipher):
    database.store_credential(workspace_id, GITHUB_TOKEN, "ghp_stored0123456789", cipher)
    credentials = WorkspaceCredentials(sessions, {"GITLAB_TOKEN": "glpat-environment"}, cipher)
    assert credentials.values(workspace_id, [GITHUB_TOKEN, GITLAB_TOKEN, OPENROUTER_API_KEY]) == {
        GITHUB_TOKEN: "ghp_stored0123456789",
        GITLAB_TOKEN: "glpat-environment",
        OPENROUTER_API_KEY: None,
    }


def test_no_credential_is_in_the_text_of_the_credentials(sessions: sessionmaker, cipher):
    environ = {"OPENROUTER_API_KEY": ENVIRONMENT_KEY, "GITHUB_TOKEN": "ghp_environment"}
    credentials = WorkspaceCredentials(sessions, environ, cipher)
    shown = repr(credentials) + str(credentials) + repr(vars(credentials)["_cipher"])
    assert ENVIRONMENT_KEY not in shown and "ghp_environment" not in shown


# ------------------------------------------------------- a connected account's token
ACCESS_TOKEN = "42-x-access-token-0123456789"
ACCESS_SECRET = "x-access-secret-0123456789"
X_APP = {"X_API_KEY": "x-app-key", "X_API_SECRET": "x-app-secret"}


class Authorization:
    """An ``AccountAuthorization`` that authorizes account 42 with the test's token."""

    def start(self, app: PlatformApp, *, callback_url: str) -> AuthorizationStart:
        return AuthorizationStart(authorize_url="https://x.example/", state="s", secret_to_keep="k")

    def finish(self, app, *, state: str, grant: str, kept_secret: str) -> AuthorizedAccount:
        account = AccountDescription("42", "acme", "Acme", None)
        return AuthorizedAccount(account, access_token=ACCESS_TOKEN, access_secret=ACCESS_SECRET)


@pytest.fixture
def project_id(sessions, workspace_id) -> uuid.UUID:
    with transaction(sessions) as session:
        return create_project(
            session, workspace_id=workspace_id, name="app", source_repo_url="https://x.example/a"
        ).id


def connect(sessions, workspace_id: uuid.UUID, project_id: uuid.UUID, cipher) -> None:
    with transaction(sessions) as session:
        start_connection(
            session,
            X_APP,
            workspace_id=workspace_id,
            project_id=project_id,
            platform=SocialPlatform.X,
            callback_url="http://localhost/callback",
            authorization=Authorization(),
            cipher=cipher,
        )
        finish_connection(
            session,
            X_APP,
            workspace_id=workspace_id,
            platform=SocialPlatform.X,
            state="s",
            grant="v",
            connected_by=uuid.uuid4(),
            authorization=Authorization(),
            cipher=cipher,
        )


def test_token_of_the_projects_account_is_opened_with_the_account_it_belongs_to(
    sessions, workspace_id, project_id, cipher
):
    connect(sessions, workspace_id, project_id, cipher)

    found = WorkspaceCredentials(sessions, {}, cipher).account_tokens(
        workspace_id, project_id, SocialPlatform.X
    )

    assert found is not None and found.account_id == "42"
    assert (found.tokens.access_token, found.tokens.access_secret) == (ACCESS_TOKEN, ACCESS_SECRET)
    assert ACCESS_TOKEN not in repr(found) and ACCESS_SECRET not in repr(found)


def test_project_without_an_account_has_no_token(sessions, workspace_id, project_id, cipher):
    credentials = WorkspaceCredentials(sessions, {}, cipher)

    assert credentials.account_tokens(workspace_id, project_id, SocialPlatform.X) is None


def test_token_of_another_workspace_is_not_found(
    sessions, database, workspace_id, project_id, cipher
):
    connect(sessions, workspace_id, project_id, cipher)
    other = database.new_workspace()
    credentials = WorkspaceCredentials(sessions, {}, cipher)

    assert credentials.account_tokens(other, project_id, SocialPlatform.X) is None


def test_token_sealed_under_another_encryption_key_is_unreadable(
    sessions, workspace_id, project_id, cipher, another_cipher
):
    connect(sessions, workspace_id, project_id, cipher)
    credentials = WorkspaceCredentials(sessions, {}, another_cipher)

    with pytest.raises(ConnectionSecretUnreadable):
        credentials.account_tokens(workspace_id, project_id, SocialPlatform.X)
