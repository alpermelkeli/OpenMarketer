"""The words and names the analyzer has stored on runs and in workflow histories.

They are compared here with literals and not with the constants that hold them: a run that was
recorded failed keeps its reason for good, and a history of a run in flight names its failures
by type, so a change of a word or a spelling has to fail a test and not pass by being consistent.
"""

from openmarketer_worker.agent_runtime.vocabulary import (
    ATTEMPT_STILL_RUNNING,
    DATABASE_UNAVAILABLE,
    UNEXPECTED,
)
from openmarketer_worker.repository_analyzer import steps


def test_words_and_failure_types_stored_on_runs_and_in_histories_keep_their_exact_spelling():
    texts = steps.ANALYSIS_TEXTS
    assert (
        texts.stopped_unexpectedly,
        texts.timed_out,
        texts.worker_stopped,
        texts.cancelled,
        texts.database_failed,
        steps.NOT_STORED,
        steps.EARLIER_ATTEMPT_RUNNING,
    ) == (
        "the analysis stopped unexpectedly",
        "the analysis did not finish in time",
        "the worker running the analysis stopped responding",
        "the analysis was cancelled",
        "the database could not be reached",
        "the analysis finished, but its profile could not be stored",
        "an earlier attempt at the analysis is still running",
    )
    assert (
        steps.ANALYSIS_FAILED,
        steps.RUN_UNAVAILABLE,
        steps.RUN_NOT_FOUND_YET,
        steps.MODEL_UNAVAILABLE,
        DATABASE_UNAVAILABLE,
        ATTEMPT_STILL_RUNNING,
        UNEXPECTED,
    ) == (
        "AnalysisFailed",
        "RunUnavailable",
        "RunNotFoundYet",
        "ModelUnavailable",
        "DatabaseUnavailable",
        "AttemptStillRunning",
        "Unexpected",
    )
    assert steps.NOT_RETRIED == ("AnalysisFailed", "RunUnavailable")
