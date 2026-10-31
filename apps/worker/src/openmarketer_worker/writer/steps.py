"""The activities of ``WritePost`` that are the writer's own: names, inputs and failures.

The workflow calls activities by name and the activities register under the same names, so
the workflow module never imports the code that talks to the database (the workflow sandbox
could not load it). The steps of the graph and the two actions it asks for (``store_draft``,
``publish_post``) are the agent runtime's activities, named there (``agent_runtime.vocabulary``);
the three here are what the workflow's hooks call. Everything here is stored in the workflow
history: identifiers, a status, an amount and short messages only, never a brief, a profile
or a post.

A run is told why it failed in the writer's words: ``in_the_writers_words`` turns the
runtime's sentences for an action or an attempt still running into the writer's own.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, fields

from openmarketer_core.writing.workflow_contract import WritingRunInput
from openmarketer_worker.agent_runtime import vocabulary
from openmarketer_worker.agent_runtime.vocabulary import (
    ACTION_TEXTS,
    WORDED_BY_THE_RUNTIME,
    FailureTexts,
)

START_RUN = "start_run"
# Once the graph finished: how the post ended, settled on its content item, and the run's end.
RECORD_OUTCOME = "record_outcome"
RECORD_FAILURE = "record_failure"

# Failure types of the writer's activities, of its handlers and of a step of its graph. Those in
# NOT_RETRIED end the run (a step) or the action (a handler) at once. The ones every agent's
# activities share (the database, an attempt still running, an unexpected error) are the
# runtime's.
WRITING_FAILED = "WritingFailed"  # the brief, the profile, a model's answers or a draft refused
RUN_UNAVAILABLE = "RunUnavailable"  # the run or its post does not exist or is already settled
RUN_NOT_FOUND_YET = "RunNotFoundYet"  # the run may not have been committed yet
MODEL_UNAVAILABLE = "ModelUnavailable"  # the provider failed or limited the rate
NOT_RETRIED = (WRITING_FAILED, RUN_UNAVAILABLE)
# Failures of these types carry a message written for the run's reader (and, for an action,
# for the writer's graph): the writer's own, and the runtime's.
WORDED_BY_THE_ACTIVITY = (
    WRITING_FAILED,
    RUN_UNAVAILABLE,
    RUN_NOT_FOUND_YET,
    MODEL_UNAVAILABLE,
    *WORDED_BY_THE_RUNTIME,
)

# What a run is told when its failure carries no message that may be shown.
WRITING_TEXTS = FailureTexts(
    stopped_unexpectedly="the writer stopped unexpectedly",
    timed_out="the writer did not finish in time",
    worker_stopped="the worker running the writer stopped responding",
    cancelled="the writing run was cancelled",
    database_failed="the database could not be reached",
)
DRAFT_NOT_STORED = "the draft could not be stored"
POST_NOT_RECORDED = "the approved post could not be recorded"
EARLIER_ATTEMPT_RUNNING = "an earlier attempt at the writing run is still running"
# What a run whose result cannot be read ends with: a defect of the graph's ``record``.
RESULT_UNREADABLE = "the writer finished without a post it could record"

# The runtime's sentences that can end a writing run, each with the writer's for it.
_THE_WRITERS_WORDS_FOR = {
    **{
        getattr(ACTION_TEXTS, text.name): getattr(WRITING_TEXTS, text.name)
        for text in fields(FailureTexts)
    },
    vocabulary.EARLIER_ATTEMPT_RUNNING: EARLIER_ATTEMPT_RUNNING,
}


def in_the_writers_words(reason: str) -> str:
    """``reason`` as a writing run is told it: a sentence of the runtime's in the writer's."""
    return _THE_WRITERS_WORDS_FOR.get(reason, reason)


@dataclass(frozen=True)
class RunOutcome:
    """How the post of a run whose graph finished ended, as ``record_outcome`` settles it.

    ``status`` is the value of core's ``PostStatus``: ``dry_run``, ``rejected`` or ``expired``
    (a post that ``failed`` ends the run through ``failed`` instead). ``cost_usd`` is what the
    run's model calls cost, as text, exact as the graph counted it.
    """

    run: WritingRunInput
    content_item_id: uuid.UUID
    status: str
    cost_usd: str


@dataclass(frozen=True)
class RunFailure:
    """A run that ended without a settled post, and the reason shown to whoever asked for it."""

    run: WritingRunInput
    error: str
