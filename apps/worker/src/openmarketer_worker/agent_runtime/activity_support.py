"""What an activity of any agent owes the engine: worded failures, heartbeats and checkpoints.

- ``worded_failures`` lets only failures leave an activity whose type and
  message were chosen. Temporal stores whatever an activity raises, with every
  cause; an error nobody worded is logged and replaced, so the history never
  holds a message written for a log (a path, a host's reply, a secret).
  ``failure`` builds the one that is raised: the original error is never
  attached (``from None`` at the call), because Temporal stores a failure with
  its causes, and those can name local paths. It also stores the stack trace,
  which names source files and lines only.
- ``run_with_heartbeats`` runs blocking work in a thread while the activity
  sends heartbeats.
- ``CheckpointAccess`` and ``checkpoints_in`` reach the checkpoints of a run by
  its thread, and ``forget_quietly`` forgets them without ever failing the
  activity that does it.

Every function takes a ``subject`` (``"analysis run <id>"``) to name the work in
the log; none holds an agent's wording (``FailureTexts``) or failure types, and
none reads the environment. This module is for activities: the workflow
sandbox does not load it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass

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


def failure(kind: str, message: str, *, non_retryable: bool = False) -> ApplicationError:
    """The error to raise, as ``raise failure(...) from None``: a type and a message to show."""
    return ApplicationError(message, type=kind, non_retryable=non_retryable)


@contextmanager
def worded_failures(texts: FailureTexts, *, subject: str) -> Iterator[None]:
    """Let only an ``ApplicationError`` leave the block; anything else is logged and replaced.

    A failure of the database becomes ``DATABASE_UNAVAILABLE`` and any other
    error ``UNEXPECTED``, both worth another attempt and told in the words of
    ``texts``. An ``ApplicationError`` is the code's own, with a type and a
    message somebody chose, and passes as it is.
    """
    try:
        yield
    except ApplicationError:
        raise
    except DatabaseError:
        logger.exception("%s: the database could not be used", subject)
        raise failure(DATABASE_UNAVAILABLE, texts.database_failed) from None
    except Exception:  # noqa: BLE001  (activity boundary, see the docstring)
        logger.exception("%s stopped on an unexpected error", subject)
        raise failure(UNEXPECTED, texts.stopped_unexpectedly) from None


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
