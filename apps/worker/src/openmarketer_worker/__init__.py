"""OpenMarketer Temporal worker: runs the long work of each agent as durable workflows.

One folder per agent holds its workflow, activities, settings and wiring;
``main.py`` holds the process. ``AgentRegistration`` is what an agent's wiring
hands to the process.

The workflow sandbox loads this module with every workflow, so at run time it
imports nothing but the standard library.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # For the type checker only: the module imports the database code.
    from openmarketer_worker.checkpoint_cleanup import CheckpointCleanup


@dataclass(frozen=True)
class AgentRegistration:
    """What one agent contributes to the worker, ready to be registered and served."""

    task_queue: str  # where its workflows are started
    workflows: Sequence[type]
    activities: Sequence[Callable[..., Any]]
    # One for each kind of run of the agent that keeps graph checkpoints; none if none does.
    checkpoint_cleanups: Sequence[CheckpointCleanup]
