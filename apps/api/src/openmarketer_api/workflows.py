"""Port: starting the workflow that executes a stored run.

This is the one thing the API asks of the workflow engine, for every kind of
run: which workflow a run is (its name, queue, id and input) is the run's
contract in core, handed over as a ``WorkflowStart``. The run itself, its
status and its result are in the database, so nothing here reads a workflow,
waits for one or cancels one.
"""

from __future__ import annotations

from typing import Protocol

from openmarketer_core.workflow_start import WorkflowStart


class Workflows(Protocol):
    async def start(self, workflow: WorkflowStart) -> None:
        """Start ``workflow``; a workflow that exists already counts as started.

        Raises ``WorkflowNotStarted`` when the engine cannot be reached or refuses.
        """
        ...
