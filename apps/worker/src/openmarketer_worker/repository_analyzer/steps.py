"""The activities of ``AnalyzeRepository``: their names, inputs and failure types.

The workflow calls activities by name and the activities register under the
same names, so the workflow module never imports the code that clones
repositories and talks to the database (the workflow sandbox could not load
it). Everything here is stored in the workflow history: identifiers and short
messages only. (Temporal adds the stack trace of a failed activity on its own.)
"""

from __future__ import annotations

from dataclasses import dataclass

from openmarketer_core.repository_analysis.workflow_contract import AnalyzeRepositoryInput

START_RUN = "start_run"
ANALYSE_AND_STORE = "analyse_and_store"
RECORD_FAILURE = "record_failure"

# Failure types of activities. Those in NOT_RETRIED end the analysis at once.
ANALYSIS_FAILED = "AnalysisFailed"  # the repository or the model's work was refused
RUN_UNAVAILABLE = "RunUnavailable"  # the run does not exist or is already finished
RUN_NOT_FOUND_YET = "RunNotFoundYet"  # the run may not have been committed yet
MODEL_UNAVAILABLE = "ModelUnavailable"  # the provider failed or limited the rate
DATABASE_UNAVAILABLE = "DatabaseUnavailable"  # the database refused or was unreachable
ATTEMPT_STILL_RUNNING = "AttemptStillRunning"  # an attempt given up on has not returned yet
UNEXPECTED = "Unexpected"
NOT_RETRIED = (ANALYSIS_FAILED, RUN_UNAVAILABLE)
# Failures raised by the activities below carry a message written for the run's reader.
WORDED_BY_THE_ACTIVITY = (
    ANALYSIS_FAILED,
    RUN_UNAVAILABLE,
    RUN_NOT_FOUND_YET,
    MODEL_UNAVAILABLE,
    DATABASE_UNAVAILABLE,
    ATTEMPT_STILL_RUNNING,
    UNEXPECTED,
)

# What a run is told when its failure carries no message that may be shown.
STOPPED_UNEXPECTEDLY = "the analysis stopped unexpectedly"
TIMED_OUT = "the analysis did not finish in time"
WORKER_STOPPED = "the worker running the analysis stopped responding"
CANCELLED = "the analysis was cancelled"
NOT_STORED = "the analysis finished, but its profile could not be stored"
DATABASE_FAILED = "the database could not be reached"
EARLIER_ATTEMPT_RUNNING = "an earlier attempt at the analysis is still running"


@dataclass(frozen=True)
class RunFailure:
    """A run that ended without a profile, and the reason shown to whoever asked for it."""

    run: AnalyzeRepositoryInput
    error: str
