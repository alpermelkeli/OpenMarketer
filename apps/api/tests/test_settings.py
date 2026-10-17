"""Tests for the settings the API process reads at start-up."""

import base64
from pathlib import Path

import pytest

from openmarketer_api.dependencies import Services
from openmarketer_api.settings import Settings, SettingsError
from openmarketer_core.credentials.cipher import CredentialCipher
from openmarketer_core.llm_config import ModelRouter
from openmarketer_core.workflow_server import WorkflowServer

DATABASE = {"DATABASE_URL": "postgresql+psycopg://u:p@localhost/db"}
MODELS_FILE = Path(__file__).resolve().parents[3] / "config" / "models.yaml"


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


def test_services_do_not_show_the_environment_when_printed(no_catalog, page_store):
    services = Services(
        sessions=None,  # pyright: ignore[reportArgumentType]
        workflows=None,  # pyright: ignore[reportArgumentType]
        environment={"GITHUB_TOKEN": "ghp_made_up_value"},
        credential_cipher=None,
        model_router=ModelRouter.from_file(MODELS_FILE, env={"GITHUB_TOKEN": "ghp_made_up_value"}),
        model_catalog=no_catalog,
        page_store=page_store,
    )
    assert "ghp_made_up_value" not in repr(services)


def test_model_configuration_is_read_with_the_environment_over_it():
    settings = Settings.from_env(
        {**DATABASE, "LLM_CONFIG_PATH": str(MODELS_FILE), "LLM_MODEL_FAST": "openai/env-fast"}
    )
    assert settings.model_router.tier_model("fast") == ("openai/env-fast", "env-tier:fast")


def test_start_without_a_readable_model_configuration_is_refused(tmp_path):
    missing = tmp_path / "models.yaml"
    with pytest.raises(SettingsError, match="cannot be read"):
        Settings.from_env({**DATABASE, "LLM_CONFIG_PATH": str(missing)})


def test_start_with_a_model_configuration_that_is_not_valid_is_refused(tmp_path):
    broken = tmp_path / "models.yaml"
    broken.write_text("tiers: {fast: not-a-slug}\ndefaults: {model: a/b}\nroles: {}\n")
    with pytest.raises(SettingsError, match="is not valid"):
        Settings.from_env({**DATABASE, "LLM_CONFIG_PATH": str(broken)})


def test_settings_do_not_show_the_environment_through_the_model_configuration():
    settings = Settings.from_env({**DATABASE, "OPENROUTER_API_KEY": "sk-made-up-value"})
    assert "sk-made-up-value" not in repr(settings)


def test_tracing_is_off_without_langfuses_keys():
    assert Settings.from_env(DATABASE).tracing.enabled is False


def test_tracing_is_on_with_both_of_langfuses_keys():
    settings = Settings.from_env(
        {**DATABASE, "LANGFUSE_PUBLIC_KEY": "pk-made-up", "LANGFUSE_SECRET_KEY": "sk-made-up"}
    )
    assert settings.tracing.enabled is True
    assert "made-up" not in repr(settings)


@pytest.mark.parametrize(
    ("given", "missing"),
    [
        ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"),
        ("LANGFUSE_SECRET_KEY", "LANGFUSE_PUBLIC_KEY"),
    ],
)
def test_start_with_one_of_langfuses_keys_is_refused_naming_the_missing_one(given, missing):
    with pytest.raises(SettingsError, match=missing) as refused:
        Settings.from_env({**DATABASE, given: "pk-made-up-value"})
    assert "pk-made-up-value" not in str(refused.value)
