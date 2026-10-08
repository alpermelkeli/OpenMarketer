"""Tests for the settings the API process reads at start-up."""

import pytest

from openmarketer_api.settings import Settings, SettingsError


def test_database_url_is_read_from_the_environment():
    settings = Settings.from_env({"DATABASE_URL": "postgresql+psycopg://u:p@localhost/db"})
    assert settings.database_url == "postgresql+psycopg://u:p@localhost/db"


@pytest.mark.parametrize("environ", [{}, {"DATABASE_URL": ""}])
def test_start_without_a_database_url_is_refused(environ):
    with pytest.raises(SettingsError, match="DATABASE_URL"):
        Settings.from_env(environ)
