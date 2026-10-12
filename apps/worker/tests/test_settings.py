"""Tests for the settings of the worker process itself, read at start-up.

The settings of an agent are tested in its folder.
"""

import base64
import os

import pytest

from openmarketer_core.credentials.cipher import CredentialCipher, EncryptionKeyInvalid
from openmarketer_worker.settings import Settings, SettingsError

DATABASE = {"DATABASE_URL": "postgresql+psycopg://u:p@localhost/db"}


def test_database_url_is_read_from_the_environment():
    assert Settings.from_env(DATABASE).database_url == DATABASE["DATABASE_URL"]


@pytest.mark.parametrize("environ", [{}, {"DATABASE_URL": ""}])
def test_start_without_a_database_url_is_refused(environ):
    with pytest.raises(SettingsError, match="DATABASE_URL"):
        Settings.from_env(environ)


def test_temporal_defaults_to_the_dev_stack_as_seen_from_the_host():
    settings = Settings.from_env(DATABASE)
    assert (settings.temporal_address, settings.temporal_namespace) == ("localhost:7233", "default")


def test_temporal_address_and_namespace_are_read_from_the_environment():
    settings = Settings.from_env(
        {
            **DATABASE,
            "TEMPORAL_ADDRESS": "temporal.internal:7233",
            "TEMPORAL_NAMESPACE": "marketing",
        }
    )
    assert (settings.temporal_address, settings.temporal_namespace) == (
        "temporal.internal:7233",
        "marketing",
    )


def test_activities_run_at_once_have_a_default():
    assert Settings.from_env(DATABASE).max_concurrent_activities == 4


def test_activities_run_at_once_are_read_from_the_environment():
    settings = Settings.from_env({**DATABASE, "WORKER_MAX_CONCURRENT_ACTIVITIES": "8"})
    assert settings.max_concurrent_activities == 8


@pytest.mark.parametrize("value", ["0", "-1", "soon"])
def test_limit_that_is_not_a_positive_number_is_refused_by_name(value):
    with pytest.raises(SettingsError, match="WORKER_MAX_CONCURRENT_ACTIVITIES"):
        Settings.from_env({**DATABASE, "WORKER_MAX_CONCURRENT_ACTIVITIES": value})


# ------------------------------------------------------- the encryption key
def made_up_key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


@pytest.mark.parametrize("environ", [{}, {"ENCRYPTION_KEY": ""}, {"ENCRYPTION_KEY": "  "}])
def test_installation_without_an_encryption_key_has_no_cipher(environ):
    assert Settings.from_env({**DATABASE, **environ}).credential_cipher is None


def test_encryption_key_of_the_environment_makes_the_cipher_of_stored_credentials():
    cipher = Settings.from_env({**DATABASE, "ENCRYPTION_KEY": made_up_key()}).credential_cipher
    assert isinstance(cipher, CredentialCipher)


@pytest.mark.parametrize(
    "key", ["not base64!", base64.b64encode(b"too short").decode()], ids=["text", "short"]
)
def test_encryption_key_that_is_not_one_is_refused_without_repeating_it(key):
    with pytest.raises(EncryptionKeyInvalid, match="ENCRYPTION_KEY") as refused:
        Settings.from_env({**DATABASE, "ENCRYPTION_KEY": key})
    assert key not in str(refused.value)


def test_encryption_key_is_not_in_the_text_of_the_settings():
    key = made_up_key()
    settings = Settings.from_env({**DATABASE, "ENCRYPTION_KEY": key})
    assert key not in repr(settings)
