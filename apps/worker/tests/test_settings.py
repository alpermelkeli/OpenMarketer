"""Tests for the settings of the worker process itself, read at start-up.

The settings of an agent are tested in its folder.
"""

import pytest

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
