"""Tests for what the policy of the ``AnalyzeRepository`` workflow computes.

``longest_analysis_seconds`` has to bound everything the workflow can spend, because the
checkpoints and the clone of a run older than it are removed. The parts are written out here
as numbers, so that a part the workflow gains and the function forgets fails a test.
"""

from openmarketer_core.repository_analysis.analyzer_agent.definitions import (
    CLONE_REPOSITORY,
    agent,
)
from openmarketer_worker.agent_runtime.vocabulary import FORGET_RUN_LIMITS
from openmarketer_worker.repository_analyzer import policy
from openmarketer_worker.repository_analyzer.policy import AnalysisPolicy, longest_analysis_seconds

# Five attempts of 30 seconds, and the waits of half a second, doubling, between them.
STARTING_THE_RUN = 5 * 30 + (0.5 + 1 + 2 + 4)
# Five attempts of a quarter of an hour, and the waits of 1, 2, 4 and 8 minutes between them.
THE_CLONE = 5 * 15 * 60 + (1 + 2 + 4 + 8) * 60
RECORDING_THE_FAILURE = 60 * 60
FORGETTING_THE_RUN = 60 * 60
WAITING_FOR_A_WORKER = 24 * 60 * 60
AROUND_THE_STEPS = (
    STARTING_THE_RUN + THE_CLONE + RECORDING_THE_FAILURE + FORGETTING_THE_RUN + WAITING_FOR_A_WORKER
)


def test_longest_analysis_is_every_part_of_the_workflow_at_its_limits():
    limits = AnalysisPolicy(attempt_timeout_seconds=600, max_attempts=3, retry_after_seconds=60)
    one_step = 3 * 600 + (60 + 120)
    assert longest_analysis_seconds(limits) == 2 * one_step + AROUND_THE_STEPS


def test_longest_analysis_counts_both_steps_of_the_graph():
    # One step ends by asking for the clone, the other does the model's work after it.
    limits = AnalysisPolicy(attempt_timeout_seconds=600, max_attempts=1, retry_after_seconds=60)
    longer = AnalysisPolicy(attempt_timeout_seconds=700, max_attempts=1, retry_after_seconds=60)
    assert longest_analysis_seconds(longer) - longest_analysis_seconds(limits) == 2 * 100


def test_longest_analysis_of_single_attempts_has_no_waits_between_them():
    limits = AnalysisPolicy(attempt_timeout_seconds=600, max_attempts=1, retry_after_seconds=60)
    assert longest_analysis_seconds(limits) == 2 * 600 + AROUND_THE_STEPS


def test_longest_analysis_counts_no_wait_longer_than_temporal_makes_it():
    few = AnalysisPolicy(attempt_timeout_seconds=600, max_attempts=9, retry_after_seconds=1)
    one_more = AnalysisPolicy(attempt_timeout_seconds=600, max_attempts=10, retry_after_seconds=1)
    # The waits double from 1 second to 128; the ninth would be 256 and is 100. In both steps.
    assert longest_analysis_seconds(one_more) - longest_analysis_seconds(few) == 2 * (600 + 100)


def test_longest_analysis_with_the_default_limits():
    one_step = 5 * 30 * 60 + (1 + 2 + 4 + 8) * 60
    assert longest_analysis_seconds(AnalysisPolicy()) == 2 * one_step + AROUND_THE_STEPS


def test_clone_is_counted_with_the_limits_declared_on_its_action():
    clone = agent.policies[CLONE_REPOSITORY]
    assert (
        clone.attempt_timeout_seconds,
        clone.max_attempts,
        clone.retry_after_seconds,
    ) == (15 * 60, 5, 60)


def test_writes_that_end_a_run_are_counted_for_as_long_as_they_keep_trying():
    assert policy.RECORD_FAILURE.give_up_after_seconds == RECORDING_THE_FAILURE
    assert FORGET_RUN_LIMITS.give_up_after_seconds == FORGETTING_THE_RUN


def test_start_of_a_run_is_counted_with_the_limits_it_is_written_under():
    start = policy.START_RUN
    assert (
        start.attempt_timeout_seconds,
        start.max_attempts,
        start.retry_after_seconds,
        start.max_retry_wait_seconds,
    ) == (30, 5, 0.5, 10)
