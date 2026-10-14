"""Tests for the settings the API process reads at start-up."""

import base64

import pytest

from openmarketer_api.dependencies import Services
from openmarketer_api.settings import Settings, SettingsError
from openmarketer_core.credentials.cipher import CredentialCipher
from openmarketer_core.workflow_server import WorkflowServer

DATABASE = {"DATABASE_URL": "postgresql+psycopg://u:p@localhost/db"}


def test_database_url_is_read_from_the_environment():
    assert Settings.from_env(DATABASE).database_url == DATABASE["DATABASE_URL"]


@pytest.mark.parametrize("environ", [{}, {"DATABASE_URL": ""}])
def test_start_without_a_database_url_is_refused(environ):
    with pytest.raises(SettingsError, match="DATABASE_URL"):
        Settings.from_env(environ)


def test_workflow_server_defaults_to_the_dev_stack_as_seen_from_the_host():
    assert Settings.from_env(DATABASE).workflow_server == WorkflowServer(
        address="localhost:7233", namespace="default"
    )


def test_workflow_server_is_read_from_the_environment():
    settings = Settings.from_env(
        {**DATABASE, "TEMPORAL_ADDRESS": "temporal.internal:7233", "TEMPORAL_NAMESPACE": "team"}
    )
    assert settings.workflow_server == WorkflowServer(
        address="temporal.internal:7233", namespace="team"
    )


@pytest.mark.parametrize("environ", [DATABASE, {**DATABASE, "ENCRYPTION_KEY": ""}])
def test_without_an_encryption_key_there_is_no_cipher(environ):
    assert Settings.from_env(environ).credential_cipher is None


def test_encryption_key_gives_the_cipher_credentials_are_stored_with():
    key = base64.b64encode(bytes(range(32))).decode()
    settings = Settings.from_env({**DATABASE, "ENCRYPTION_KEY": key})
    assert isinstance(settings.credential_cipher, CredentialCipher)


@pytest.mark.parametrize(
    ("key", "reason"),
    [
        ("not base64 !!", "ENCRYPTION_KEY is not base64"),
        (base64.b64encode(b"sixteen byte key").decode(), "ENCRYPTION_KEY is 16 bytes, not 32"),
    ],
)
def test_start_with_an_encryption_key_that_is_not_one_is_refused(key, reason):
    with pytest.raises(SettingsError, match=reason) as refused:
        Settings.from_env({**DATABASE, "ENCRYPTION_KEY": key})
    assert key not in str(refused.value)
    assert "openssl rand -base64 32" in str(refused.value)


def test_settings_do_not_show_the_encryption_key_when_printed():
    key = base64.b64encode(bytes(range(32))).decode()
    assert key not in repr(Settings.from_env({**DATABASE, "ENCRYPTION_KEY": key}))


def test_services_do_not_show_the_environment_when_printed():
    services = Services(
        sessions=None,  # pyright: ignore[reportArgumentType]
        analysis_workflows=None,  # pyright: ignore[reportArgumentType]
        run_signals=None,  # pyright: ignore[reportArgumentType]
        environment={"GITHUB_TOKEN": "ghp_made_up_value"},
        credential_cipher=None,
    )
    assert "ghp_made_up_value" not in repr(services)
