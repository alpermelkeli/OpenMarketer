"""What a studio run raises when it ends without an asset, and nothing else.

The worker words a failure for whoever asked for the run with the message of the error
itself, as for a research run: nothing here names a folder or a path on the worker's disk.
Everything a graph or a handler raises that is not in ``STUDIO_FAILURES`` is a defect.
"""

from __future__ import annotations

from openmarketer_core.llm import LLMError


class StudioError(Exception):
    """The studio could not make the asset; the message is worded for whoever asked."""


# What a run that produced no asset raises; anything else is a defect.
STUDIO_FAILURES = (StudioError, LLMError)
