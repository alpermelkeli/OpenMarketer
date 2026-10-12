"""The activities of ``AnalyzeRepository`` that are the analyzer's own: names, inputs, failures.

The workflow calls activities by name and the activities register under the
same names, so the workflow module never imports the code that talks to the
database (the workflow sandbox could not load it). The steps of the graph and
the clone it asks for are the agent runtime's activities, named there
(``agent_runtime.vocabulary``); the two here are what the workflow's hooks
call. Everything here is stored in the workflow history: identifiers and short
messages only. (Temporal adds the stack trace of a failed activity on its own.)

A run is told why it failed in the analyzer's words. The runtime has sentences
of its own for what happens to an action and for an attempt that is still
running, and has no place to be given an agent's: ``in_the_analyzers_words``
turns those into the ones a run of an analysis has always been told.
"""

from __future__ import annotations

from dataclasses import dataclass, fields

from openmarketer_core.repository_analysis.workflow_contract import AnalyzeRepositoryInput
from openmarketer_worker.agent_runtime import vocabulary
from openmarketer_worker.agent_runtime.vocabulary import (
    ACTION_TEXTS,
    WORDED_BY_THE_RUNTIME,
    FailureTexts,
)

START_RUN = "start_run"
RECORD_FAILURE = "record_failure"

# Failure types of the analyzer's activities, of its clone and of a step of its graph. Those in
# NOT_RETRIED end the analysis at once. The ones every agent's activities share (the database,
# an attempt still running, an unexpected error) are the agent runtime's
# (``agent_runtime.vocabulary``).
ANALYSIS_FAILED = "AnalysisFailed"  # the repository or the model's work was refused
RUN_UNAVAILABLE = "RunUnavailable"  # the run does not exist or is already finished
RUN_NOT_FOUND_YET = "RunNotFoundYet"  # the run may not have been committed yet
MODEL_UNAVAILABLE = "ModelUnavailable"  # the provider failed or limited the rate
NOT_RETRIED = (ANALYSIS_FAILED, RUN_UNAVAILABLE)
# Failures of these types carry a message written for the run's reader: the analyzer's own, and
# the runtime's, which a step or an action of the graph can now end a run with.
WORDED_BY_THE_ACTIVITY = (
    ANALYSIS_FAILED,
    RUN_UNAVAILABLE,
    RUN_NOT_FOUND_YET,
    MODEL_UNAVAILABLE,
    *WORDED_BY_THE_RUNTIME,
)

# What a run is told when its failure carries no message that may be shown.
ANALYSIS_TEXTS = FailureTexts(
    stopped_unexpectedly="the analysis stopped unexpectedly",
    timed_out="the analysis did not finish in time",
    worker_stopped="the worker running the analysis stopped responding",
    cancelled="the analysis was cancelled",
    database_failed="the database could not be reached",
)
NOT_STORED = "the analysis finished, but its profile could not be stored"
EARLIER_ATTEMPT_RUNNING = "an earlier attempt at the analysis is still running"

# The runtime's sentences that can end a run of an analysis, each with the analyzer's for it:
# what the graph is told when its clone timed out, lost its worker or stopped on an error
# nobody worded (the analysis then ends with those words), and what a step says while an
# earlier attempt holds the run.
_THE_ANALYZERS_WORDS_FOR = {
    **{
        getattr(ACTION_TEXTS, text.name): getattr(ANALYSIS_TEXTS, text.name)
        for text in fields(FailureTexts)
    },
    vocabulary.EARLIER_ATTEMPT_RUNNING: EARLIER_ATTEMPT_RUNNING,
}


def in_the_analyzers_words(reason: str) -> str:
    """``reason`` as a run of an analysis is told it: a sentence of the runtime's in ours."""
    return _THE_ANALYZERS_WORDS_FOR.get(reason, reason)


@dataclass(frozen=True)
class RunFailure:
    """A run that ended without a profile, and the reason shown to whoever asked for it."""

    run: AnalyzeRepositoryInput
    error: str
