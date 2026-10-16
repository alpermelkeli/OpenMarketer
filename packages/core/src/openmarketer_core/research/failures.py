"""What a research run raises when it ends without a report, and nothing else.

The worker words a failure for whoever asked for the run with the message of
the error itself: unlike an analysis, nothing here names a folder or a path on
the worker's disk, so there is nothing to take out of it. Everything a graph or
a handler raises that is not in ``RESEARCH_FAILURES`` is a defect.
"""

from __future__ import annotations

from openmarketer_core.llm import LLMError
from openmarketer_core.research.research_agent import ResearchError

# What a run that produced no report raises; anything else is a defect.
RESEARCH_FAILURES = (ResearchError, LLMError)
