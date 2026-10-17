"""How long a follow-up turn may take and how long it is tried, so the workflow holds no number.

The follow-up is one activity: one turn of the chat agent, which reads the project and
answers within its own limits (``ChatLimits``: a few model calls). Why these numbers:

- An attempt may take ten minutes: well above a turn of a few model calls, and below the
  quarter of an hour after which a turn still running is taken for abandoned
  (``db.chat.TURN_ABANDONED_AFTER``), so a turn this worker is still answering is never
  given up by the next one. A test holds the two together.
- The attempt sends heartbeats, so a worker that died is noticed within a minute and the
  turn is taken again by another one.
- A chat that is answering the operator is busy for as long as that turn lasts, a minute or
  two: the follow-up waits two seconds, then twice as long each time up to two minutes, and
  gives up after half an hour, by when the operator has been answered many times over.
- Whatever is tried again (a busy chat, a database that could not be reached, a model
  provider that may answer later), an attempt that gets as far as a turn may cost a model's
  turn. Core counts the turns a chat started about a run and gives the follow-up up after
  ``MAX_FOLLOW_UP_TURNS`` of them (``db.chat_turns``), so the half hour of attempts spends
  at most that many model turns, however the attempts failed.
- What the chat is told when the follow-up never got to a turn is stored by one short
  activity, tried a few times within a minute: telling it is worth little after that.

This module imports nothing with a side effect: the workflow sandbox loads it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FollowUpPolicy:
    attempt_timeout_seconds: float = 10 * 60
    heartbeat_timeout_seconds: float = 60
    retry_after_seconds: float = 2
    max_retry_wait_seconds: float = 2 * 60
    give_up_after_seconds: float = 30 * 60


FOLLOW_UP = FollowUpPolicy()


@dataclass(frozen=True)
class NotTakenPolicy:
    attempt_timeout_seconds: float = 20
    retry_after_seconds: float = 2
    attempts: int = 3


NOT_TAKEN = NotTakenPolicy()
