"""Tests for the settings the API process reads at start-up."""

import pytest

from openmarketer_api.settings import Settings, SettingsError
from openmarketer_core.analysis_workflow import WorkflowServer

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
