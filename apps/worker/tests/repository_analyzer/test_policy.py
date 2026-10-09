"""Tests for what the policy of the ``AnalyzeRepository`` workflow computes."""

from openmarketer_worker.repository_analyzer.policy import AnalysisPolicy, longest_analysis_seconds


def test_heartbeats_are_sent_three_times_within_their_timeout():
    assert AnalysisPolicy(heartbeat_timeout_seconds=60).heartbeat_every_seconds == 20


def test_longest_analysis_is_every_attempt_the_waits_between_them_and_the_fixed_allowances():
    limits = AnalysisPolicy(attempt_timeout_seconds=600, max_attempts=3, retry_after_seconds=60)
    recording_the_failure = 60 * 60
    waiting_for_a_worker = 24 * 60 * 60
    assert longest_analysis_seconds(limits) == (
        3 * 600 + (60 + 120) + recording_the_failure + waiting_for_a_worker
    )


def test_longest_analysis_of_a_single_attempt_has_no_waits():
    limits = AnalysisPolicy(attempt_timeout_seconds=600, max_attempts=1, retry_after_seconds=60)
    assert longest_analysis_seconds(limits) == 600 + 60 * 60 + 24 * 60 * 60


def test_longest_analysis_counts_no_wait_longer_than_temporal_makes_it():
    few = AnalysisPolicy(attempt_timeout_seconds=600, max_attempts=9, retry_after_seconds=1)
    one_more = AnalysisPolicy(attempt_timeout_seconds=600, max_attempts=10, retry_after_seconds=1)
    # The waits double from 1 second to 128; the ninth would be 256 and is 100.
    assert longest_analysis_seconds(one_more) - longest_analysis_seconds(few) == 600 + 100


def test_longest_analysis_with_the_default_limits():
    assert longest_analysis_seconds(AnalysisPolicy()) == (
        5 * 30 * 60 + (1 + 2 + 4 + 8) * 60 + 60 * 60 + 24 * 60 * 60
    )
