"""What a writing run raises when it ends without a post, and nothing else.

The worker words a failure for whoever asked for the run with the message of the error
itself, as for a research or a studio run: nothing here names a folder or a path on the
worker's disk. Everything a graph or a handler raises that is not in ``WRITING_FAILURES`` is a
defect.
"""

from __future__ import annotations

from openmarketer_core.llm import LLMError


class WritingError(Exception):
    """The writer could not write the post; the message is worded for whoever asked."""


# What a run that proposed no post raises; anything else is a defect.
WRITING_FAILURES = (WritingError, LLMError)
