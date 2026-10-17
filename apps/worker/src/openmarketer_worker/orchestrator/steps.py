"""The activity of ``ChatFollowUp``: its name, its failure types and the words of its failures.

The workflow calls the activity by name and the activity registers under the same name, so
the workflow module never imports the code that talks to the database or to a model (the
workflow sandbox could not load it). Everything here is stored in the workflow history:
identifiers and short sentences only.

A follow-up records no failure on a run: the turn it takes records its own (core marks the
turn failed and records ``turn_failed`` in the chat). What a failure here says is for the
worker's log and the workflow history, so it is one of the sentences below or the worded
message of a workspace's credential, never what a model provider wrote.
"""

from __future__ import annotations

from openmarketer_worker.agent_runtime.vocabulary import (
    DATABASE_UNAVAILABLE,
    UNEXPECTED,
    FailureTexts,
)

FOLLOW_UP_ON_RESEARCH = "follow_up_on_research"
RECORD_FOLLOW_UP_NOT_TAKEN = "record_follow_up_not_taken"

# Failure types of the activity. A busy chat and a model provider that may answer later are
# tried again; the database too (the runtime's ``DATABASE_UNAVAILABLE``). The rest end the
# follow-up at once: there is nothing to follow up on, or another attempt would fail the same
# way. ``UNEXPECTED`` is the runtime's type for an error nobody worded, which is not retried
# here: a turn that failed that way is recorded failed in the chat already.
CHAT_BUSY = "ChatBusy"  # another turn of the chat is running
MODEL_UNAVAILABLE = "ModelUnavailable"  # the provider failed or limited the rate
# no such project, chat, or run of the chat, or a run that has not ended
NOTHING_TO_FOLLOW_UP = "NothingToFollowUp"
# no answer within the limits, no usable model, or as many turns tried as a chat may
FOLLOW_UP_FAILED = "FollowUpFailed"
NOT_RETRIED = (NOTHING_TO_FOLLOW_UP, FOLLOW_UP_FAILED, UNEXPECTED)
WORDED_BY_THE_ACTIVITY = (
    CHAT_BUSY,
    MODEL_UNAVAILABLE,
    NOTHING_TO_FOLLOW_UP,
    FOLLOW_UP_FAILED,
    DATABASE_UNAVAILABLE,
    UNEXPECTED,
)

CHAT_IS_BUSY = "the chat is answering another turn"
NOTHING_TO_FOLLOW_UP_ON = "the chat or the research run is not there to follow up on"
FOLLOW_UP_GIVEN_UP = "the chat tried to follow up as often as it may"

# What the chat is told, after ``follow_up.NOT_TAKEN``'s start, when the follow-up ended
# before it got to a turn, by the type of its failure. A failure that came after a turn shows
# in that turn, so telling it again records nothing (``record_unstarted_follow_up``).
NOT_TAKEN_BECAUSE = {
    CHAT_BUSY: "the chat stayed busy with other turns",
    FOLLOW_UP_FAILED: "the workspace's model could not be set up",
    MODEL_UNAVAILABLE: "the model could not answer",
    DATABASE_UNAVAILABLE: "the database could not be reached",
}
NOT_TAKEN_IN_TIME = "no worker took it up in time"
NOT_TAKEN_UNEXPECTEDLY = "it stopped unexpectedly"

# What the follow-up is told when its failure carries no message that may be shown.
FOLLOW_UP_TEXTS = FailureTexts(
    stopped_unexpectedly="the follow-up stopped unexpectedly",
    timed_out="the follow-up did not finish in time",
    worker_stopped="the worker running the follow-up stopped responding",
    cancelled="the follow-up was cancelled",
    database_failed="the database could not be reached",
)
