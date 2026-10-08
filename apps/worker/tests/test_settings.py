"""Tests for the settings the worker process reads at start-up."""

import pytest

from openmarketer_worker.policy import AnalysisPolicy
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


def test_analysis_limits_have_defaults():
    assert Settings.from_env(DATABASE).analysis_policy == AnalysisPolicy()


def test_analysis_limits_are_read_from_the_environment():
    settings = Settings.from_env(
        {**DATABASE, "ANALYSIS_TIMEOUT_MINUTES": "45", "ANALYSIS_MAX_ATTEMPTS": "1"}
    )
    assert settings.analysis_policy.attempt_timeout_seconds == 45 * 60
    assert settings.analysis_policy.max_attempts == 1


@pytest.mark.parametrize(
    "name",
    ["ANALYSIS_TIMEOUT_MINUTES", "ANALYSIS_MAX_ATTEMPTS", "WORKER_MAX_CONCURRENT_ACTIVITIES"],
)
@pytest.mark.parametrize("value", ["0", "-1", "soon"])
def test_limit_that_is_not_a_positive_number_is_refused_by_name(name, value):
    with pytest.raises(SettingsError, match=name):
        Settings.from_env({**DATABASE, name: value})


def test_heartbeats_are_sent_three_times_within_their_timeout():
    assert AnalysisPolicy(heartbeat_timeout_seconds=60).heartbeat_every_seconds == 20
