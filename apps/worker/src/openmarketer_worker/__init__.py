"""OpenMarketer Temporal worker: runs the long work of each agent as durable workflows.

One folder per agent holds its workflow, activities, settings and wiring;
``main.py`` holds the process, and beside it is what every agent shares: the
agent runtime, the schedule of checkpoint cleanups, and the installation's
credentials resolved for a run's workspace (``credentials.py``).
``AgentRegistration`` is what an agent's wiring
hands to the process.

The workflow sandbox loads this module with every workflow, so at run time it
imports nothing but the standard library.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # For the type checker only: the module imports the database code.
    from openmarketer_worker.checkpoint_cleanup import CheckpointCleanup

# A task an agent brings that the process runs while it serves: a coroutine that
# repeats until it is cancelled (the researcher's page retention).
PeriodicTask = Callable[[], Coroutine[Any, Any, None]]


@dataclass(frozen=True)
class AgentRegistration:
    """What one agent contributes to the worker, ready to be registered and served."""

    task_queue: str  # where its workflows are started
    workflows: Sequence[type]
    activities: Sequence[Callable[..., Any]]
    # One for each kind of run of the agent that keeps graph checkpoints; none if none does.
    checkpoint_cleanups: Sequence[CheckpointCleanup]
    # The agent's own schedules, which ``main.py`` starts beside the checkpoint cleanups.
    periodic_tasks: Sequence[PeriodicTask] = ()
