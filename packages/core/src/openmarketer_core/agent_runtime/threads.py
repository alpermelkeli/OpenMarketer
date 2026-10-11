"""The name of the thread a run of an agent graph keeps its checkpoints under.

The checkpoint tables are shared by every graph and know a run by its thread's
name alone, so the name says whose it is: the kind of run, a colon, the run's
id. The cleanup of left-over checkpoints finds the threads of one kind by that
prefix (see ``db/checkpoint_cleanup.py``), which is why a kind is limited to
characters a pattern match cannot read as anything else.

This module only names threads. It opens no store and decides nothing about
when checkpoints are kept or removed.
"""

from __future__ import annotations

import re
import uuid

# A colon ends the kind, and ``_`` and ``%`` are wildcards to the pattern match of the cleanup.
_KIND = re.compile(r"[a-z0-9-]+")


def thread_id(kind: str, run_id: uuid.UUID) -> str:
    """The thread of run ``run_id`` of ``kind``, such as ``analysis-run:<run id>``.

    Raises ``ValueError`` when ``kind`` is empty or has anything but lower-case
    letters, digits and hyphens.
    """
    if not _KIND.fullmatch(kind):
        raise ValueError(
            f"kind of run {kind!r} must be one or more lower-case letters, digits or hyphens"
        )
    return f"{kind}:{run_id}"
