"""The activities of ``CreativeStudio`` that are the studio's own: names, inputs and failures.

The workflow calls activities by name and the activities register under the same names, so
the workflow module never imports the code that talks to the database (the workflow sandbox
could not load it). The steps of the graph and the five actions it asks for are the agent
runtime's activities, named there (``agent_runtime.vocabulary``); the three here are what the
workflow's hooks call. Everything here is stored in the workflow history: identifiers, flags,
amounts and short messages only, never a prompt, a page, a program or an image.

A run is told why it failed in the studio's words: ``in_the_studios_words`` turns the
runtime's sentences for an action or an attempt still running into the studio's own.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, fields

from openmarketer_core.studio.workflow_contract import StudioRunInput
from openmarketer_worker.agent_runtime import vocabulary
from openmarketer_worker.agent_runtime.vocabulary import (
    ACTION_TEXTS,
    WORDED_BY_THE_RUNTIME,
    FailureTexts,
)

START_RUN = "start_run"
RECORD_FAILURE = "record_failure"
# Once a run is over, either way: its last event, for the chat that asked for it, if one did.
RECORD_END = "record_end"

# Failure types of the studio's activities, of its handlers and of a step of its graph. Those
# in NOT_RETRIED end the run (a step) or the action (a handler) at once; an action that failed
# is told to the graph, which may try another way. The ones every agent's activities share
# (the database, an attempt still running, an unexpected error) are the runtime's.
STUDIO_FAILED = "StudioFailed"  # the request, a model's answers or the configuration refused
STUDIO_UNAVAILABLE = "StudioUnavailable"  # object storage, the image provider or the sandbox
OPERATION_FAILED = "OperationFailed"  # an operation did not make its image; the graph is told
RUN_UNAVAILABLE = "RunUnavailable"  # the run does not exist or is already finished
RUN_NOT_FOUND_YET = "RunNotFoundYet"  # the run may not have been committed yet
MODEL_UNAVAILABLE = "ModelUnavailable"  # the provider failed or limited the rate
NOT_RETRIED = (STUDIO_FAILED, OPERATION_FAILED, RUN_UNAVAILABLE)
# Failures of these types carry a message written for the run's reader (and, for an action,
# for the studio's model): the studio's own, and the runtime's.
WORDED_BY_THE_ACTIVITY = (
    STUDIO_FAILED,
    STUDIO_UNAVAILABLE,
    OPERATION_FAILED,
    RUN_UNAVAILABLE,
    RUN_NOT_FOUND_YET,
    MODEL_UNAVAILABLE,
    *WORDED_BY_THE_RUNTIME,
)

# What a run is told when its failure carries no message that may be shown.
STUDIO_TEXTS = FailureTexts(
    stopped_unexpectedly="the studio stopped unexpectedly",
    timed_out="the studio did not finish in time",
    worker_stopped="the worker running the studio stopped responding",
    cancelled="the studio run was cancelled",
    database_failed="the database could not be reached",
)
NOT_STORED = "the studio finished, but its outcome could not be stored"
VERSION_NOT_STORED = "the image was made, but it could not be stored"
EARLIER_ATTEMPT_RUNNING = "an earlier attempt at the studio run is still running"

# The runtime's sentences that can end a studio run, each with the studio's for it.
_THE_STUDIOS_WORDS_FOR = {
    **{
        getattr(ACTION_TEXTS, text.name): getattr(STUDIO_TEXTS, text.name)
        for text in fields(FailureTexts)
    },
    vocabulary.EARLIER_ATTEMPT_RUNNING: EARLIER_ATTEMPT_RUNNING,
}


def in_the_studios_words(reason: str) -> str:
    """``reason`` as a studio run is told it: a sentence of the runtime's in the studio's."""
    return _THE_STUDIOS_WORDS_FOR.get(reason, reason)


@dataclass(frozen=True)
class RunFailure:
    """A run that ended without an asset, and the reason shown to whoever asked for it."""

    run: StudioRunInput
    error: str


@dataclass(frozen=True)
class RunEnd:
    """How a run ended, as the chat that asked for it is shown it.

    A run that ended with an asset names it and the version it ends with, and whether that
    version waits for a person because a limit was hit (``needs_review``); one that failed
    has the reason recorded on the run instead.
    """

    run: StudioRunInput
    asset_id: str | None = None
    version_id: str | None = None
    needs_review: bool | None = None
    reason: str | None = None


@dataclass(frozen=True)
class RunEndRecorded:
    """What ``record_end`` answers: the chat that asked for the run, or ``None`` if none did."""

    chat_id: uuid.UUID | None
