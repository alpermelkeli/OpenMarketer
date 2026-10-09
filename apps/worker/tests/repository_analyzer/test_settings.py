"""Tests for the settings of the repository analyzer's workflow, read at start-up."""

import pytest

from openmarketer_worker.repository_analyzer.policy import AnalysisPolicy
from openmarketer_worker.repository_analyzer.settings import RepositoryAnalyzerSettings
from openmarketer_worker.settings import SettingsError


def test_analysis_limits_have_defaults():
    assert RepositoryAnalyzerSettings.from_env({}).policy == AnalysisPolicy()


def test_analysis_limits_are_read_from_the_environment():
    settings = RepositoryAnalyzerSettings.from_env(
        {"ANALYSIS_TIMEOUT_MINUTES": "45", "ANALYSIS_MAX_ATTEMPTS": "1"}
    )
    assert settings.policy.attempt_timeout_seconds == 45 * 60
    assert settings.policy.max_attempts == 1


@pytest.mark.parametrize("name", ["ANALYSIS_TIMEOUT_MINUTES", "ANALYSIS_MAX_ATTEMPTS"])
@pytest.mark.parametrize("value", ["0", "-1", "soon"])
def test_limit_that_is_not_a_positive_number_is_refused_by_name(name, value):
    with pytest.raises(SettingsError, match=name):
        RepositoryAnalyzerSettings.from_env({name: value})
