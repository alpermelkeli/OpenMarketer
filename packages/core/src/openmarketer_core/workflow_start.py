"""Starting a durable workflow: what a starter is asked to start, and how a start fails.

Each kind of run has a contract with the worker that executes it (an analysis
run's is ``repository_analysis/workflow_contract.py``), and the contract says
which workflow a run is: its name, the task queue the worker serves, the id
derived from the run and the input. ``WorkflowStart`` carries exactly that to
whatever starts workflows (the API's Temporal adapter), so a starter knows no
workflow by name and serves every kind of run the same way.

It imports nothing of the engine and does not start anything; the starter is
handed to a use case as a function. Where the engine is, which every process
that talks to it needs, is ``workflow_server.py``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkflowStart:
    """One workflow to start, as a kind of run's contract describes it."""

    workflow: str  # the name the worker registers the workflow under
    workflow_id: str  # derived from the run, never stored: one run, one workflow
    task_queue: str  # the queue the worker of this kind of run serves
    # Identifiers only, as a frozen dataclass: the engine keeps the input in its history,
    # where it can be read, so no URL, content or credential belongs in it.
    input: object


class WorkflowNotStarted(Exception):
    """The workflow engine could not be reached, or refused to start the workflow.

    Raised by the function that starts workflows. A workflow that exists
    already is not this error: for the caller it has been started.
    """
