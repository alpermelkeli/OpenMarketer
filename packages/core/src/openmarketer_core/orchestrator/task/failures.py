"""What an orchestrator task raises when it ends without a report, and nothing else.

The worker words a failure for whoever asked for the task with the message of the error
itself, as for a research, a studio or a writing run: nothing here names a folder or a path on
the worker's disk. Everything a graph or a handler raises that is not in ``TASK_FAILURES`` is a
defect.
"""

from __future__ import annotations

from openmarketer_core.llm import LLMError


class TaskError(Exception):
    """The task could not be carried to its report; the message is worded for whoever asked."""


# What a task that stored no report raises; anything else is a defect.
TASK_FAILURES = (TaskError, LLMError)
