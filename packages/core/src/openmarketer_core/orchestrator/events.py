"""What a chat turn tells the person watching it while it runs: the port its events go through.

The chat agent records what it does as it goes (``tool_called``, ``tool_answered``,
``run_started``: ``openmarketer_core.run_events``) through ``TurnEvents``, built by the
turn's caller for one turn. The dashboard shows them live under the turn being answered.
They are for the person's screen only: the model never reads them, and a turn answers the
same whether or not anyone records them.

``NoTurnEvents`` records nothing, for a caller with no database (the CLI, a test that only
reads). The adapter that stores them is ``events_postgres.py``. This module imports no
database and no framework, so the chat agent may depend on it.
"""

from __future__ import annotations

from typing import Protocol

from openmarketer_core.run_events import RunEvent


class TurnEvents(Protocol):
    """Where the events of one chat turn go."""

    def record(self, event: RunEvent) -> None:
        """Record ``event`` of the turn, at once, for a screen that is watching.

        Recording is never a reason for the turn to fail: an adapter that cannot store an
        event says so in its log and returns.
        """
        ...


class NoTurnEvents:
    """``TurnEvents`` that records nothing: for a turn nobody watches."""

    def record(self, event: RunEvent) -> None:
        return None
