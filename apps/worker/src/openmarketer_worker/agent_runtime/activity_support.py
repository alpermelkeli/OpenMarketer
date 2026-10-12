"""What an activity of any agent owes the engine: worded failures, heartbeats and checkpoints.

- ``worded_failures`` lets only failures leave an activity whose type and message were
  chosen. Temporal stores whatever an activity raises, with every cause and every error it
  was raised while handling; an error nobody worded is logged and replaced, and one that was
  worded leaves as a copy that holds nothing but what was chosen, so the history never holds a
  message written for a log (a path, a host's reply, a URL with a key in it). The stack trace
  Temporal stores of a failure names source files and lines only.
- ``run_with_heartbeats`` runs blocking work in a thread while the activity sends heartbeats,
  and ``run_in_thread`` does so at the rate the workflow's heartbeat timeout asks for.
- ``stored_size`` says how much of the workflow history a value takes, and refuses what the
  history cannot hold.
- ``CheckpointAccess`` and ``checkpoints_in`` reach the checkpoints of a run by its thread, and
  ``forget_quietly`` forgets them without ever failing the activity that does it.

Every function that logs takes a ``subject`` (``"campaign run <id>"``) to name the work. The
helpers that take ``FailureTexts`` hold no words of their own: an agent gives its own. None
reads the environment. This module is for activities: the workflow sandbox does not load it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from openmarketer_core.db.session import DatabaseError
from openmarketer_core.graph_checkpoints import RunCheckpoints
from openmarketer_core.graph_checkpoints.postgres import (
    driver_connection_string,
    forget_checkpoints,
    run_checkpoints,
)
from openmarketer_worker.agent_runtime.vocabulary import (
    DATABASE_UNAVAILABLE,
    UNEXPECTED,
    FailureTexts,
)

logger = logging.getLogger(__name__)


@contextmanager
def worded_failures(texts: FailureTexts, *, subject: str) -> Iterator[None]:
    """Let only an ``ApplicationError`` leave the block; anything else is logged and replaced.

    A failure of the database becomes ``DATABASE_UNAVAILABLE`` and any other
    error ``UNEXPECTED``, both worth another attempt and told in the words of
    ``texts``. An ``ApplicationError`` is the code's own, with a type and a
    message somebody chose, and leaves as a copy of itself: the original may have been
    raised while its author handled another error (a library's, with the URL it
    called), and Temporal follows that link and stores the text of what it finds.
    """
    try:
        yield
    except ApplicationError as worded:
        raise _without_context(worded) from None
    except DatabaseError:
        logger.exception("%s: the database could not be used", subject)
        raise ApplicationError(texts.database_failed, type=DATABASE_UNAVAILABLE) from None
    except Exception:  # noqa: BLE001  (activity boundary, see the docstring)
        logger.exception("%s stopped on an unexpected error", subject)
        raise ApplicationError(texts.stopped_unexpectedly, type=UNEXPECTED) from None


def _without_context(error: ApplicationError) -> ApplicationError:
    """``error`` as it was worded, without the errors it was raised from or while handling."""
    return ApplicationError(
        error.message,
        *error.details,
        type=error.type,
        non_retryable=error.non_retryable,
        next_retry_delay=error.next_retry_delay,
        category=error.category,
    ).with_traceback(error.__traceback__)


async def run_with_heartbeats[T](work: Callable[[], T], *, every_seconds: float, subject: str) -> T:
    """Run the blocking ``work`` in a thread, sending heartbeats, and return what it returns.

    A heartbeat says the worker process is alive, not that the work is making
    progress; the activity's timeout bounds that. What the work raises is
    raised here.

    When the activity is cancelled the thread cannot be stopped, and whatever it
    still does is settled by the rules of what it writes (a run that is already
    over takes no result). The activity ends at once, so a retry may start
    while the thread works: whatever the thread holds that makes the retry
    wait for it (a run's checkpoints) is what keeps the two apart.
    """
    running = asyncio.create_task(asyncio.to_thread(work))
    # When this coroutine is cancelled nobody else collects the thread's outcome.
    running.add_done_callback(lambda finished: finished.cancelled() or finished.exception())
    try:
        while not running.done():
            activity.heartbeat()
            await asyncio.wait({running}, timeout=every_seconds)
    except asyncio.CancelledError:
        logger.warning("%s was cancelled while its work was running", subject)
        raise
    return running.result()


async def run_in_thread[T](work: Callable[[], T], *, subject: str) -> T:
    """Run the blocking ``work`` in a thread, heartbeating as often as the workflow asked for.

    The workflow sets the activity's heartbeat timeout when it schedules it, and this reads it
    from there: three heartbeats in it, so the one place a limit is set is the workflow's. An
    activity scheduled without a heartbeat timeout sends none, and so is never told that it was
    cancelled (cancellation reaches an activity in the answer to a heartbeat).
    """
    timeout = activity.info().heartbeat_timeout
    if timeout is None:
        return await asyncio.to_thread(work)
    return await run_with_heartbeats(
        work, every_seconds=timeout.total_seconds() / 3, subject=subject
    )


class NotStorable(Exception):
    """A value cannot be written to the workflow history as JSON."""


def stored_size(value: Any) -> int:
    """How many bytes the history keeps of ``value``, a dataclass of plain data.

    It is the size of its payload as the activity's data converter writes it, which is what the
    history stores. Raises ``NotStorable`` for what the history cannot hold: what JSON cannot
    write (NaN and infinity, which the converter would write as text that is not JSON, and
    sets, which it would turn into lists), and nesting so deep that encoding it fails, which
    the converter does only when it returns the activity's result, after the work was done.
    """
    try:
        json.dumps(dataclasses.asdict(value), allow_nan=False)
        return len(activity.payload_converter().to_payload(value).data)
    except (TypeError, ValueError, RecursionError):
        raise NotStorable from None


@dataclass(frozen=True)
class CheckpointAccess:
    """How the activities reach the checkpoints of a run, by its thread id.

    Built from the database URL when the worker starts (``checkpoints_in``),
    from core's ``run_checkpoints`` and ``forget_checkpoints``.
    """

    # The checkpoints for one attempt; refuses with ``AttemptInProgress`` while another has them.
    of_attempt: Callable[[str], AbstractContextManager[RunCheckpoints]]
    forget: Callable[[str], None]


def checkpoints_in(database_url: str) -> CheckpointAccess:
    """The checkpoints of runs, kept in the database the runs themselves are in.

    Raises ``DatabaseError`` when the URL is not one the checkpoint store can
    use, without repeating it: it holds the password.
    """
    # Asked for here, so a URL the checkpoint store cannot use stops the worker and not every run.
    driver_connection_string(database_url)

    # Functions, not ``functools.partial``: the text of a partial shows its arguments,
    # and the URL holds the password.
    def of_attempt(thread_id: str) -> AbstractContextManager[RunCheckpoints]:
        return run_checkpoints(database_url, thread_id)

    def forget(thread_id: str) -> None:
        forget_checkpoints(database_url, thread_id)

    return CheckpointAccess(of_attempt=of_attempt, forget=forget)


def forget_quietly(checkpoints: CheckpointAccess, thread_id: str, *, subject: str) -> None:
    """Forget the checkpoints of a run that is over; failing to is logged, not raised.

    The outcome of the run is recorded by then, and a failed activity would be
    retried or reported in its place. The rows are left to the worker's cleanup.
    """
    try:
        checkpoints.forget(thread_id)
    except DatabaseError as e:
        logger.warning(
            "checkpoints of %s were not forgotten, the cleanup removes them: %s", subject, e
        )
