"""Port: telling a run's workflow that a person decided on one of its proposals.

The decision is in the database before this is asked (``openmarketer_core.approvals``); the
signal only wakes the run, which reads it. Nothing here carries the decision itself.
"""

from __future__ import annotations

import uuid
from typing import Protocol


class RunSignals(Protocol):
    async def tell_decided(self, workflow_id: str, proposal_id: uuid.UUID) -> None:
        """Tell the workflow ``workflow_id`` that the proposal ``proposal_id`` was decided.

        Raises ``RunNotTold`` when the engine cannot be reached or refuses (the workflow
        ended, say).
        """
        ...
