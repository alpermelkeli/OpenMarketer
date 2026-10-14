"""Port: starting the workflow that executes a stored run, and telling a run of a decision.

These are the two things the API asks of the workflow engine. To start one, for every kind of
run: which workflow a run is (its name, queue, id and input) is the run's contract in core,
handed over as a ``WorkflowStart``. To tell a run that waits for a person that one of its
proposals was decided: the decision is in the database before it is told
(``openmarketer_core.approvals``), and the signal only wakes the run, which reads it. The run
itself, its status and its result are in the database, so nothing here reads a workflow, waits
for one or cancels one.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from openmarketer_core.workflow_start import WorkflowStart


class Workflows(Protocol):
    async def start(self, workflow: WorkflowStart) -> None:
        """Start ``workflow``; a workflow that exists already counts as started.

        Raises ``WorkflowNotStarted`` when the engine cannot be reached or refuses.
        """
        ...

    async def tell_decided(self, workflow_id: str, proposal_id: uuid.UUID) -> None:
        """Tell the workflow ``workflow_id`` that the proposal ``proposal_id`` was decided.

        Raises ``RunNotTold`` when the engine cannot be reached or refuses (the workflow
        ended, say).
        """
        ...
