"""Port: starting the workflow that executes a stored analysis run.

This is the one thing the API asks of the workflow engine. The run itself, its
status and its result are in the database (``openmarketer_core.db.analysis_runs``),
so nothing here reads a workflow, waits for one or cancels one.
"""

from __future__ import annotations

from typing import Protocol

from openmarketer_core.analysis_workflow import AnalyzeRepositoryInput


class AnalysisWorkflows(Protocol):
    async def start(self, run: AnalyzeRepositoryInput) -> None:
        """Start the workflow of ``run``; a workflow that exists already counts as started.

        Raises ``WorkflowNotStarted`` when the engine cannot be reached or refuses.
        """
        ...
